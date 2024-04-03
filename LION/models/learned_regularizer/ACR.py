# This file is part of LION library
# License : BSD-3
#
# Author  : Subhadip Mukherjee
# Modifications: Ander Biguri, Zakhar Shumaylov
# =============================================================================


import torch
import torch.nn as nn
from LION.models import LIONmodel
from LION.utils.parameter import LIONParameter
import torch.nn.utils.parametrize as P


class Positive(nn.Module):
    def forward(self, X):
        return torch.clip(X, min=0.0)


class ICNN_layer(nn.Module):
    def __init__(self, channels, kernel_size=3, stride=1, relu_type="LeakyReLU"):
        super().__init__()

        # The paper diagram is in color, channels are described by "blue" and "orange"
        # The blue layers are W_z, and hence have to be positive, and not have bias terms
        self.blue = nn.Conv2d(
            in_channels=channels,
            out_channels=channels,
            kernel_size=kernel_size,
            stride=stride,
            padding="same",
            bias=False,
        )
        P.register_parametrization(self.blue, "weight", Positive())
        
        # The orange layers are W_x
        self.orange = nn.Conv2d(
            in_channels=1,
            out_channels=channels,
            kernel_size=kernel_size,
            stride=stride,
            padding="same",
            bias=True,
        )
        if relu_type == "LeakyReLU":
            self.activation = nn.LeakyReLU(negative_slope=0.2)
        elif relu_type == "ReLU":
            self.activation = nn.ReLU()
        else:
            raise ValueError(
                "Only Leaky ReLU supported (needs to be a convex and monotonically nondecreasin fun)"
            )

    def forward(self, z, x0):
        res = self.blue(z) + self.orange(x0)
        res = self.activation(res)
        return res


###An L2 term with learnable weight
# define a network for training the L2 term
class L2net(nn.Module):
    def __init__(self):
        super(L2net, self).__init__()

        self.l2_penalty = nn.Parameter((-9.0) * torch.ones(1))

    def forward(self, x):
        l2_term = torch.sum(x.view(x.size(0), -1) ** 2, dim=1)
        out = ((torch.nn.functional.softplus(self.l2_penalty)) * l2_term).view(
            x.size(0), -1
        )
        return out


# sparsifying filter-bank (SFB) module
class SFB(LIONmodel.LIONmodel):
    def __init__(self, model_parameters):

        super().__init__(model_parameters)
        # FoE kernels
        self.penalty = nn.Parameter((-12.0) * torch.ones(1))
        self.n_kernels = model_parameters.n_kernels
        self.conv = nn.ModuleList(
            [
                nn.Conv2d(
                    1,
                    model_parameters.n_filters,
                    kernel_size=7,
                    stride=1,
                    padding=3,
                    bias=False,
                )
                for i in range(self.n_kernels)
            ]
        )
        if model_parameters.L2net:
            self.L2net = L2net()

    @staticmethod
    def default_parameters():
        param = LIONParameter()
        param.n_kernels = 10
        param.n_filters = 32
        param.L2net = True
        return param

    def forward(self, x):
        # compute the output of the FoE part
        total_out = 0.0
        for kernel_idx in range(self.n_kernels):
            x_out = torch.abs(self.conv[kernel_idx](x))
            x_out_flat = x_out.view(x.size(0), -1)
            total_out += torch.sum(x_out_flat, dim=1)

        total_out = total_out.view(x.size(0), -1)
        out = (torch.nn.functional.softplus(self.penalty)) * total_out
        if self.model_parameters.L2net:
            out = out + self.L2net(x)
        return out


class ACR(LIONmodel.LIONmodel):
    def __init__(self, model_parameters: LIONParameter = None):

        super().__init__(model_parameters)

        # First Conv
        self.first_layer = nn.Conv2d(
            in_channels=1,
            out_channels=model_parameters.channels,
            kernel_size=model_parameters.kernel_size,
            stride=model_parameters.stride,
            padding="same",
            bias=True,
        )

        if model_parameters.relu_type == "LeakyReLU":
            self.first_activation = nn.LeakyReLU(negative_slope=0.2)
        elif model_parameters.relu_type == "ReLU":
            self.first_activation = nn.ReLU()
        else:
            raise ValueError(
                "Only Leaky ReLU supported (needs to be a convex and monotonically nondecreasin fun)"
            )
            

        for i in range(model_parameters.layers):
            self.add_module(
                f"ICNN_layer_{i}",
                ICNN_layer(
                    channels=model_parameters.channels,
                    kernel_size=model_parameters.kernel_size,
                    stride=model_parameters.stride,
                    relu_type=model_parameters.relu_type,
                ),
            )

        self.last_layer = nn.Conv2d(
            in_channels=model_parameters.channels,
            out_channels=1,
            kernel_size=model_parameters.kernel_size,
            stride=model_parameters.stride,
            padding="same",
            bias=False,
        )
        P.register_parametrization(self.last_layer, "weight", Positive())

        self.pool = nn.AdaptiveAvgPool2d((1, 1))
        self.initialize_weights()

    # a weight initialization routine for the ICNN
    def initialize_weights(self, min_val=0.0, max_val=0.001):
        device = torch.cuda.current_device()
        for i in range(self.model_parameters.layers):
            block = getattr(self, f"ICNN_layer_{i}")
            block.blue.weight.data = min_val + (max_val - min_val) * torch.rand(
                self.model_parameters.channels,
                self.model_parameters.channels,
                self.model_parameters.kernel_size,
                self.model_parameters.kernel_size,
            ).to(device)

        return self

    def forward(self, x):
        z = self.first_layer(x)
        z = self.first_activation(z)
        for i in range(self.model_parameters.layers):
            layer = primal_module = getattr(self, f"ICNN_layer_{i}")
            z = layer(z, x)
        z = self.last_layer(z)
        return self.pool(z)

    @staticmethod
    def default_parameters():
        param = LIONParameter()
        param.channels = 48
        param.kernel_size = 5
        param.stride = 1
        param.relu_type = "LeakyReLU"
        param.layers = 10
        param.var_step = 1e-5
        param.var_momentum = 0.0
        param.output = True
        param.earlystop = True
        return param
    
    #This functions takes in a sinogram and returns the reconstructed image
    def reconstruction(self, y_0,x_t = None):
        fbp = self.fbp_op_mod
        fwd = self.fwd_op_mod
        adj = self.fwd_op_mod.adjoint
        x_0 = fbp(y_0)
        lambd = 1 #regularistaion parameter
        grad = torch.zeros(x_0.shape).type_as(x_0)

        x_0=torch.nn.Parameter(x_0)
        optimizer = torch.optim.SGD([x_0], lr=self.param.var_step, momentum=self.param.var_momentum)
        self.cntr+=1
        prevpsn=0
        curpsn=0
        for j in range(self.args.iterates):
            data_misfit=fwd(x_0)-y_0
            grad = adj(data_misfit)
            if(x_t is not None):
                loss = nn.MSELoss()(x_0.detach(),x_t.detach().cuda())
                cur_loss = 0
                ssim = self.ssim(x_0.detach(),x_t.detach())
                psnr = self.psnr(x_0.detach(),x_t.detach())
                if(self.params.outp):
                    print(j)
                    print('MSE Loss:', loss.item())
                    print('SSIM:',ssim)
                    print('PSNR:',psnr)
                prevpsn=curpsn
                curpsn=psnr
                if(self.params.earlystop is True and curpsn<prevpsn):
                    return x_0

            lossm=lambd*self(x_0).sum()
            lossm.backward()
            x_0.grad+=grad
            optimizer.step()
        return x_0

    @staticmethod
    def cite(cite_format="MLA"):
        if cite_format == "MLA":
            print("Mukherjee, Subhadip, et al.")
            print('"Learned convex regularizers for inverse problems."')
            print("\x1B[3marXiv preprint \x1B[0m")
            print("arXiv:2008.02839 (2020).")
        elif cite_format == "bib":
            string = """
            @article{mukherjee2020learned,
            title={Learned convex regularizers for inverse problems},
            author={Mukherjee, Subhadip and Dittmer, S{\"o}ren and Shumaylov, Zakhar and Lunz, Sebastian and {\"O}ktem, Ozan and Sch{\"o}nlieb, Carola-Bibiane},
            journal={arXiv preprint arXiv:2008.02839},
            year={2020}
            }"""
            print(string)
        else:
            raise AttributeError(
                'cite_format not understood, only "MLA" and "bib" supported'
            )
