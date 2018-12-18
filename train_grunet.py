import torch
import torch.nn as nn
from common import data, metrics
from GRUnet import GRUnet
import matplotlib.pyplot as plt


class Criterion(nn.Module):
    def __init__(self, weights):
        super(Criterion, self).__init__()
        self.dc = metrics.BatchDiceLoss(weights)  # weighted inversely by each volume proportion

    def forward(self, pred, target):
        loss = self.dc(pred, target)

        '''
        for i in range(pred.size()[1]-1):
            diff = pred[:, i+1] - pred[:, i]
            loss += torch.mean(torch.abs(diff) - diff)  # monotone
        '''

        return loss


def get_title(prefix, idx, batch):
    suffix = ''
    if idx == int(batch[data.KEY_GLOBAL][row, 0, :, :, :]):
        suffix += ' core'
    elif idx == int(batch[data.KEY_GLOBAL][row, 1, :, :, :]):
        suffix += ' fuct'
    elif idx == 8:
        suffix += ' penu'
    return prefix + '[' + str(idx) + ']' + suffix


zsize = 1  # change here for 2D/3D: 1 or 28
input2d = (zsize == 1)
convgru_kernel = 3
if input2d:
    convgru_kernel = (1, 3, 3)
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
sequence_length = 9
num_input = 4
batchsize = 4
zslice = zsize // 2
pad = (20, 20, 20)
n_visual_samples = min(4, batchsize)

train_trafo = [data.UseLabelsAsImages(),
               data.HemisphericFlip(),
               data.ElasticDeform2D(apply_to_images=True, random=0.95),
               data.ToTensor()]
valid_trafo = [data.UseLabelsAsImages(),
               data.ElasticDeform2D(apply_to_images=True, random=0.67, seed=0),
               data.ToTensor()]

ds_train, ds_valid = data.get_toy_seq_shape_training_data(train_trafo, valid_trafo,
                                                          [0, 1, 2, 3],  #, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
                                                          [4, 5, 6, 7],  #[16, 17, 18, 19],
                                                          batchsize=batchsize, normalize=sequence_length, growth='fast',
                                                          zsize=zsize)

grunet = GRUnet(input_size=num_input, hidden_sizes=[10, 20, 30, 20, 10], kernel_sizes=[convgru_kernel] * 5, output_size=3).to(device)

params = [p for p in grunet.parameters() if p.requires_grad]
print('# optimizing params', sum([p.nelement() * p.requires_grad for p in params]),
      '/ total: GRUnet', sum([p.nelement() for p in grunet.parameters()]))

criterion = Criterion([0.3, 0.4, 0.3])
optimizer = torch.optim.Adam(params, lr=0.001)
scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=75, gamma=0.1)

loss_train = []
loss_valid = []

for epoch in range(0, 500):
    scheduler.step()
    f, axarr = plt.subplots(n_visual_samples * 2, sequence_length + 3)
    loss_mean = 0
    inc = 0

    ### Train ###

    is_train = True
    grunet.train(is_train)
    with torch.set_grad_enabled(is_train):

        for batch in ds_train:
            gt = batch[data.KEY_LABELS].to(device)

            mask = torch.zeros(gt.size()).byte()
            for b in range(batchsize):
                mask[b, int(batch[data.KEY_GLOBAL][b, 0, :, :, :]), :, :, :] = 1  # core
                mask[b, -1, :, :, :] = 1  # penumbra

            input = torch.ones(batchsize, num_input, zsize, 128 ,128)
            input[:, :2, :, :, :] = gt[mask].view(batchsize, 2, zsize, 128, 128)
            #input[:, 2:, :, :, :] = input[:, 2:, :, :, :] * batch[data.KEY_GLOBAL]
            input = input.to(device).requires_grad_()
            for b in range(batchsize):
                mask[b, int(batch[data.KEY_GLOBAL][b, 1, :, :, :]), :, :, :] = 1  # lesion

            _, output = grunet(input)

            loss = criterion(output,
                             gt[mask].view(batchsize, 3, zsize, 128, 128))
            loss_mean += loss.item()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            inc += 1

        loss_train.append(loss_mean/inc)

        for row in range(n_visual_samples):
            titles = []
            for i in range(sequence_length):
                axarr[row, i].imshow(gt.cpu().detach().numpy()[row, i, zslice, :, :], vmin=0, vmax=1, cmap='gray')
                titles.append(get_title('GT', i, batch))
            for i in range(3):
                axarr[row, i + sequence_length].imshow(output.cpu().detach().numpy()[row, i, zslice, :, :], vmin=0, vmax=1, cmap='gray')
                titles.append(get_title('Pr', i, batch))
            for ax, title in zip(axarr[row], titles):
                ax.set_title(title)
        del batch

    del output
    del input
    del loss
    del gt

    ### Validate ###

    inc = 0
    loss_mean = 0
    is_train = False
    optimizer.zero_grad()
    grunet.train(is_train)
    with torch.set_grad_enabled(is_train):

        for batch in ds_valid:
            gt = batch[data.KEY_LABELS].to(device)

            mask = torch.zeros(gt.size()).byte()
            for b in range(batchsize):
                mask[b, int(batch[data.KEY_GLOBAL][b, 0, :, :, :]), :, :, :] = 1  # core
                mask[b, -1, :, :, :] = 1  # penumbra

            input = torch.ones(batchsize, 4, zsize, 128 ,128)
            input[:, :2, :, :, :] = gt[mask].view(batchsize, 2, zsize, 128, 128)
            input[:, 2:, :, :, :] = input[:, 2:, :, :, :] * batch[data.KEY_GLOBAL]
            input = input.to(device).requires_grad_()
            for b in range(batchsize):
                mask[b, int(batch[data.KEY_GLOBAL][b, 1, :, :, :]), :, :, :] = 1  # lesion

            _, output = grunet(input)

            loss = criterion(output,
                             gt[mask].view(batchsize, 3, zsize, 128, 128))
            loss_mean += loss.item()

            inc += 1

        loss_valid.append(loss_mean/inc)

        for row in range(n_visual_samples):
            titles = []
            for i in range(sequence_length):
                axarr[row + n_visual_samples, i].imshow(gt.cpu().detach().numpy()[row, i, zslice, :, :], vmin=0, vmax=1, cmap='gray')
                titles.append(get_title('GT', i, batch))
            for i in range(3):
                axarr[row + n_visual_samples, i + sequence_length].imshow(output.cpu().detach().numpy()[row, i, zslice, :, :], vmin=0, vmax=1, cmap='gray')
                titles.append(get_title('Pr', i, batch))
            for ax, title in zip(axarr[row + n_visual_samples], titles):
                ax.set_title(title)
        del batch

    print('Epoch', epoch, 'last batch training loss:', loss_train[-1], '\tvalidation batch loss:', loss_valid[-1])

    if epoch % 5 == 0:
        torch.save(grunet, '/share/data_zoe1/lucas/NOT_IN_BACKUP/tmp/GRUnet.model')

    for ax in axarr.flatten():
        ax.title.set_fontsize(3)
        ax.xaxis.set_visible(False)
        ax.yaxis.set_visible(False)
    f.subplots_adjust(hspace=0.05)
    f.savefig('/share/data_zoe1/lucas/NOT_IN_BACKUP/tmp/GRUnet' + str(epoch) + '.png', bbox_inches='tight', dpi=300)

    del f
    del axarr

    if epoch > 0:
        fig, plot = plt.subplots()
        epochs = range(1, epoch + 2)
        plot.plot(epochs, loss_train, 'r-')
        plot.plot(epochs, loss_valid, 'b-')
        plot.set_ylabel('Loss Training (r) & Validation (b)')
        fig.savefig('/share/data_zoe1/lucas/NOT_IN_BACKUP/tmp/GRUnet_plots.png', bbox_inches='tight', dpi=300)
        del plot
        del fig