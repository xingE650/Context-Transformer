from __future__ import print_function
import sys
import os
import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn.init as init
import argparse
import torch.utils.data as data
from data import VOCroot, COCOroot, VOC_300, VOC_512, COCO_300, COCO_512, COCO_mobile_300, BaseTransform, preproc, EpisodicBatchSampler
from data.voc0712 import AnnotationTransform, VOCDetection, detection_collate
from layers.modules.multibox_loss_combined_imprinted import MultiBoxLoss_combined
from layers.functions import PriorBox
from utils.box_utils import match
import time
from data.voc0712_meta import VOC_CLASSES
from data.coco_voc_form import COCO_CLASSES
from logger import Logger
# torch.cuda.set_device(7)
# np.random.seed(100)

parser = argparse.ArgumentParser(
    description='Receptive Field Block Net Training')
parser.add_argument('-v', '--version', default='RFB_vgg',
                    help='RFB_vgg ,RFB_E_vgg or RFB_mobile version.')
parser.add_argument('-s', '--size', default='300',
                    help='300 or 512 input size.')
parser.add_argument('-d', '--dataset', default='VOC',
                    help='VOC or COCO dataset')
parser.add_argument(
    '--basenet', default='./weights/vgg16_reducedfc.pth', help='pretrained base model')
parser.add_argument('--jaccard_threshold', default=0.5,
                    type=float, help='Min Jaccard index for matching')
parser.add_argument('-b', '--batch_size', default=64,
                    type=int, help='Batch size for training')
parser.add_argument('--n_shot_task', type=int, default=5,
                    help="number of support examples per class on target domain")
parser.add_argument('--support_episodes', type=int, default=50,
                    help="number of center calculation per support image (default: 100)")
parser.add_argument('--train_episodes', type=int, default=100,
                    help="number of train episodes per epoch (default: 100)")
parser.add_argument('--num_workers', default=4,
                    type=int, help='Number of workers used in dataloading')
parser.add_argument('--cuda', default=True,
                    type=bool, help='Use cuda to train model')
parser.add_argument('--ngpu', default=1, type=int, help='gpus')
parser.add_argument('--lr', '--learning-rate',
                    default=4e-3, type=float, help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, help='momentum')
parser.add_argument(
    '--resume_net', default=None, help='resume net for retraining')
parser.add_argument('--resume_epoch', default=0,
                    type=int, help='resume iter for retraining')
parser.add_argument('-max', '--max_epoch', default=40,
                    type=int, help='max epoch for retraining')
parser.add_argument('--weight_decay', default=5e-4,
                    type=float, help='Weight decay for SGD')
parser.add_argument('--gamma', default=0.1,
                    type=float, help='Gamma update for SGD')
parser.add_argument('--log', default=False,
                    type=bool, help='Print the loss at each iteration')
parser.add_argument('--save_folder', default='./weights/',
                    help='Location to save checkpoint models')
args = parser.parse_args()

if not os.path.exists(args.save_folder):
    os.mkdir(args.save_folder)

if args.dataset == 'VOC':
    train_sets = [('2007', 'trainval'), ('2012', 'trainval')]
    cfg = (VOC_300, VOC_512)[args.size == '512']
else:
    # train_sets = [('2014', 'train'), ('2014', 'valminusminival')]
    train_sets = [('2014', 'trainval')]
    cfg = (COCO_300, COCO_512)[args.size == '512']

if args.version == 'RFB_vgg':
    from models.RFB_Net_vgg_imprinted import build_net
elif args.version == 'RFB_E_vgg':
    from models.RFB_Net_E_vgg import build_net
elif args.version == 'RFB_mobile':
    from models.RFB_Net_mobile import build_net
    cfg = COCO_mobile_300
else:
    print('Unknown version!')

img_dim = (300, 512)[args.size == '512']
rgb_means = ((104, 117, 123), (103.94, 116.78, 123.68))[args.version == 'RFB_mobile']
p = (0.6, 0.2)[args.version == 'RFB_mobile']
num_classes = 21
overlap_threshold = 0.5
feature_dim = 60
n_way = 20
num = args.batch_size

net = build_net('train', img_dim, feature_dim, overlap_threshold)
print(net)
if args.resume_net == None:
    base_weights = torch.load(args.basenet)
    print('Loading base network...')
    net.base.load_state_dict(base_weights)

    def xavier(param):
        init.xavier_uniform(param)

    def weights_init(m):
        for key in m.state_dict():
            if key.split('.')[-1] == 'weight':
                if 'conv' in key:
                    init.kaiming_normal_(m.state_dict()[key], mode='fan_out')
                if 'bn' in key:
                    m.state_dict()[key][...] = 1
            elif key.split('.')[-1] == 'bias':
                m.state_dict()[key][...] = 0

    print('Initializing weights...')
    # initialize newly added layers' weights with kaiming_normal method
    net.extras.apply(weights_init)
    net.loc.apply(weights_init)
    net.conf.apply(weights_init)
    net.obj.apply(weights_init)
    net.Norm.apply(weights_init)
    if args.version == 'RFB_E_vgg':
        net.reduce.apply(weights_init)
        net.up_reduce.apply(weights_init)

else:
    # load resume network
    print('Loading resume network...')
    state_dict = torch.load(args.resume_net)
    # create new OrderedDict that does not contain `module.`
    from collections import OrderedDict

    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        head = k[:7]
        if head == 'module.':
            name = k[7:]  # remove `module.`
        else:
            name = k
        new_state_dict[name] = v
    net.load_state_dict(new_state_dict, strict=False)

optimizer = optim.SGD([
                            {'params': net.base.parameters(), 'lr': 0.1*args.lr},
                            {'params': net.Norm.parameters(), 'lr': 0.5*args.lr},
                            {'params': net.extras.parameters(), 'lr': 0.5*args.lr},
                            {'params': net.loc.parameters()},
                            {'params': net.conf.parameters()},
                            {'params': net.obj.parameters()},
                            {'params': net.denselayer1.parameters()},
                            {'params': net.denselayer2.parameters()},
                            {'params': net.denselayer3.parameters()},
                            {'params': net.scale},
                        ], lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
# optimizer = optim.SGD(net.parameters(), lr=args.lr,
#                       momentum=args.momentum, weight_decay=args.weight_decay)
# optimizer = optim.RMSprop(net.parameters(), lr=args.lr,alpha = 0.9, eps=1e-08,
#                      momentum=args.momentum, weight_decay=args.weight_decay)
for group in optimizer.param_groups:
    group.setdefault('initial_lr', group['lr'])

if args.cuda:
    net.cuda()
    cudnn.benchmark = True

criterion = MultiBoxLoss_combined(num_classes-1, overlap_threshold, True, 0, True, 3, 0.5, False)

if args.log:
    logger = Logger(args.save_folder + 'logs')

priorbox = PriorBox(cfg)
with torch.no_grad():
    priors = priorbox.forward()
    if args.cuda:
        priors = priors.cuda()
num_priors = priors.size(0)

def composite(bn, norm, fc=None):
    def function(*inputs):
        concated_features = torch.cat(inputs, 1)
        if fc is not None:
            output = fc(norm(bn(concated_features)))
        else:
            output = norm(bn(concated_features))
        return output

    return function

def train(net):
    net.train()
    for param in net.parameters():
        param.requires_grad = False

    print('Loading Dataset...')
    if args.dataset == 'VOC':
        # dataset_init = VOCDetection(VOCroot, train_sets, preproc(img_dim, rgb_means, p),
        #                        VOC_AnnotationTransform(), n_shot, 0,
        #                        phase='test_support', n_shot_task=args.n_shot_task)
        dataset = VOCDetection(VOCroot, train_sets, preproc(
            img_dim, rgb_means, p), AnnotationTransform(), n_shot_task=args.n_shot_task)
    else:
        print('Only VOC is supported now!')
        return

    bn1 = net.denselayer1.bn
    bn2 = net.denselayer2.bn
    bn3 = net.denselayer3.bn
    norm = net.denselayer3.norm
    fc1 = net.denselayer1.fc
    fc2 = net.denselayer2.fc
    for item in (bn1, bn2, bn3):
        for key in item.state_dict():
            if 'weight' in key:
                item.state_dict()[key][...] = 1

    for i in range(3):
        print('Initializing the ' + ('first', 'second', 'third')[i] + ' layer...')
        sampler = EpisodicBatchSampler(n_classes=len(dataset), n_way=args.batch_size,
                                       n_episodes=args.support_episodes, phase='train')
        batch_iterator = iter(data.DataLoader(dataset, batch_sampler=sampler, num_workers=args.num_workers,
                                              collate_fn=detection_collate))

        if args.cuda:
            way_list = [torch.empty(0).cuda() for _ in range(n_way)]
        else:
            way_list = [torch.empty(0) for _ in range(n_way)]

        for _ in range(args.support_episodes):
            # load support data
            # for i in range(10):
            images, targets = next(batch_iterator)
            # vis_picture(images, s_t)

            if args.cuda:
                images = images.cuda()
                targets = [anno.cuda() for anno in targets]
            else:
                targets = [anno for anno in targets]

            out = net(images, 'init')

            _, conf_data, _ = out

            if args.cuda:
                loc_t = torch.Tensor(num, num_priors, 4).cuda()
                conf_t = torch.CharTensor(num, num_priors).cuda()
                obj_t = torch.ByteTensor(num, num_priors).cuda()
            else:
                loc_t = torch.Tensor(num, num_priors, 4)
                conf_t = torch.CharTensor(num, num_priors)
                obj_t = torch.ByteTensor(num, num_priors)

            # match priors with gt
            for idx in range(num):  # batch_size
                truths = targets[idx][:, :-1].data  # [obj_num, 4]
                labels = targets[idx][:, -1].data  # [obj_num]
                defaults = priors.data  # [num_priors,4]
                match(overlap_threshold, truths, defaults, [0.1, 0.2], labels, loc_t, conf_t, obj_t, idx)

            cls_idx = conf_t[obj_t.byte()]
            features = [conf_data[obj_t.byte()].view(-1, feature_dim)]
            if i == 0:
                layer1 = composite(bn1, norm)
                new_features = layer1(*features)
                way_list = [torch.cat((way_list[i], new_features[cls_idx==i+1].view(-1, 60)), 0) for i in range(n_way)]
            elif i == 1:
                layer1 = composite(bn1, norm, fc1)
                layer2 = composite(bn2, norm)
                new_features = layer1(*features)
                features.append(new_features)
                new_features = layer2(*features)
                way_list = [torch.cat((way_list[i], new_features[cls_idx==i+1].view(-1, 80)), 0) for i in range(n_way)]
            else:
                layer1 = composite(bn1, norm, fc1)
                layer2 = composite(bn2, norm, fc2)
                layer3 = composite(bn3, norm)
                new_features = layer1(*features)
                features.append(new_features)
                new_features = layer2(*features)
                features.append(new_features)
                new_features = layer3(*features)
                way_list = [torch.cat((way_list[i], new_features[cls_idx == i + 1].view(-1, 100)), 0) for i in range(n_way)]
        way_list = [item.mean(0) for item in way_list]
        if i == 0:
            net.denselayer1.fc.weight.data = torch.stack([item / torch.norm(item) for item in way_list], 0)  # [20, 60]
        elif i == 1:
            net.denselayer2.fc.weight.data = torch.stack([item / torch.norm(item) for item in way_list], 0)  # [20, 80]
        else:
            net.denselayer3.fc.weight.data = torch.stack([item / torch.norm(item) for item in way_list], 0)  # [20, 100]

    print('Fine tuning on ' + str(args.n_shot_task) + 'shot task')
    for param in net.parameters():
        param.requires_grad = True

    if args.ngpu > 1:
        net = torch.nn.DataParallel(net, device_ids=list(range(args.ngpu)), output_device=0)

    net.train()
    epoch = 0 + args.resume_epoch
    epoch_size = args.train_episodes
    max_iter = args.max_epoch * epoch_size

    milestones_VOC = [30, 35]
    milestones_COCO = [30, 60, 90]
    milestones = (milestones_VOC, milestones_COCO)[args.dataset == 'COCO']

    scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones, gamma=args.gamma, last_epoch=epoch - 1)

    if args.resume_epoch > 0:
        start_iter = args.resume_epoch * epoch_size
        t0 = time.time()
    else:
        start_iter = 0

    first_or_not = 1

    sampler = EpisodicBatchSampler(n_classes=len(dataset), n_way=args.batch_size,
                                   n_episodes=args.train_episodes, phase='train')

    for iteration in range(start_iter, max_iter):
        if iteration % epoch_size == 0:
            # create batch iterator
            batch_iterator = iter(data.DataLoader(dataset, batch_sampler=sampler, num_workers=args.num_workers,
                                                  collate_fn=detection_collate))
            if not first_or_not:
                print('Epoch' + repr(epoch) + ' Finished! || L: %.4f C: %.4f O: %.4f' % (
                    loc_loss / epoch_size, conf_loss / epoch_size, obj_loss / epoch_size)
                      )
                if epoch % 2 == 0:
                    torch.save(net.state_dict(), args.save_folder + args.version + '_' + args.dataset + '_imprinted_epoches_' +
                               repr(epoch) + '.pth')
            loc_loss = 0
            conf_loss = 0
            obj_loss = 0

            epoch += 1
            scheduler.step()  # 等价于lr = args.lr * (gamma ** (step_index))
            lr = scheduler.get_lr()

        # load train data
        images, targets = next(batch_iterator)  # [n_way, n_shot, 3, im_size, im_size]

        # vis_picture(images, targets)

        if args.cuda:
            images = images.cuda()
            targets = [anno.cuda() for anno in targets]
        else:
            targets = [anno for anno in targets]

        # forward
        out = net(images)

        # backprop
        optimizer.zero_grad()
        loss_l, loss_c, loss_obj = criterion(out, priors, targets)
        loss = loss_l + loss_c + loss_obj
        loss.backward()
        optimizer.step()
        if args.ngpu > 1:
            net.module.normalize()
        else:
            net.normalize()
        loc_loss += loss_l.item()
        conf_loss += loss_c.item()
        obj_loss += loss_obj.item()

        if iteration % 10 == 0:
            if not first_or_not:
                t1 = time.time()
                print('Epoch:' + repr(epoch) + ' || epochiter: ' + repr(iteration % epoch_size) + '/' + repr(epoch_size)
                      + ' || Totel iter ' +
                      repr(iteration) + ' || L: %.4f C: %.4f O: %.4f ||' % (
                          loss_l.item(), loss_c.item(), loss_obj.item()) +
                      ' Time: %.4f sec. ||' % (t1 - t0) + ' LR: %.8f, %.8f' % (lr[0], lr[3]))
                if args.log:
                    logger.scalar_summary('loc_loss', loss_l.item(), iteration)
                    logger.scalar_summary('conf_loss', loss_c.item(), iteration)
                    logger.scalar_summary('obj_loss', loss_obj.item(), iteration)
                    logger.scalar_summary('lr', max(lr), iteration)
            t0 = time.time()

        first_or_not = 0
    torch.save(net.state_dict(), args.save_folder +
               'Final_' + args.version + '_' + args.dataset + '_imprinted.pth')


def vis_picture(imgs, targets):
    """
    Args:
        imgs: (tensor) Image to show
            Shape: [n_way, n_shot, 3, image_size, image_size]
        targets: (list) bounding boxes
            Shape: each way is a list, each shot is a tensor, shape of the tensor[num_boxes, 5]
    """
    import numpy as np
    import matplotlib.pyplot as plt
    import cv2
    np_img = imgs.cpu().numpy()
    targets = [anno.cpu().numpy() for anno in targets]
    num = imgs.shape[0]
    imgs = np.transpose(np_img, (0, 2, 3, 1))
    imgs = (imgs + np.array([104, 117, 123])) / 255 # RGB
    imgs = imgs[:, :, :, ::-1] # BGR

    for i in range(num):
        img = imgs[i, :, :, :].copy()
        labels = targets[i][:, -1]
        boxes = targets[i][:, :4]
        boxes = (boxes * 300).astype(np.uint16)
        for k in range(boxes.shape[0]):
            cv2.rectangle(img, (boxes[k, 0], boxes[k, 1]), (boxes[k, 2], boxes[k, 3]), (0, 1, 0))
        plt.imshow(img)
        plt.show()

def vis_picture_1(imgs, targets):
    import numpy as np
    import matplotlib.pyplot as plt
    import cv2
    npimg = imgs.cpu().numpy()
    targets = [[anno.cpu().numpy() for anno in cls_list] for cls_list in targets]
    n_way = npimg.shape[0]
    per_way = npimg.shape[1]
    imgs = np.transpose(npimg, (0, 1, 3, 4, 2))
    imgs = (imgs + np.array([104, 117, 123])) / 255 # RGB
    imgs = imgs[:, :, :, :, ::-1] # BGR

    for i in range(20):
        CLASSES = (VOC_CLASSES, COCO_CLASSES)[n_way == 60]
        cls = CLASSES[int(targets[i][0][-1, -1])]
        for j in range(per_way):
            fig = plt.figure()
            fig.suptitle(cls)
            # ax = fig.add_subplot(per_way, 1, j+1)
            img = imgs[i, j, :, :, :].copy()
            labels = targets[i][j][:, -1]
            boxes = targets[i][j][:, :4]
            boxes = (boxes * 300).astype(np.uint16)
            for k in range(boxes.shape[0]):
                cv2.rectangle(img, (boxes[k, 0], boxes[k, 1]), (boxes[k, 2], boxes[k, 3]), (1, 0, 0))
            # cls = COCO_CLASSES[int(labels[0])]
            plt.imshow(img)
            plt.show()


def adjust_learning_rate(optimizer, iteration, epoch_size):
    """Sets the learning rate
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    lr = 1e-6 + (args.lr - 1e-6) * iteration / (epoch_size * 5)  # 前5个epoch有warm up的过程
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


if __name__ == '__main__':
    train(net)
