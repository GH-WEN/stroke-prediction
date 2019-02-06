"""
Based on:
https://github.com/jacobkimmel/pytorch_convgru
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.checkpoint import checkpoint


def affine_identity(n=1):
    result = []
    for i in range(n):
        result += [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
    return torch.tensor(result, dtype=torch.float)


def grid_identity(batch_size=4, n=1, out_size=(4, 1, 28, 128, 128)):
    result = affine_identity(1)
    result = result.view(-1, 3, 4).expand(batch_size, 3, 4).cuda()
    return torch.cat([nn.functional.affine_grid(result, out_size) for _ in range(n)], dim=4)


def def_vec2vec(n_dim, final_activation=None, init_fn=lambda x: x):
    assert len(n_dim) > 1

    result = []
    for i in range(1, len(n_dim) - 1):
        result += [nn.Linear(n_dim[i - 1], n_dim[i]), nn.ReLU(True), nn.Dropout()]
    result += [nn.Linear(n_dim[len(n_dim) - 2], n_dim[len(n_dim) - 1])]

    if final_activation:
        if final_activation.lower() == 'relu':
            result.append(nn.ReLU())
        elif final_activation.lower() == 'sigmoid':
            result.append(nn.Sigmoid())
        elif final_activation.lower() == 'tanh':
            result.append(nn.Tanh())
        else:
            raise AssertionError('Unknown final activation function')

    return nn.Sequential(*init_fn(result))


def def_img2vec(n_dim, depth2d=False):
    assert len(n_dim) == 5
    ksize = 3
    ksize2 = (1, 3, 3)
    psize = 1
    dsize = (1, 2, 2)
    if depth2d:
        ksize = (1, ksize, ksize)
        psize = (0, psize, psize)
    return nn.Sequential(
        nn.InstanceNorm3d(n_dim[0]),  # 128x128x28
        nn.Conv3d(n_dim[0], n_dim[1], kernel_size=ksize, padding=psize),  # 128x128x28
        nn.ReLU(),
        nn.MaxPool3d(4, 4),  # 32x32x7
        nn.InstanceNorm3d(n_dim[1]),
        nn.Conv3d(n_dim[1], n_dim[2], kernel_size=ksize, padding=psize),  # 32x32x7
        nn.ReLU(),
        nn.MaxPool3d(dsize, dsize),  # 16x16x7
        nn.InstanceNorm3d(n_dim[2]),
        nn.Conv3d(n_dim[2], n_dim[3], kernel_size=ksize2),  # 14x14x7
        nn.ReLU(),
        nn.MaxPool3d(dsize, dsize),  # 7x7x7
        nn.InstanceNorm3d(n_dim[3]),
        nn.Conv3d(n_dim[3], n_dim[4], kernel_size=1),  # 7x7x7
        nn.ReLU(),
        nn.MaxPool3d(7)  # 1x1x1
    )


def time2index(time, thresholds):
    assert thresholds
    idx = 0
    while time > thresholds[idx]:
        idx += 1
    if idx > len(thresholds) :
        raise Exception('Invalid time >' + thresholds[-1])
    return idx


def tensor2index(time_tensor, thresholds):
    assert thresholds
    indices = -1 * torch.ones(time_tensor.size())
    batchsize = time_tensor.size(0)
    for b in range(batchsize):
        indices[b] = time2index(time_tensor[b], thresholds)
    return indices


class GRUnetBlock(nn.Module):
    def __init__(self, input_size, hidden_size, kernel_size, output_size=None):
        super().__init__()

        # Allow for anisotropic inputs
        if (isinstance(kernel_size, tuple) or isinstance(kernel_size, list)) and len(kernel_size) == 3:
            padding = (kernel_size[0] // 2, kernel_size[1] // 2, kernel_size[2] // 2)
        else:
            padding = kernel_size // 2
        self.input_size = input_size

        # GRU convolution with incorporation of hidden state
        self.hidden_size = hidden_size
        self.reset_gate = nn.Conv3d(input_size + hidden_size, hidden_size, kernel_size, padding=padding)
        self.update_gate = nn.Conv3d(input_size + hidden_size, hidden_size, kernel_size, padding=padding)
        self.out_gate = nn.Conv3d(input_size + hidden_size, hidden_size, kernel_size, padding=padding)

        # Additional "normal" convolution as in vanilla Unet to map to another channel number
        if output_size is None:
            output_size = hidden_size
        self.conv3d = nn.Conv3d(hidden_size, output_size, kernel_size, padding=padding)

        # Appropriate initialization
        nn.init.orthogonal_(self.reset_gate.weight)
        nn.init.orthogonal_(self.update_gate.weight)
        nn.init.orthogonal_(self.out_gate.weight)
        nn.init.xavier_normal(self.conv3d.weight)
        nn.init.normal(self.conv3d.bias)
        nn.init.constant_(self.reset_gate.bias, 0.)
        nn.init.constant_(self.update_gate.bias, 0.)
        nn.init.constant_(self.out_gate.bias, 0.)

    def forward(self, input_, prev_state):
        # Get batch and spatial sizes
        batch_size = input_.data.size()[0]
        spatial_size = input_.data.size()[2:]

        # Generate empty prev_state, if None is provided
        if prev_state is None:
            state_size = [batch_size, self.hidden_size] + list(spatial_size)
            if torch.cuda.is_available():
                prev_state = torch.zeros(state_size).cuda()
            else:
                prev_state = torch.zeros(state_size)

        # Data size: [batch, channel, depth, height, width]
        stacked_inputs = torch.cat([input_, prev_state], dim=1)
        update = torch.sigmoid(self.update_gate(stacked_inputs))
        reset = torch.sigmoid(self.reset_gate(stacked_inputs))
        out_inputs = torch.tanh(self.out_gate(torch.cat([input_, prev_state * reset], dim=1)))
        new_state = prev_state * (1 - update) + out_inputs * update

        output = self.conv3d(new_state)

        return new_state, output


class GRUnet(nn.Module):
    def unet_def(self, h_sizes, k_sizes):
        return [GRUnetBlock(h_sizes[0], h_sizes[0], k_sizes[0], output_size=h_sizes[0]),
                GRUnetBlock(h_sizes[0], h_sizes[1], k_sizes[1], output_size=h_sizes[1]),
                GRUnetBlock(h_sizes[1], h_sizes[2], k_sizes[2], output_size=h_sizes[1]),
                GRUnetBlock(h_sizes[1] + h_sizes[1], h_sizes[3], k_sizes[3], output_size=h_sizes[0]),
                GRUnetBlock(h_sizes[0] + h_sizes[0], h_sizes[4], k_sizes[4], output_size=h_sizes[0])]

    def __init__(self, hidden_sizes, kernel_sizes, down_scaling):
        self.N_BLOCKS = 5

        super().__init__()

        if type(hidden_sizes) != list:
            self.hidden_sizes = [hidden_sizes] * self.N_BLOCKS
        else:
            assert len(hidden_sizes) == self.N_BLOCKS, '`hidden_sizes` must have the same length as n_layers'
            self.hidden_sizes = hidden_sizes
        if type(kernel_sizes) != list:
            self.kernel_sizes = [kernel_sizes] * self.N_BLOCKS
        else:
            assert len(kernel_sizes) == self.N_BLOCKS, '`kernel_sizes` must have the same length as n_layers'
            self.kernel_sizes = kernel_sizes

        self.blocks = self.unet_def(hidden_sizes, kernel_sizes)
        for i in range(len(self.blocks)):
            setattr(self, 'GRUnetBlock' + str(i).zfill(2), self.blocks[i])

        # pooling between blocks / levels
        pool = 2
        if type(kernel_sizes[0]) == tuple:
            pool = (1, 2, 2)
        self.pool = nn.MaxPool3d(pool, pool, return_indices=True)
        self.unpool = nn.MaxUnpool3d(pool, pool)

        # Grid offset prediction
        self.grid_offset = nn.Conv3d(self.blocks[-1].conv3d.out_channels, 6, 1)
        torch.nn.init.normal(self.grid_offset.weight, 0, 0.001)
        torch.nn.init.normal(self.grid_offset.bias, 0, 0.001)

        self.downscaling = down_scaling

    def forward(self, input_rep, *hidden):
        input_rep = F.interpolate(input_rep, scale_factor=(1, 1/self.downscaling, 1/self.downscaling))

        outputs = [None] * (self.N_BLOCKS // 2 + 1)
        indices = [None] * (self.N_BLOCKS // 2)
        upd_hidden = [None] * self.N_BLOCKS
        if len(hidden) == 1 and hidden[0] is None:
            hidden = [None] * (self.N_BLOCKS + 2)

        #
        # Non-lin deform

        for i in range(self.N_BLOCKS // 2):
           # upd_block_hidden, output = checkpoint(lambda a, b: self.blocks[i](a, b), input_rep, hidden[i])
            upd_block_hidden, output = self.blocks[i](input_rep, hidden[i])
            upd_hidden[i] = upd_block_hidden
            outputs[i] = output

            input_rep, indices_ = self.pool(output)
            indices[i] = indices_
        del indices_

        upd_block_hidden, output = self.blocks[self.N_BLOCKS // 2](input_rep, hidden[self.N_BLOCKS // 2])
        upd_hidden[self.N_BLOCKS // 2] = upd_block_hidden
        outputs[self.N_BLOCKS // 2] = output

        for i in range(self.N_BLOCKS // 2):
            unpool = self.unpool(output, indices[self.N_BLOCKS // 2 - (i + 1)])
            skip = outputs[self.N_BLOCKS // 2 - (i + 1)]
            input_rep = torch.cat((unpool, skip), dim=1)

            j = self.N_BLOCKS // 2 + (i + 1)
            upd_block_hidden, output = self.blocks[j](input_rep, hidden[j])
            upd_hidden[j] = upd_block_hidden

        del upd_block_hidden
        del outputs
        del input_rep
        del unpool
        del skip

        output = F.interpolate(self.grid_offset(output), scale_factor=(1, self.downscaling, self.downscaling))

        return output.permute(0, 2, 3, 4, 1), upd_hidden[0], upd_hidden[1], upd_hidden[2], upd_hidden[3], upd_hidden[4]


class AffineModule(nn.Module):
    def init_theta(self, pos):
        def _init(sequential):
            sequential[pos].weight.data.zero_()
            sequential[pos].bias.data.copy_(affine_identity(2))
            return sequential

        return _init

    def __init__(self, dim_img2vec, dim_vec2vec, dim_clinical, kernel_size, seq_len, depth2d=False):
        super().__init__()

        dim_in_img = dim_img2vec[0]
        dim_hidden = dim_img2vec[-1] + dim_clinical
        assert dim_vec2vec[0] == dim_hidden
        assert dim_vec2vec[-1] == 24  # core and penumbra affine parameters

        self.len = seq_len

        self.affine1 = GRUnetBlock(dim_in_img, dim_in_img, kernel_size)
        self.affine2 = def_img2vec(n_dim=dim_img2vec, depth2d=depth2d)
        self.affine3 = nn.GRUCell(dim_hidden, dim_hidden, bias=True)
        self.affine4 = def_vec2vec(n_dim=dim_vec2vec, init_fn=self.init_theta(-1))
        self.affine5 = nn.GRUCell(24, 24, bias=True)

    def forward(self, input_img, clinical, core, hidden_affine1, hidden_affine3, hidden_affine5):
        out_size = core.size()
        del core
        hidden_affine1, affine1 = self.affine1(input_img, hidden_affine1)
        del input_img
        affine2 = self.affine2(affine1)
        hidden_affine3 = self.affine3(torch.cat((affine2, clinical), dim=1).squeeze(), hidden_affine3)
        del clinical
        affine4 = self.affine4(hidden_affine3)
        hidden_affine5 = self.affine5(affine4, hidden_affine5)
        grid_core = nn.functional.affine_grid(hidden_affine5[:, :12].view(-1, 3, 4), out_size)
        grid_penu = nn.functional.affine_grid(hidden_affine5[:, 12:].view(-1, 3, 4), out_size)

        return torch.cat((grid_core, grid_penu), dim=4), hidden_affine1, hidden_affine3, hidden_affine5


class LesionPositionModule(nn.Module):
    def __init__(self, dim_img2vec, dim_vec2vec, dim_clinical, kernel_size, seq_len, depth2d=False):
        super().__init__()

        dim_in_img = dim_img2vec[0]
        dim_hidden = dim_img2vec[-1] + dim_clinical
        assert dim_vec2vec[0] == dim_hidden

        self.len = seq_len

        self.affine1 = GRUnetBlock(dim_in_img, dim_in_img, kernel_size)
        self.affine2 = def_img2vec(n_dim=dim_img2vec, depth2d=depth2d)
        self.affine3 = nn.GRUCell(dim_hidden, dim_hidden, bias=True)
        self.affine4 = def_vec2vec(n_dim=dim_vec2vec, final_activation='sigmoid')

        torch.nn.init.normal(self.affine4[-2].weight, 0, 0.001)
        torch.nn.init.normal(self.affine4[-2].bias, 0, 0.1)


    def forward(self, input_img, clinical, hidden_affine1, hidden_affine3, hidden_affine5):

        hidden_affine1, affine1 = self.affine1(input_img, hidden_affine1)
        affine2 = self.affine2(affine1)
        hidden_affine3 = self.affine3(torch.cat((affine2, clinical), dim=1).squeeze(), hidden_affine3)
        affine4 = self.affine4(hidden_affine3)

        return affine4, hidden_affine1, hidden_affine3, hidden_affine5


class UnidirectionalSequence(nn.Module):
    def _init_zero_normal(self, pos):
        def _init(sequential):
            sequential[pos].weight.data.normal_(mean=0, std=0.001)  # for Sigmoid()=0.5 init
            sequential[pos].bias.data.normal_(mean=0, std=0.1)  # for Sigmoid()=0.5 init
            return sequential

        return _init

    def __init__(self, n_ch_grunet, dim_img2vec_affine, dim_vec2vec_affine, dim_img2vec_time, dim_vec2vec_time,
                 dim_clinical, dim_feat_rnn, kernel_size, seq_len, batchsize=4, out_size=6, depth2d=False,
                 reverse=False, add_factor=False, clinical_grunet=False):
        super().__init__()

        self.len = seq_len
        self.batchsize = batchsize
        self.out_size = out_size
        self.reverse = reverse
        self.add_factor = add_factor
        self.clinical_grunet = clinical_grunet

        self.grid_identity = grid_identity(batchsize, out_size=(batchsize, out_size, 28, 128, 128))

        #
        # Separate (hidden) features for core / penumbra
        self.core_rep = GRUnetBlock(dim_feat_rnn, dim_feat_rnn, kernel_size)
        self.penu_rep = GRUnetBlock(dim_feat_rnn, dim_feat_rnn, kernel_size)

        #
        # Affine
        self.affine = None
        if dim_img2vec_affine and dim_vec2vec_affine:
            assert dim_img2vec_affine[0] == 2 * dim_feat_rnn + 4, '{} != 2 * {} + 4'.format(int(dim_img2vec_affine[0]), int(dim_feat_rnn))  # ... + 4 = ... + 2 core/penumbra + 2 previous deform
            assert dim_img2vec_affine[-1] + dim_clinical == dim_vec2vec_affine[0]
            self.affine = AffineModule(dim_img2vec_affine, dim_vec2vec_affine, dim_clinical, kernel_size, seq_len, depth2d=depth2d)

        #
        # Non-lin.
        self.grunet = None
        if n_ch_grunet:
            self.grunet = GRUnet(hidden_sizes=n_ch_grunet, kernel_sizes=[kernel_size] * 5, down_scaling=2)

        assert self.grunet or self.affine, 'Either affine or non-lin. deformation parameter numbers must be given'

        #
        # Time position
        self.lesion_pos = None
        if dim_img2vec_time and dim_vec2vec_time:
            assert dim_img2vec_time[-1] + dim_clinical == dim_vec2vec_time[0]
            self.lesion_pos = LesionPositionModule(dim_img2vec_time, dim_vec2vec_time, dim_clinical, kernel_size,
                                                   seq_len, depth2d=depth2d)

    def forward(self, core, penu, core_rep, penu_rep, clinical, factor):
        offset = []
        pr_time = []

        if self.reverse:
            factor = 1 - factor

        hidden_core = torch.zeros(self.batchsize, self.core_rep.hidden_size, 28, 128, 128).cuda()
        hidden_penu = torch.zeros(self.batchsize, self.penu_rep.hidden_size, 28, 128, 128).cuda()

        if self.grunet:
            hidden_grunet = [torch.zeros([self.batchsize, self.grunet.blocks[0].hidden_size, 28, 64, 64]).cuda(),
                             torch.zeros([self.batchsize, self.grunet.blocks[1].hidden_size, 14, 32, 32]).cuda(),
                             torch.zeros([self.batchsize, self.grunet.blocks[2].hidden_size, 7, 16, 16]).cuda(),
                             torch.zeros([self.batchsize, self.grunet.blocks[3].hidden_size, 14, 32, 32]).cuda(),
                             torch.zeros([self.batchsize, self.grunet.blocks[4].hidden_size, 28, 64, 64]).cuda()]
        if self.affine:
            h_affine1 = torch.zeros(self.batchsize, self.affine.affine1.hidden_size, 28, 128, 128).cuda()
            h_affine3 = torch.zeros((self.batchsize, self.affine.affine3.hidden_size)).cuda()
            h_affine5 = torch.zeros((self.batchsize, 24)).cuda()
        if self.lesion_pos:
            h_time1 = None
            h_time3 = torch.zeros((self.batchsize, self.lesion_pos.affine3.hidden_size)).cuda()
            h_time5 = torch.zeros((self.batchsize, 1)).cuda()

        for i in range(self.len):
            if i == 0:
                if self.reverse:
                    previous_result = torch.cat((penu, penu), dim=1)
                else:
                    previous_result = torch.cat((core, core), dim=1)
            else:
                previous_result = torch.cat((nn.functional.grid_sample(core, self.grid_identity + offset[-1][:, :, :, :, :3]),
                                             nn.functional.grid_sample(penu, self.grid_identity + offset[-1][:, :, :, :, 3:])), dim=1)

            if self.add_factor:
                clinical_step = torch.cat((clinical, factor[:, i]), dim=1)
            else:
                clinical_step = clinical

            hidden_core, core_rep = checkpoint(lambda a, b: self.core_rep(a, b), core_rep, hidden_core)  #self.core_rep(core_rep, hidden_core)
            hidden_penu, penu_rep = checkpoint(lambda a, b: self.penu_rep(a, b), penu_rep, hidden_penu)  #self.penu_rep(penu_rep, hidden_core)
            input_img = torch.cat((core_rep, penu_rep, core, penu, previous_result), dim=1)

            if self.affine:
                affine_grids, h_affine1, h_affine3, h_affine5 = checkpoint(lambda a, b, c, d, e, f: self.affine(a, b, c, d, e, f), input_img, clinical_step, core, h_affine1, h_affine3, h_affine5)
                input_grunet = torch.cat((input_img, affine_grids.permute(0, 4, 1, 2, 3)), dim=1)
            else:
                input_grunet = input_img

            if self.grunet:
                if self.clinical_grunet:
                    input_grunet = torch.cat((F.interpolate(clinical_step, input_grunet.size()[2:5]), input_grunet), dim=1)
                nonlin_grids, h0, h1, h2, h3, h4 = checkpoint(lambda a, b, c, d, e, f: self.grunet(a, b, c, d, e, f), input_grunet, *hidden_grunet)
                hidden_grunet = [h0, h1, h2, h3, h4]
                offset.append(nonlin_grids)
            else:
                offset.append(affine_grids)

            if self.lesion_pos:
                lesion_pos, h_time1, h_time3, h_time5 = self.lesion_pos(
                    torch.cat((input_img, offset[-1].permute(0, 4, 1, 2, 3)), dim=1),
                    clinical_step,
                    h_time1,
                    h_time3,
                    h_time5
                )
                pr_time.append(lesion_pos)

        return offset, pr_time  # lesion cannot be first or last!


class BidirectionalSequence(nn.Module):
    def common_rep(self, n_in, n_out, k_size=(1,3,3), p_size=(0,1,1)):
        return nn.Sequential(
            nn.InstanceNorm3d(n_in),
            nn.Conv3d(n_in, n_out, kernel_size=k_size, padding=p_size),
            nn.ReLU(),
            nn.InstanceNorm3d(n_out),
            nn.Conv3d(n_out, n_out, kernel_size=k_size, padding=p_size),
            nn.ReLU()
        )

    def visualise_grid(self, batchsize):
        visual_grid = torch.ones(batchsize, 1, 28, 128, 128, requires_grad=False).cuda()
        visual_grid[:, :, 1::4, :, :] = 0.75
        visual_grid[:, :, 2::4, :, :] = 0.5
        visual_grid[:, :, 3::4, :, :] = 0.75
        visual_grid[:, :, :, 3::24, :] = 0
        visual_grid[:, :, :, 4::24, :] = 0
        visual_grid[:, :, :, 5::24, :] = 0
        visual_grid[:, :, :, :, 3::24] = 0
        visual_grid[:, :, :, :, 4::24] = 0
        visual_grid[:, :, :, :, 5::24] = 0
        return visual_grid

    def __init__(self, n_ch_feature_single, n_ch_affine_img2vec, n_ch_affine_vec2vec, dim_img2vec_time,
                 dim_vec2vec_time, n_ch_grunet, n_ch_clinical, kernel_size, seq_len, batch_size=4, out_size=6,
                 depth2d=False, add_factor=False, soften_kernel=(3, 13, 13), clinical_grunet=False):
        super().__init__()
        self.len = seq_len
        assert seq_len > 0

        self.add_factor = add_factor

        self.grid_identity = grid_identity(batch_size, out_size=(batch_size, out_size, 28, 128, 128))

        self.visual_grid = self.visualise_grid(batch_size)

        ##############################################################
        # Part 1: Commonly used separate core/penumbra representations
        self.common_core = self.common_rep(1, n_ch_feature_single)
        self.common_penu = self.common_rep(1, n_ch_feature_single)

        ##################################
        # Part 2: Bidirectional Recurrence
        self.rnn1 = UnidirectionalSequence(n_ch_grunet, n_ch_affine_img2vec, n_ch_affine_vec2vec, dim_img2vec_time,
                                           dim_vec2vec_time, n_ch_clinical, n_ch_feature_single, kernel_size, seq_len,
                                           batchsize=batch_size, out_size=out_size, depth2d=depth2d,
                                           add_factor=add_factor, clinical_grunet=clinical_grunet)
        self.rnn2 = UnidirectionalSequence(n_ch_grunet, n_ch_affine_img2vec, n_ch_affine_vec2vec, dim_img2vec_time,
                                           dim_vec2vec_time, n_ch_clinical, n_ch_feature_single, kernel_size, seq_len,
                                           batchsize=batch_size, out_size=out_size, depth2d=depth2d, reverse=True,
                                           add_factor=add_factor, clinical_grunet=clinical_grunet)

        ################################################
        # Part 3: Combine predictions of both directions
        if len(soften_kernel) < 1:
            soften_kernel = [soften_kernel] * 3
        if depth2d:
            soften_kernel[2] = 1

        self.soften = nn.AvgPool3d(soften_kernel, (1, 1, 1), padding=[i//2 for i in soften_kernel])

    def forward(self, core, penu, clinical, factor):
        factor = factor.unsqueeze(2).unsqueeze(3).unsqueeze(4).unsqueeze(5)  # one additional dim for later when squeeze

        ##############################################################
        # Part 1: Commonly used separate core/penumbra representations
        core_rep = self.common_core(core)
        penu_rep = self.common_penu(penu)

        ##################################
        # Part 2: Bidirectional Recurrence
        offset1, lesion_pos1 = self.rnn1(core, penu, core_rep, penu_rep, clinical, factor)
        offset2, lesion_pos2 = self.rnn2(core, penu, core_rep, penu_rep, clinical, factor)

        ################################################
        # Part 3: Combine predictions of both directions
        offsets = [factor[:, i] * offset1[i] + (1 - factor[:, i]) * offset2[self.len - i - 1] for i in range(self.len)]
        if lesion_pos1 and lesion_pos2:
            lesion_pos = [factor[:, i].squeeze() * lesion_pos1[i].squeeze()
                          + (1 - factor[:, i]).squeeze() * lesion_pos2[self.len - i - 1].squeeze() for i in range(self.len)]
            lesion_pos = torch.stack(lesion_pos, dim=1)
        else:
            lesion_pos = None
        del offset1
        del offset2
        del lesion_pos1
        del lesion_pos2

        output_by_core = []
        output_by_penu = []
        grids_by_core = []
        grids_by_penu = []
        grids_core = []
        grids_penu = []

        for i in range(self.len):
            offsets[i] = self.soften(offsets[i].permute(0, 4, 1, 2, 3)).permute(0, 2, 3, 4, 1)
            grids_core.append(self.grid_identity + offsets[i][:, :, :, :, :3])
            grids_penu.append(self.grid_identity + offsets[i][:, :, :, :, 3:])

            pred_by_core = nn.functional.grid_sample(core, grids_core[-1])
            pred_by_penu = nn.functional.grid_sample(penu, grids_penu[-1])
            output_by_core.append(pred_by_core)
            output_by_penu.append(pred_by_penu)
            del pred_by_core
            del pred_by_penu

            grid_by_core = nn.functional.grid_sample(self.visual_grid, grids_core[-1])
            grid_by_penu = nn.functional.grid_sample(self.visual_grid, grids_penu[-1])
            grids_by_core.append(grid_by_core)
            grids_by_penu.append(grid_by_penu)
            del grid_by_core
            del grid_by_penu

        return torch.cat(output_by_core, dim=1), torch.cat(output_by_penu, dim=1), lesion_pos,\
               torch.cat(grids_by_core, dim=1), torch.cat(grids_by_penu, dim=1),\
               torch.stack(grids_core, dim=1), torch.stack(grids_penu, dim=1)
