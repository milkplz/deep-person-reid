from __future__ import print_function
from __future__ import division

import os
import sys
import time
import datetime
import argparse
import os.path as osp
import numpy as np

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from torch.optim import lr_scheduler

import data_manager
from dataset_loader import ImageDataset
import transforms as T
import models
from losses import CrossEntropyLabelSmooth, DeepSupervision
from utils.iotools import save_checkpoint
from utils.avgmeter import AverageMeter
from utils.logger import Logger
from utils.torchtools import set_bn_to_eval, count_num_param
from eval_metrics import evaluate
from optimizers import init_optim
# from torchsummary import summary


parser = argparse.ArgumentParser(description='Train image model with cross entropy loss')
# Datasets
parser.add_argument('--root', type=str, default='data',
                    help="root path to data directory")
parser.add_argument('-d', '--dataset', type=str, default='market1501',
                    choices=data_manager.get_names())
parser.add_argument('-j', '--workers', default=4, type=int,
                    help="number of data loading workers (default: 4)")
parser.add_argument('--height', type=int, default=256,
                    help="height of an image (default: 256)")
parser.add_argument('--width', type=int, default=128,
                    help="width of an image (default: 128)")
parser.add_argument('--split-id', type=int, default=0,
                    help="split index")
parser.add_argument('--use-lmdb', action='store_true',
                    help="whether to use lmdb dataset")
# CUHK03-specific setting
parser.add_argument('--cuhk03-labeled', action='store_true',
                    help="whether to use labeled images, if false, detected images are used (default: False)")
parser.add_argument('--cuhk03-classic-split', action='store_true',
                    help="whether to use classic split by Li et al. CVPR'14 (default: False)")
parser.add_argument('--use-metric-cuhk03', action='store_true',
                    help="whether to use cuhk03-metric (default: False)")
# Optimization options
parser.add_argument('--optim', type=str, default='adam',
                    help="optimization algorithm (see optimizers.py)")
parser.add_argument('--max-epoch', default=60, type=int,
                    help="maximum epochs to run")
parser.add_argument('--start-epoch', default=0, type=int,
                    help="manual epoch number (useful on restarts)")
parser.add_argument('--train-batch', default=32, type=int,
                    help="train batch size")
parser.add_argument('--test-batch', default=100, type=int,
                    help="test batch size")
parser.add_argument('--lr', '--learning-rate', default=0.0003, type=float,
                    help="initial learning rate")
parser.add_argument('--stepsize', default=[20, 40], nargs='+', type=int,
                    help="stepsize to decay learning rate")
parser.add_argument('--gamma', default=0.1, type=float,
                    help="learning rate decay")
parser.add_argument('--weight-decay', default=5e-04, type=float,
                    help="weight decay (default: 5e-04)")
parser.add_argument('--fixbase-epoch', default=0, type=int,
                    help="epochs to fix base network (only train classifier, default: 0)")
parser.add_argument('--fixbase-lr', default=0.0003, type=float,
                    help="learning rate (when base network is frozen)")
parser.add_argument('--freeze-bn', action='store_true',
                    help="freeze running statistics in BatchNorm layers during training (default: False)")
# Architecture
parser.add_argument('-a', '--arch', type=str, default='resnet50', choices=models.get_names())
# Miscs
parser.add_argument('--print-freq', type=int, default=10,
                    help="print frequency")
parser.add_argument('--seed', type=int, default=1,
                    help="manual seed")
parser.add_argument('--resume', type=str, default='', metavar='PATH')
parser.add_argument('--load-weights', type=str, default='',
                    help="load pretrained weights but ignores layers that don't match in size")
parser.add_argument('--evaluate', action='store_true',
                    help="evaluation only")
parser.add_argument('--eval-step', type=int, default=-1,
                    help="run evaluation for every N epochs (set to -1 to test after training)")
parser.add_argument('--start-eval', type=int, default=0,
                    help="start to evaluate after specific epoch")
parser.add_argument('--save-dir', type=str, default='log')
parser.add_argument('--use-cpu', action='store_true',
                    help="use cpu")
parser.add_argument('--gpu-devices', default='0', type=str,
                    help='gpu device ids for CUDA_VISIBLE_DEVICES')

args = parser.parse_args()


def main():
    torch.manual_seed(args.seed)
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_devices
    use_gpu = torch.cuda.is_available()
    if args.use_cpu: use_gpu = False

    if not args.evaluate:
        sys.stdout = Logger(osp.join(args.save_dir, 'log_train.txt'))
    else:
        sys.stdout = Logger(osp.join(args.save_dir, 'log_test.txt'))
    print("==========\nArgs:{}\n==========".format(args))

    if use_gpu:
        print("Currently using GPU {}".format(args.gpu_devices))
        cudnn.benchmark = True
        torch.cuda.manual_seed_all(args.seed)
    else:
        print("Currently using CPU (GPU is highly recommended)")

    print("Initializing dataset {}".format(args.dataset))
    dataset = data_manager.init_imgreid_dataset(
        root=args.root, name=args.dataset, split_id=args.split_id,
        cuhk03_labeled=args.cuhk03_labeled, cuhk03_classic_split=args.cuhk03_classic_split,
        use_lmdb=args.use_lmdb,
    )

    transform_train = T.Compose([
        T.Random2DTranslation(args.height, args.width),
        # T.Resize((args.height, args.width)),
        T.RandomHorizontalFlip(),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    transform_test = T.Compose([
        T.Resize((args.height, args.width)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    pin_memory = True if use_gpu else False

    trainset = ImageDataset(dataset.train, transform=transform_train, use_lmdb=args.use_lmdb, lmdb_path=dataset.train_lmdb_path)
    trainloader = DataLoader(
        trainset,
        batch_size=args.train_batch, shuffle=True, num_workers=args.workers,
        pin_memory=pin_memory, drop_last=True,
    )

    queryset = ImageDataset(dataset.query, transform=transform_test, use_lmdb=args.use_lmdb, lmdb_path=dataset.query_lmdb_path)
    queryloader = DataLoader(
        queryset,
        batch_size=args.test_batch, shuffle=False, num_workers=args.workers,
        pin_memory=pin_memory, drop_last=False,
    )

    galleryset = ImageDataset(dataset.gallery, transform=transform_test, use_lmdb=args.use_lmdb, lmdb_path=dataset.gallery_lmdb_path)
    galleryloader = DataLoader(
        galleryset,
        batch_size=args.test_batch, shuffle=False, num_workers=args.workers,
        pin_memory=pin_memory, drop_last=False,
    )

    print("Initializing model: {}".format(args.arch))
    model = models.init_model(name=args.arch, num_classes=dataset.num_train_pids, loss={'xent'}, use_gpu=use_gpu)
    print("Model size: {:.3f} M".format(count_num_param(model)))
    # summary(model, (3, 160, 64))

    criterion = CrossEntropyLabelSmooth(num_classes=dataset.num_train_pids, use_gpu=use_gpu)
    optimizer = init_optim(args.optim, model.parameters(), args.lr, args.weight_decay)
    scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=args.stepsize, gamma=args.gamma)
    start_epoch = args.start_epoch

    if args.fixbase_epoch > 0:
        if hasattr(model, 'classifier') and isinstance(model.classifier, nn.Module):
            optimizer_tmp = init_optim(args.optim, model.classifier.parameters(), args.fixbase_lr, args.weight_decay)
        else:
            print("Warn: model has no attribute 'classifier' and fixbase_epoch is reset to 0")
            args.fixbase_epoch = 0

    if args.load_weights:
        # load pretrained weights but ignore layers that don't match in size
        print("Loading pretrained weights from '{}'".format(args.load_weights))
        if torch.cuda.is_available():
            checkpoint = torch.load(args.load_weights)
        else :
            checkpoint = torch.load(args.load_weights, map_location='cpu')
        pretrain_dict = checkpoint['state_dict']
        model_dict = model.state_dict()
        pretrain_dict = {k: v for k, v in pretrain_dict.items() if k in model_dict and model_dict[k].size() == v.size()}
        model_dict.update(pretrain_dict)
        model.load_state_dict(model_dict)

    if args.resume:
        checkpoint = torch.load(args.resume)
        model.load_state_dict(checkpoint['state_dict'])
        start_epoch = checkpoint['epoch']
        rank1 = checkpoint['rank1']
        print("Loaded checkpoint from '{}'".format(args.resume))
        print("- start_epoch: {}\n- rank1: {}".format(start_epoch, rank1))

    if use_gpu:
        model = nn.DataParallel(model).cuda()

    if args.evaluate:
        print("Evaluate only")
        test(model, queryloader, galleryloader, use_gpu)
        return

    start_time = time.time()
    train_time = 0
    best_rank1 = -np.inf
    best_epoch = 0
    print("==> Start training")

    if args.fixbase_epoch > 0:
        print("Train classifier for {} epochs while keeping base network frozen".format(args.fixbase_epoch))

        for epoch in range(args.fixbase_epoch):
            start_train_time = time.time()
            train(epoch, model, criterion, optimizer_tmp, trainloader, use_gpu, freeze_bn=True)
            train_time += round(time.time() - start_train_time)

        del optimizer_tmp
        print("Now open all layers for training")

    for epoch in range(start_epoch, args.max_epoch):
        start_train_time = time.time()
        train(epoch, model, criterion, optimizer, trainloader, use_gpu)
        train_time += round(time.time() - start_train_time)
        
        scheduler.step()
        
        if (epoch+1) > args.start_eval and args.eval_step > 0 and (epoch+1) % args.eval_step == 0 or (epoch+1) == args.max_epoch:
            print("==> Test")
            rank1 = test(model, queryloader, galleryloader, use_gpu)
            is_best = rank1 > best_rank1
            
            if is_best:
                best_rank1 = rank1
                best_epoch = epoch + 1
            
            if use_gpu:
                state_dict = model.module.state_dict()
            else:
                state_dict = model.state_dict()
            
            save_checkpoint({
                'state_dict': state_dict,
                'rank1': rank1,
                'epoch': epoch,
            }, is_best, osp.join(args.save_dir, 'checkpoint_ep' + str(epoch+1) + '.pth.tar'))
        
        
        '''
        if use_gpu:
            state_dict = model.module.state_dict()
        else:
            state_dict = model.state_dict()
            
        save_checkpoint({
            'state_dict': state_dict,
            'epoch': epoch,
        }, True, osp.join(args.save_dir, 'checkpoint_ep' + str(epoch+1) + '.pth.tar'))
        '''

    print("==> Best Rank-1 {:.1%}, achieved at epoch {}".format(best_rank1, best_epoch))

    elapsed = round(time.time() - start_time)
    elapsed = str(datetime.timedelta(seconds=elapsed))
    train_time = str(datetime.timedelta(seconds=train_time))
    print("Finished. Total elapsed time (h:m:s): {}. Training time (h:m:s): {}.".format(elapsed, train_time))


def train(epoch, model, criterion, optimizer, trainloader, use_gpu, freeze_bn=False):
    losses = AverageMeter()
    batch_time = AverageMeter()
    data_time = AverageMeter()

    model.train()

    if freeze_bn or args.freeze_bn:
        model.apply(set_bn_to_eval)

    end = time.time()
    for batch_idx, (imgs, pids, _) in enumerate(trainloader):
        data_time.update(time.time() - end)
        
        if use_gpu:
            imgs, pids = imgs.cuda(), pids.cuda()
        
        outputs = model(imgs)
        
        if isinstance(outputs, tuple):
            loss = DeepSupervision(criterion, outputs, pids)
        else:
            loss = criterion(outputs, pids)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        batch_time.update(time.time() - end)

        losses.update(loss.item(), pids.size(0))

        if (batch_idx+1) % args.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.4f} ({data_time.avg:.4f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'.format(
                   epoch+1, batch_idx+1, len(trainloader), batch_time=batch_time,
                   data_time=data_time, loss=losses))
        
        end = time.time()


def test(model, queryloader, galleryloader, use_gpu, ranks=[1, 5, 10, 20]):
    batch_time = AverageMeter()
    
    model.eval()

    with torch.no_grad():
        qf, q_pids, q_camids = [], [], []
        for batch_idx, (imgs, pids, camids) in enumerate(queryloader):
            if use_gpu: imgs = imgs.cuda()

            end = time.time()
            features = model(imgs)
            batch_time.update(time.time() - end)
            
            features = features.data.cpu()
            print('features', features.size(), features)
            qf.append(features)
            print('qf', len(qf))
            q_pids.extend(pids)
            q_camids.extend(camids)
        qf = torch.cat(qf, 0)
        q_pids = np.asarray(q_pids)
        q_camids = np.asarray(q_camids)

        print("Extracted features for query set, obtained {}-by-{} matrix".format(qf.size(0), qf.size(1)))

        gf, g_pids, g_camids = [], [], []
        end = time.time()
        for batch_idx, (imgs, pids, camids) in enumerate(galleryloader):
            if use_gpu: imgs = imgs.cuda()

            end = time.time()
            features = model(imgs)
            batch_time.update(time.time() - end)

            features = features.data.cpu()
            gf.append(features)
            g_pids.extend(pids)
            g_camids.extend(camids)
        gf = torch.cat(gf, 0)
        g_pids = np.asarray(g_pids)
        g_camids = np.asarray(g_camids)

        print("Extracted features for gallery set, obtained {}-by-{} matrix".format(gf.size(0), gf.size(1)))

    print("==> BatchTime(s)/BatchSize(img): {:.3f}/{}".format(batch_time.avg, args.test_batch))

    print('qf', qf.size(), qf)
    print('gf', gf.size(), gf)

    m, n = qf.size(0), gf.size(0)
    print('m,n',m,n)
    print('qf', qf.size(), qf)
    qf_pow = torch.pow(qf, 2)
    print('qf_pow', qf_pow.size(), qf_pow)
    qf_sum = qf_pow.sum(dim=1, keepdim=True)
    print('qf_sum', qf_sum.size(), qf_sum)
    qf_exp = qf_sum.expand(m, n)
    print('qf_exp', qf_exp.size(), qf_exp)

    print('gf', gf.size(), gf)
    gf_pow = torch.pow(gf, 2)
    print('gf_pow', gf_pow.size(), gf_pow)
    gf_sum = gf_pow.sum(dim=1, keepdim=True)
    print('gf_sum', gf_sum.size(), gf_sum)
    gf_exp = gf_sum.expand(n, m)
    print('gf_exp', gf_exp.size(), gf_exp)
    gf_t = gf_exp.t()
    print('gf_t', gf_t.size(), gf_t)
    # distmat = torch.pow(qf, 2).sum(dim=1, keepdim=True).expand(m, n) + \
    #           torch.pow(gf, 2).sum(dim=1, keepdim=True).expand(n, m).t()
    distmat = qf_exp + gf_t
    print('distmat', distmat.size(), distmat)

    # torch.from_numpy(nd)
    # print('mm', distmat.size, qf.size, gf.size, gf.t().size)
    # mm = np.dot(qf.numpy(), gf.numpy().T)
    # print('mm', mm.shape, distmat.shape, qf.shape, gf.t().shape)
    # distmat = distmat.numpy() - mm*(-2)
    distmat.addmm_(1, -2, qf, gf.t())
    print('distmat', distmat.shape, distmat)
    # distmat = distmat.numpy()

    print("Computing CMC and mAP")
    # cmc, mAP = evaluate(distmat, q_pids, g_pids, q_camids, g_camids, use_metric_cuhk03=args.use_metric_cuhk03)
    cmc, mAP = evaluate(distmat, q_pids, g_pids, q_camids, g_camids, dataset_type=args.dataset)

    print("Results ----------")
    print("mAP: {:.1%}".format(mAP))
    print("CMC curve")
    for r in ranks:
        print("Rank-{:<3}: {:.1%}".format(r, cmc[r-1]))
    print("------------------")

    return cmc[0]


if __name__ == '__main__':
    main()
