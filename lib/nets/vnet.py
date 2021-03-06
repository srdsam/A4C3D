import torch
import torch.nn as nn
import torch.nn.functional as F

try:
    from torch.cuda.amp import autocast, GradScaler
except:
    pass

def passthrough(x, **kwargs):
    return x


def ELUCons(elu, nchan):
    if elu:
        return nn.ELU(inplace=True)
    else:
        return nn.PReLU(nchan)


# normalization between sub-volumes is necessary
# for good performance
class ContBatchNorm3d(nn.modules.batchnorm._BatchNorm):
    def _check_input_dim(self, input):
        if input.dim() != 5:
            raise ValueError('expected 5D input (got {}D input)'
                             .format(input.dim()))
        # super(ContBatchNorm3d, self)._check_input_dim(input)

    def forward(self, input):
        self._check_input_dim(input)
        return F.batch_norm(
            input, self.running_mean, self.running_var, self.weight, self.bias,
            True, self.momentum, self.eps)


class LUConv(nn.Module):
    def __init__(self, nchan, elu, normfunc):
        super(LUConv, self).__init__()
        self.relu1 = ELUCons(elu, nchan)
        self.conv1 = nn.Conv3d(nchan, nchan, kernel_size=5, padding=2)
        self.norm1 = normfunc(nchan)

    def forward(self, x):
        out = self.relu1(self.norm1(self.conv1(x)))
        return out


def _make_nConv(nchan, depth, elu, normfunc):
    layers = []
    for _ in range(depth):
        layers.append(LUConv(nchan, elu, normfunc))
    return nn.Sequential(*layers)


class InputTransition(nn.Module):
    def __init__(self, outChans, elu, normfunc):
        super(InputTransition, self).__init__()
        self.outChans = outChans
        self.conv1 = nn.Conv3d(1, outChans, kernel_size=5, padding=2)
        self.norm1 = normfunc(outChans)
        self.relu1 = ELUCons(elu, outChans)

    def forward(self, x):
        # do we want a PRELU here as well?
        out = self.norm1(self.conv1(x))
        # split input in to 16 channels
        if self.outChans == 16:
            x = torch.cat((x, x, x, x, x, x, x, x,
                           x, x, x, x, x, x, x, x), 1)
        elif self.outChans == 32:
            x = torch.cat((x, x, x, x, x, x, x, x,
                           x, x, x, x, x, x, x, x,
                           x, x, x, x, x, x, x, x,
                           x, x, x, x, x, x, x, x), 1)
        out = self.relu1(torch.add(out, x))
        return out


class DownTransition(nn.Module):
    def __init__(self, inChans, nConvs, elu, normfunc, dropout=False):
        super(DownTransition, self).__init__()
        outChans = 2 * inChans
        self.down_conv = nn.Conv3d(inChans, outChans, kernel_size=2, stride=2)
        self.norm1 = normfunc(outChans)
        self.do1 = passthrough
        self.relu1 = ELUCons(elu, outChans)
        self.relu2 = ELUCons(elu, outChans)
        if dropout:
            self.do1 = nn.Dropout3d()
        self.ops = _make_nConv(outChans, nConvs, elu, normfunc)

    def forward(self, x):
        down = self.relu1(self.norm1(self.down_conv(x)))
        out = self.do1(down)
        out = self.ops(out)
        out = self.relu2(torch.add(out, down))
        return out


class UpTransition(nn.Module):
    def __init__(self, inChans, outChans, nConvs, elu, normfunc, dropout=False):
        super(UpTransition, self).__init__()
        self.up_conv = nn.ConvTranspose3d(inChans, outChans // 2, kernel_size=2, stride=2)
        self.norm1 = normfunc(outChans // 2)
        self.do1 = passthrough
        self.do2 = nn.Dropout3d()
        self.relu1 = ELUCons(elu, outChans // 2)
        self.relu2 = ELUCons(elu, outChans)
        if dropout:
            self.do1 = nn.Dropout3d()
        self.ops = _make_nConv(outChans, nConvs, elu, normfunc)

    def forward(self, x, skipx):
        out = self.do1(x)
        skipxdo = self.do2(skipx)
        out = self.relu1(self.norm1(self.up_conv(out)))
        xcat = torch.cat((out, skipxdo), 1)
        out = self.ops(xcat)
        out = self.relu2(torch.add(out, xcat))
        return out


class OutputTransition(nn.Module):
    def __init__(self, inChans, outChans, elu, normfunc):
        super(OutputTransition, self).__init__()
        self.conv1 = nn.Conv3d(inChans, outChans, kernel_size=5, padding=2)
        self.norm1 = normfunc(outChans)
        self.conv2 = nn.Conv3d(outChans, outChans, kernel_size=1)
        self.relu1 = ELUCons(elu, outChans)

    def forward(self, x):
        # convolve 32 down to 2 channels
        out = self.relu1(self.norm1(self.conv1(x)))
        out = self.conv2(out)
        return out


class VNet(nn.Module):
    def __init__(self, outChans, normfunc, multiplier=1, elu=True, mixed_precision=False):
        super(VNet, self).__init__()
        self.mixed_precision = mixed_precision
        self.normfunc = normfunc

        self.scaler = GradScaler() if mixed_precision else None
        self.in_tr = InputTransition(int(16*multiplier), elu, normfunc)
        self.down_tr32 = DownTransition(int(16*multiplier), 1, elu, normfunc)
        self.down_tr64 = DownTransition(int(32*multiplier), 2, elu, normfunc)
        self.down_tr128 = DownTransition(int(64*multiplier), 3, elu, normfunc, dropout=True)
        self.down_tr256 = DownTransition(int(128*multiplier), 2, elu, normfunc, dropout=True)
        self.up_tr256 = UpTransition(int(256*multiplier), int(256*multiplier), 2, elu, normfunc, dropout=True)
        self.up_tr128 = UpTransition(int(256*multiplier), int(128*multiplier), 2, elu, normfunc, dropout=True)
        self.up_tr64 = UpTransition(int(128*multiplier), int(64*multiplier), 1, elu, normfunc)
        self.up_tr32 = UpTransition(int(64*multiplier), int(32*multiplier), 1, elu, normfunc)
        self.out_tr = OutputTransition(int(32*multiplier), outChans, elu, normfunc)

    def fwd(self, x):
        """Separate function so we can wrap this with autocast if using mixed precision"""
        out16 = self.in_tr(x)
        out32 = self.down_tr32(out16)
        out64 = self.down_tr64(out32)
        out128 = self.down_tr128(out64)
        out256 = self.down_tr256(out128)
        out = self.up_tr256(out256, out128)
        out = self.up_tr128(out, out64)
        out = self.up_tr64(out, out32)
        out = self.up_tr32(out, out16)
        out = self.out_tr(out)
        return out

    def forward(self, x):
        if self.mixed_precision:
            with autocast():
                return self.fwd(x)
        else:
            return self.fwd(x)
