import torch
import torch.nn as nn
import common.data as data
from common.dto.CaeDto import CaeDto
import common.dto.CaeDto as CaeDtoUtil


class CaeBase(nn.Module):

    def __init__(self, size_input_xy=128, size_input_z=28, channels=[1, 16, 32, 64, 128, 1024, 128, 1], n_ch_global=2,
                 inner_xy=12, inner_z=3):
        super().__init__()
        assert size_input_xy % 4 == 0 and size_input_z % 4 == 0
        self.n_ch_block1 = channels[1]
        self.n_ch_block2 = channels[2]
        self.n_ch_block3 = channels[3]
        self.n_ch_block4 = channels[4]
        self.n_ch_block5 = channels[5]

        self._inner_ch = self.n_ch_block4
        self._inner_xy = inner_xy
        self._inner_z = inner_z

        self.n_ch_global = n_ch_global
        self.n_input = channels[0]
        self.n_classes = channels[-1]

    def freeze(self, freeze=False):
        requires_grad = not freeze
        for param in self.parameters():
            param.requires_grad = requires_grad


class Enc3D(CaeBase):
    def __init__(self, size_input_xy, size_input_z, channels, n_ch_global):
        super().__init__(size_input_xy, size_input_z, channels, n_ch_global, inner_xy=10, inner_z=3)

        self.trunk = nn.Sequential(
            nn.BatchNorm3d(self.n_input),
            nn.Conv3d(self.n_input, self.n_ch_block1, 3, stride=1, padding=(1, 0, 0)),
            nn.ReLU(True),
            nn.BatchNorm3d(self.n_ch_block1),
            nn.Conv3d(self.n_ch_block1, self.n_ch_block1, 3, stride=1, padding=(1, 0, 0)),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block1),
            nn.Conv3d(self.n_ch_block1, self.n_ch_block2, 3, stride=2, padding=1),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block2),
            nn.Conv3d(self.n_ch_block2, self.n_ch_block2, 3, stride=1, padding=(1, 0, 0)),
            nn.ReLU(True),
            nn.BatchNorm3d(self.n_ch_block2),
            nn.Conv3d(self.n_ch_block2, self.n_ch_block2, 3, stride=1, padding=(1, 0, 0)),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block2),
            nn.Conv3d(self.n_ch_block2, self.n_ch_block3, 3, stride=2, padding=1),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block3),
            nn.Conv3d(self.n_ch_block3, self.n_ch_block3, 3, stride=1, padding=(1, 0, 0)),
            nn.ReLU(True),
            nn.BatchNorm3d(self.n_ch_block3),
            nn.Conv3d(self.n_ch_block3, self.n_ch_block3, 3, stride=1, padding=(1, 0, 0)),
            nn.ReLU(True)
        )

        self.branch_bottleneck = nn.Sequential(
            nn.BatchNorm3d(self.n_ch_block3),
            nn.Conv3d(self.n_ch_block3, self.n_ch_block4, 3, stride=2, padding=0),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block4),
            nn.Conv3d(self.n_ch_block4, self.n_ch_block5, 3, stride=1, padding=0),
            nn.ReLU(True)
        )

        self.branch_linear = nn.Sequential(
            nn.BatchNorm3d(self.n_ch_block3),
            nn.Conv3d(self.n_ch_block3, self.n_ch_block3, 3, stride=2, padding=0),
            nn.ReLU(True),

            nn.MaxPool3d((1, 4, 4), (1, 4, 4)),

            nn.BatchNorm3d(self.n_ch_block3),
            nn.Conv3d(self.n_ch_block3, self.n_ch_block3, 3, stride=1, padding=0),
            nn.ReLU(True)
        )

        n_linear_concat = self.n_ch_block3 * 2 + self.n_ch_global
        n_linear_concat_up = n_linear_concat + self.n_ch_block5 // 2

        self.step_map_generator0 = nn.Sequential(
            nn.BatchNorm3d(n_linear_concat),
            nn.Conv3d(n_linear_concat, n_linear_concat, 1, stride=1, padding=0),
            nn.ReLU(True),

            nn.BatchNorm3d(n_linear_concat),
            nn.ConvTranspose3d(n_linear_concat, n_linear_concat_up, 5, stride=1, padding=(2,0,0), output_padding=0),
            nn.ReLU(True),

            nn.BatchNorm3d(n_linear_concat_up),
            nn.Conv3d(n_linear_concat_up, n_linear_concat_up, 3, stride=1, padding=1),
            nn.ReLU(True),
            nn.BatchNorm3d(n_linear_concat_up),
            nn.Conv3d(n_linear_concat_up, n_linear_concat_up, 3, stride=1, padding=1),
            nn.ReLU(True),

            nn.BatchNorm3d(n_linear_concat_up),
            nn.ConvTranspose3d(n_linear_concat_up, self.n_ch_block5, (1, 2, 2), stride=(1, 2, 2), padding=0, output_padding=0),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block5),
            nn.Conv3d(self.n_ch_block5, self.n_ch_block5, 3, stride=1, padding=1),
            nn.ReLU(True),
        )

        self.step_map_generator1 = nn.Conv3d(self.n_ch_block5, self.n_ch_block5, 1, stride=1, padding=0)
        torch.nn.init.normal(self.step_map_generator1.weight, 0, 0.001)  # crucial and important!
        torch.nn.init.normal(self.step_map_generator1.bias, -2, 0.01)  # crucial and important!

        self.sigmoid = nn.Sigmoid()  # slows down learning, but ensures [0,1] range and adds another non-linearity

    def _interpolate(self, latent_core, latent_penu, step):
        assert step is not None, 'Step must be given for interpolation!'
        if latent_core is None or latent_penu is None:
            return None
        core_to_penumbra = latent_penu - latent_core
        results = []
        for batch_sample in range(step.size()[0]):
            results.append(
                (latent_core[batch_sample, :, :, :, :] +
                 step[batch_sample, :, :, :, :] * core_to_penumbra[batch_sample, :, :, :, :]).unsqueeze(0)
            )
        return torch.cat(results, dim=0)

    def _forward_single(self, input_image):
        if input_image is None:
            return None, None

        trunk = self.trunk(input_image)
        branch_bottleneck = self.branch_bottleneck(trunk)
        branch_linear = self.branch_linear(trunk)

        return branch_bottleneck, branch_linear

    def _get_step(self, dto: CaeDto, linear_core, linear_penu):
        if dto.given_variables.time_to_treatment is None:
            concatenated = torch.cat((dto.given_variables.globals, linear_core, linear_penu), dim=data.DIM_CHANNEL_TORCH3D_5)
            return self.sigmoid(self.step_map_generator1(self.step_map_generator0(concatenated)))
        assert not self.training  # provide step only for visualization
        return dto.given_variables.time_to_treatment

    def forward(self, dto: CaeDto):
        if dto.flag == CaeDtoUtil.FLAG_GTRUTH or dto.flag == CaeDtoUtil.FLAG_DEFAULT:
            assert dto.latents.gtruth._is_empty()  # Don't accidentally overwrite other results by code mistakes
            dto.latents.gtruth.lesion, _ = self._forward_single(dto.given_variables.gtruth.lesion)
            dto.latents.gtruth.core, linear_core = self._forward_single(dto.given_variables.gtruth.core)
            dto.latents.gtruth.penu, linear_penu = self._forward_single(dto.given_variables.gtruth.penu)
            step = self._get_step(dto, linear_core, linear_penu)
            dto.latents.gtruth.interpolation = self._interpolate(dto.latents.gtruth.core,
                                                                 dto.latents.gtruth.penu,
                                                                 step)
        return dto


class Dec3D(CaeBase):
    def __init__(self, size_input_xy, size_input_z, channels, n_ch_global):
        super().__init__(size_input_xy, size_input_z, channels, n_ch_global, inner_xy=10, inner_z=3)

        self.decoder = nn.Sequential(
            nn.BatchNorm3d(self.n_ch_block5),
            nn.ConvTranspose3d(self.n_ch_block5, self.n_ch_block4, 3, stride=1, padding=0, output_padding=0),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block4),
            nn.ConvTranspose3d(self.n_ch_block4, self.n_ch_block3, 3, stride=2, padding=0, output_padding=0),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block3),
            nn.Conv3d(self.n_ch_block3, self.n_ch_block3, 3, stride=1, padding=(1, 2, 2)),
            nn.ReLU(True),
            nn.BatchNorm3d(self.n_ch_block3),
            nn.Conv3d(self.n_ch_block3, self.n_ch_block2, 3, stride=1, padding=(1, 2, 2)),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block2),
            nn.ConvTranspose3d(self.n_ch_block2, self.n_ch_block2, 2, stride=2, padding=0, output_padding=0),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block2),
            nn.Conv3d(self.n_ch_block2, self.n_ch_block2, 3, stride=1, padding=(1, 2, 2)),
            nn.ReLU(True),
            nn.BatchNorm3d(self.n_ch_block2),
            nn.Conv3d(self.n_ch_block2, self.n_ch_block1, 3, stride=1, padding=(1, 2, 2)),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block1),
            nn.ConvTranspose3d(self.n_ch_block1, self.n_ch_block1, 2, stride=2, padding=0, output_padding=0),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block1),
            nn.Conv3d(self.n_ch_block1, self.n_ch_block1, 3, stride=1, padding=(1, 2, 2)),
            nn.ReLU(True),
            nn.BatchNorm3d(self.n_ch_block1),
            nn.Conv3d(self.n_ch_block1, self.n_ch_block1, 3, stride=1, padding=(1, 2, 2)),
            nn.ReLU(True),

            nn.BatchNorm3d(self.n_ch_block1),
            nn.Conv3d(self.n_ch_block1, self.n_ch_block1, 1, stride=1, padding=0),
            nn.ReLU(True),
            nn.BatchNorm3d(self.n_ch_block1),
            nn.Conv3d(self.n_ch_block1, self.n_classes, 1, stride=1, padding=0),
            nn.Sigmoid()
        )

    def _forward_single(self, input_latent):
        if input_latent is None:
            return None
        return self.decoder(input_latent)

    def forward(self, dto: CaeDto):
        if dto.flag == CaeDtoUtil.FLAG_GTRUTH or dto.flag == CaeDtoUtil.FLAG_DEFAULT:
            assert dto.reconstructions.gtruth._is_empty()  # Don't accidentally overwrite other results by code mistakes
            dto.reconstructions.gtruth.core = self._forward_single(dto.latents.gtruth.core)
            dto.reconstructions.gtruth.penu = self._forward_single(dto.latents.gtruth.penu)
            dto.reconstructions.gtruth.lesion = self._forward_single(dto.latents.gtruth.lesion)
            dto.reconstructions.gtruth.interpolation = self._forward_single(dto.latents.gtruth.interpolation)
        if dto.flag == CaeDtoUtil.FLAG_INPUTS or dto.flag == CaeDtoUtil.FLAG_DEFAULT:
            assert dto.reconstructions.inputs._is_empty()  # Don't accidentally overwrite other results by code mistakes
            dto.reconstructions.inputs.core = self._forward_single(dto.latents.inputs.core)
            dto.reconstructions.inputs.penu = self._forward_single(dto.latents.inputs.penu)
            dto.reconstructions.inputs.interpolation = self._forward_single(dto.latents.inputs.interpolation)
        return dto


class Cae3D(nn.Module):
    def __init__(self, enc: Enc3D, dec: Dec3D):
        super().__init__()
        self.enc = enc
        self.dec = dec

    def forward(self, dto: CaeDto):
        dto = self.enc(dto)
        dto = self.dec(dto)
        return dto

    def freeze(self, freeze: bool):
        self.enc.freeze(freeze)
        self.dec.freeze(freeze)
