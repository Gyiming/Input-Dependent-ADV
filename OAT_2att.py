import os, argparse, time
import numpy as np
import matplotlib.pyplot as plt

import torch
import torch.nn.functional as F
from torch.optim import SGD, Adam, lr_scheduler

from models.cifar10.resnet_OAT import ResNet34OAT
from models.svhn.wide_resnet_OAT import WRN16_8OAT
from models.stl10.wide_resnet_OAT import WRN40_2OAT

from dataloaders.cifar10 import cifar10_dataloaders
from dataloaders.svhn import svhn_dataloaders
from dataloaders.stl10 import stl10_dataloaders

from utils.utils import *
from utils.context import ctx_noparamgrad_and_eval
from utils.sample_lambda import element_wise_sample_lambda, batch_wise_sample_lambda
from attacks.pgd import PGD

from skimage import io
from torchvision import transforms
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, utils

import pdb
import os
import PIL
from PIL import Image
import gzip
import pickle

parser = argparse.ArgumentParser(description='cifar10 Training')
parser.add_argument('--gpu', default='0')
parser.add_argument('--cpus', default=4)
# dataset:
parser.add_argument('--dataset', '--ds', default='cifar10', choices=['cifar10', 'svhn', 'stl10'], help='which dataset to use')
# optimization parameters:
parser.add_argument('--batch_size', '-b', default=128, type=int, help='mini-batch size')
parser.add_argument('--epochs', '-e', default=200, type=int, help='number of total epochs to run')
parser.add_argument('--decay_epochs', '--de', default=[50,150], nargs='+', type=int, help='milestones for multisteps lr decay')
parser.add_argument('--opt', default='sgd', choices=['sgd', 'adam'], help='which optimizer to use')
parser.add_argument('--decay', default='cos', choices=['cos', 'multisteps'], help='which lr decay method to use')
parser.add_argument('--lr', default=0.1, type=float, help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, help='momentum')
parser.add_argument('--wd', default=5e-4, type=float, help='weight decay')
# adv parameters:
parser.add_argument('--targeted', action='store_true', help='If true, targeted attack')
parser.add_argument('--eps', type=int, default=8)
parser.add_argument('--steps', type=int, default=7)
# OAT parameters:
parser.add_argument('--distribution', default='disc', choices=['disc'], help='Lambda distribution')
parser.add_argument('--lambda_choices', default=[0.0,0.1,0.2,0.3,0.4,1.0], nargs='*', type=float, help='possible lambda values to sample during training')
parser.add_argument('--probs', default=-1, type=float, help='the probs for sample 0, if not uniform sample')
parser.add_argument('--encoding', default='rand', choices=['none', 'onehot', 'dct', 'rand'], help='encoding scheme for Lambda')
parser.add_argument('--dim', default=128, type=int, help='encoding dimention for Lambda')
parser.add_argument('--use2BN', action='store_true', help='If true, use dual BN')
parser.add_argument('--sampling', default='ew', choices=['ew', 'bw'], help='sampling scheme for Lambda')
# others:
parser.add_argument('--resume', action='store_true', help='If true, resume from early stopped ckpt')
args = parser.parse_args()
args.efficient = True
print(args)

os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
torch.backends.cudnn.benchmark = True
args.use2BN = True
# data loader:
if args.dataset == 'cifar10':
    train_loader, val_loader, _ = cifar10_dataloaders(train_batch_size=args.batch_size, num_workers=args.cpus)
elif args.dataset == 'svhn':
    train_loader, val_loader, _ = svhn_dataloaders(train_batch_size=args.batch_size, num_workers=args.cpus)
elif args.dataset == 'stl10':
    train_loader, val_loader = stl10_dataloaders(train_batch_size=args.batch_size, num_workers=args.cpus)

# model:
if args.encoding in ['onehot', 'dct', 'rand']:
    FiLM_in_channels = args.dim
else: # non encoding
    FiLM_in_channels = 1
if args.dataset == 'cifar10':
    model_fn = ResNet34OAT
elif args.dataset == 'svhn':
    model_fn = WRN16_8OAT
elif args.dataset == 'stl10':
    model_fn = WRN40_2OAT
model = model_fn(use2BN=True, FiLM_in_channels=FiLM_in_channels).cuda()
model = torch.nn.DataParallel(model)

model_str = os.path.join(model_fn.__name__)
if args.use2BN:
    model_str += '-2BN'
if args.opt == 'sgd':
    opt_str = 'e%d-b%d_sgd-lr%s-m%s-wd%s' % (args.epochs, args.batch_size, args.lr, args.momentum, args.wd)
elif args.opt == 'adam':
    opt_str = 'e%d-b%d_adam-lr%s-wd%s' % (args.epochs, args.batch_size, args.lr, args.wd)
if args.decay == 'cos':
    decay_str = 'cos'
elif args.decay == 'multisteps':
    decay_str = 'multisteps-%s' % args.decay_epochs
attack_str = 'targeted' if args.targeted else 'untargeted' + '-pgd-%s-%d' % (args.eps, args.steps)
lambda_str = '%s-%s-%s' % (args.distribution, args.sampling, args.lambda_choices)
if args.probs > 0:
    lambda_str += '-%s' % args.probs
if args.encoding in ['onehot', 'dct', 'rand']:
    lambda_str += '-%s-d%s' % (args.encoding, args.dim)
save_folder = os.path.join('./2attack', 'cifar10', model_str, '%s_%s_%s_%s' % (attack_str, opt_str, decay_str, lambda_str))
print(save_folder)
create_dir(save_folder)

if args.encoding == 'onehot':
    I_mat = np.eye(args.dim)
    encoding_mat = I_mat
elif args.encoding == 'dct':
    from scipy.fftpack import dct
    dct_mat = dct(np.eye(args.dim), axis=0)
    encoding_mat = dct_mat
elif args.encoding == 'rand':
    rand_mat = np.random.randn(args.dim, args.dim)
    np.save(os.path.join(save_folder, 'rand_mat.npy'), rand_mat)
    rand_otho_mat, _ = np.linalg.qr(rand_mat)
    np.save(os.path.join(save_folder, 'rand_otho_mat.npy'), rand_otho_mat)
    encoding_mat = rand_otho_mat
elif args.encoding == 'none':
    encoding_mat = None

#pdb.set_trace()
# optimizer:
if args.opt == 'sgd':
    optimizer = SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.wd)
elif args.opt == 'adam':
    optimizer = Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)
if args.decay == 'cos':
    scheduler = lr_scheduler.CosineAnnealingLR(optimizer, args.epochs)
elif args.decay == 'multisteps':
    scheduler = lr_scheduler.MultiStepLR(optimizer, args.decay_epochs, gamma=0.1)

# load ckpt:
if args.resume:
    last_epoch, best_TA, best_ATA, training_loss, val_TA, val_ATA \
         = load_ckpt(model, optimizer, scheduler, os.path.join(save_folder, 'latest.pth'))
    start_epoch = last_epoch + 1
else:
    start_epoch = 0
    best_TA, best_ATA1, best_ATA2 = 0, 0, 0
    # training curve lists:
    training_loss, val_TA, val_ATA1, val_ATA2 = [], [], [], []

# attacker:
attacker = PGD(eps=args.eps/255, steps = args.steps, use_FiLM=True)
attacker2 = PGD(eps=(args.eps+16)/255, steps = args.steps, use_FiLM=True)

vector_normal = torch.from_numpy(encoding_mat[0]).unsqueeze(0).float().cuda()
vector_att1 = torch.from_numpy(encoding_mat[1]).unsqueeze(0).float().cuda()
vector_att2 = torch.from_numpy(encoding_mat[2]).unsqueeze(0).float().cuda()
## training:
for epoch in range(start_epoch, args.epochs):
    train_fp = open(os.path.join(save_folder, 'train_log.txt'), 'a+')
    val_fp = open(os.path.join(save_folder, 'val_log.txt'), 'a+')
    start_time = time.time()
    
    ## training:
    model.train()
    requires_grad_(model, True)
    accs, accs_adv1, accs_adv2, losses = AverageMeter(), AverageMeter(), AverageMeter(), AverageMeter()
    for i, (imgs, labels) in enumerate(train_loader):
        imgs, labels = imgs.cuda(), labels.cuda()

        #pdb.set_trace()
        with ctx_noparamgrad_and_eval(model):
            imgs_adv1 = attacker.attack(model, imgs, labels=labels, _lambda=vector_att1.repeat(imgs.size()[0],1,1), idx2BN=0)
            imgs_adv2 = attacker2.attack(model, imgs, labels=labels, _lambda=vector_att2.repeat(imgs.size()[0],1,1), idx2BN=0)
        logits_adv1 = model(imgs_adv1.detach(), vector_att1.repeat(imgs_adv1.size()[0],1,1), idx2BN = 0)
        logits_adv2 = model(imgs_adv2.detach(), vector_att2.repeat(imgs_adv2.size()[0],1,1), idx2BN = 0)
        # logits for clean imgs:
        logits = model(imgs,vector_normal.repeat(imgs.size()[0],1,1), idx2BN = imgs.size()[0])

        # loss and update:
        loss = F.cross_entropy(logits, labels)
        
        #loss = (1-args.Lambda) * loss + args.Lambda * F.cross_entropy(logits_adv, labels)
        loss = loss + 1.2 * F.cross_entropy(logits_adv1, labels) + 1.4 * F.cross_entropy(logits_adv2, labels)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # proximal gradient for channel pruning:
        current_lr = scheduler.get_lr()[0]

        # metrics:
        accs.append((logits.argmax(1) == labels).float().mean().item())
        accs_adv1.append((logits_adv1.argmax(1) == labels).float().mean().item())
        accs_adv2.append((logits_adv2.argmax(1) == labels).float().mean().item())
        losses.append(loss.item())

        if i % 50 == 0:
            train_str = 'Epoch %d-%d | Train | Loss: %.4f, SA: %.4f' % (epoch, i, losses.avg, accs.avg)
            train_str += ', RA1: %.4f, RA2: %.4f' % (accs_adv1.avg, accs_adv2.avg)
            print(train_str)
        train_fp.write(train_str + '\n')
    # lr schedualr update at the end of each epoch:
    scheduler.step()


    ## validation:
    model.eval()
    requires_grad_(model, False)
    print(model.training)

    if args.dataset == 'cifar10':
        eval_this_epoch = (epoch % 10 == 0) or (epoch>=int(0.7*args.epochs)) # boolean
    elif args.dataset == 'svhn':
        eval_this_epoch = (epoch % 10 == 0) or (epoch>=int(0.25*args.epochs)) # boolean

    if eval_this_epoch:
        val_accs, val_accs_adv1, val_accs_adv2 = AverageMeter(), AverageMeter(), AverageMeter()
        for i, (imgs, labels) in enumerate(val_loader):
            imgs, labels = imgs.cuda(), labels.cuda()
            #pdb.set_trace()
            # generate adversarial images:
            with ctx_noparamgrad_and_eval(model):
                imgs_adv1 = attacker.attack(model, imgs, labels=labels, _lambda=vector_att1.repeat(imgs.size()[0],1,1), idx2BN=0)
                imgs_adv2 = attacker2.attack(model, imgs, labels=labels, _lambda=vector_att2.repeat(imgs.size()[0],1,1), idx2BN=0)
            linf_norms1 = (imgs_adv1 - imgs).view(imgs.size()[0], -1).norm(p=np.Inf, dim=1)
            linf_norms2 = (imgs_adv2 - imgs).view(imgs.size()[0], -1).norm(p=np.Inf, dim=1)
            logits_adv1 = model(imgs_adv1.detach(), vector_att1.repeat(imgs_adv1.size()[0],1,1), idx2BN = 0)
            logits_adv2 = model(imgs_adv2.detach(), vector_att2.repeat(imgs_adv2.size()[0],1,1), idx2BN = 0)
            # logits for clean imgs:
            logits = model(imgs,vector_normal.repeat(imgs.size()[0],1,1), idx2BN = imgs.size()[0])

            val_accs.append((logits.argmax(1) == labels).float().mean().item())
            val_accs_adv1.append((logits_adv1.argmax(1) == labels).float().mean().item())
            val_accs_adv2.append((logits_adv2.argmax(1) == labels).float().mean().item())

        val_str = 'Epoch %d | Validation | Time: %.4f | lr: %s | SA: %.4f, RA1: %.4f, RA2: %.4f, linf1: %.4f - %.4f, linf2: %.4f - %.4f' % (
            epoch, (time.time()-start_time), current_lr, val_accs.avg, val_accs_adv1.avg, val_accs_adv2.avg,  
            torch.min(linf_norms1).data, torch.max(linf_norms1).data,torch.min(linf_norms2).data, torch.max(linf_norms2).data)
        print(val_str)
        val_fp.write(val_str + '\n')

    # save loss curve:
    training_loss.append(losses.avg)
    plt.plot(training_loss)
    plt.grid(True)
    plt.savefig(os.path.join(save_folder, 'training_loss.png'))
    plt.close()

    if eval_this_epoch:
        val_TA.append(val_accs.avg) 
        plt.plot(val_TA, 'r')
        val_ATA1.append(val_accs_adv1.avg)
        plt.plot(val_ATA1, 'g')
        val_ATA2.append(val_accs_adv2.avg)
        plt.plot(val_ATA2, 'b')
        plt.grid(True)
        plt.savefig(os.path.join(save_folder, 'val_acc.png'))
        plt.close()
    else:
        val_TA.append(val_TA[-1]) 
        plt.plot(val_TA, 'r')
        val_ATA1.append(val_ATA1[-1])
        plt.plot(val_ATA1, 'g')
        val_ATA2.append(val_ATA2[-1])
        plt.plot(val_ATA2, 'b')
        plt.grid(True)
        plt.savefig(os.path.join(save_folder, 'val_acc.png'))
        plt.close()

    # save pth:
    if eval_this_epoch:
        if val_accs.avg >= best_TA:
            best_TA = val_accs.avg
            torch.save(model.state_dict(), os.path.join(save_folder, 'best_SA.pth'))
        if val_accs_adv1.avg >= best_ATA1:
            best_ATA1 = val_accs_adv1.avg
            torch.save(model.state_dict(), os.path.join(save_folder, 'best_RA1.pth'))
        if val_accs_adv2.avg >= best_ATA2:
            best_ATA2 = val_accs_adv2.avg
            torch.save(model.state_dict(), os.path.join(save_folder, 'best_RA2.pth'))
    save_ckpt(epoch, model, optimizer, scheduler, best_TA, best_ATA1, training_loss, val_TA, val_ATA1,  
        os.path.join(save_folder, 'latest.pth'))