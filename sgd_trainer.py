# -*- encoding: utf-8 -*-
'''
@File    :   trainer.py    
@Contact :   whut.hexin@foxmail.com
@License :   (C)Copyright 2017-2020, HeXin

@Modify Time      @Author    @Version    @Desciption
------------      -------    --------    -----------
2019/11/6 19:23   xin      1.0         None
'''

import torch
import torch.nn as nn
from torch import optim
from tqdm import tqdm
import numpy as np
import logging
from evaluate import eval_func, re_rank
from evaluate import euclidean_dist
from utils import AvgerageMeter, calculate_acc
import os.path as osp
import os
from common.sync_bn import convert_model
from common.optimizers import LRScheduler

from torch.optim import SGD
from utils.model import make_optimizer
try:
    from apex import amp
    from apex.parallel import DistributedDataParallel as DDP
    import apex
except:
    pass


class SGDTrainer(object):
    def __init__(self, cfg, model, train_dl, val_dl,
                 loss_func, num_query, num_gpus):
        self.cfg = cfg
        self.model = model
        self.train_dl = train_dl
        self.val_dl = val_dl
        self.loss_func = loss_func
        self.num_query = num_query

        self.loss_avg = AvgerageMeter()
        self.acc_avg = AvgerageMeter()
        self.train_epoch = 1
        self.batch_cnt = 0

        self.logger = logging.getLogger('reid_baseline.train')
        self.log_period = cfg.SOLVER.LOG_PERIOD
        self.checkpoint_period = cfg.SOLVER.CHECKPOINT_PERIOD
        self.eval_period = cfg.SOLVER.EVAL_PERIOD
        self.output_dir = cfg.OUTPUT_DIR
        self.device = cfg.MODEL.DEVICE
        self.epochs = cfg.SOLVER.MAX_EPOCHS

        if num_gpus > 1:
            # convert to use sync_bn
            self.logger.info('More than one gpu used, convert model to use SyncBN.')
            # Multi-GPU model without FP16
            self.model = nn.DataParallel(self.model)
            self.model = convert_model(self.model)
            self.model.cuda()
            self.logger.info('Using pytorch SyncBN implementation')

            self.scheduler = LRScheduler(base_lr=cfg.SOLVER.BASE_LR, step=cfg.SOLVER.STEPS,
                                         factor=cfg.SOLVER.GAMMA, warmup_epoch=cfg.SOLVER.WARMUP_EPOCH,
                                         warmup_begin_lr=cfg.SOLVER.WARMUP_BEGAIN_LR,
                                         warmup_mode=cfg.SOLVER.WARMUP_METHOD)
            lr = self.scheduler.update(0)
            self.optim = optim.SGD(self.model.parameters(), lr=lr, weight_decay=5e-4, momentum=0.9, nesterov=True)
            # self.optim = make_optimizer(self.model,opt=self.cfg.SOLVER.OPTIMIZER_NAME,lr=lr,weight_decay=self.cfg.SOLVER.WEIGHT_DECAY,momentum=0.9)

            self.mix_precision = False
            self.logger.info(self.optim)
            self.logger.info('Trainer Built')
            return

        else:
            # Single GPU model
            self.model.cuda()

            self.scheduler = LRScheduler(base_lr=cfg.SOLVER.BASE_LR, step=cfg.SOLVER.STEPS,
                                         factor=cfg.SOLVER.GAMMA, warmup_epoch=cfg.SOLVER.WARMUP_EPOCH,
                                         warmup_begin_lr=cfg.SOLVER.WARMUP_BEGAIN_LR,
                                         warmup_mode=cfg.SOLVER.WARMUP_METHOD)
            lr = self.scheduler.update(0)
            self.optim = optim.SGD(self.model.parameters(), lr=lr, weight_decay=5e-4, momentum=0.9, nesterov=True)
            # self.optim = make_optimizer(self.model,opt=self.cfg.SOLVER.OPTIMIZER_NAME,lr=lr,weight_decay=self.cfg.SOLVER.WEIGHT_DECAY,momentum=0.9)
            self.logger.info(self.optim)
            self.mix_precision = False
            return




    def handle_new_batch(self):
        self.batch_cnt += 1
        if self.batch_cnt % self.cfg.SOLVER.LOG_PERIOD == 0:
            self.logger.info('Epoch[{}] Iteration[{}/{}] Loss: {:.3f},'
                             'Acc: {:.3f}, Base Lr: {:.2e}'
                             .format(self.train_epoch, self.batch_cnt,
                                     len(self.train_dl), self.loss_avg.avg,
                                     self.acc_avg.avg, self.scheduler.learning_rate))

    def handle_new_epoch(self):

        self.batch_cnt = 1
        lr = self.scheduler.update(self.train_epoch)
        self.optim = optim.SGD(self.model.parameters(), lr=lr, weight_decay=5e-4, momentum=0.9, nesterov=True)
        # self.optim = make_optimizer(self.model,opt=self.cfg.SOLVER.OPTIMIZER_NAME,lr=lr,weight_decay=self.cfg.SOLVER.WEIGHT_DECAY,momentum=0.9)
        for param_group in self.optim.param_groups:
            param_group['lr'] = lr
        self.logger.info('Epoch {} done'.format(self.train_epoch))
        self.logger.info('-' * 20)

        torch.save(self.model.state_dict(), osp.join(self.output_dir,
                                                     self.cfg.MODEL.NAME + '_epoch_last.pth'))
        torch.save(self.optim.state_dict(), osp.join(self.output_dir,
                                                     self.cfg.MODEL.NAME + '_epoch_last_optim.pth'))

        if self.train_epoch > self.cfg.SOLVER.START_SAVE_EPOCH and self.train_epoch % self.checkpoint_period == 0:
            self.save()
        if (self.train_epoch > 0 and self.train_epoch % self.eval_period == 0) or self.train_epoch == 50 :
            self.evaluate()
            pass
        self.train_epoch += 1

    def step(self, batch):
        self.model.train()
        self.optim.zero_grad()
        img, target = batch
        img, target = img.cuda(), target.cuda()
        outputs = self.model(img)

        loss = self.loss_func(outputs, target)

        if self.mix_precision:
            with amp.scale_loss(loss, self.optim) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        self.optim.step()

        # acc = (score.max(1)[1] == target).float().mean()
        acc = calculate_acc(self.cfg, outputs, target)

        self.loss_avg.update(loss.cpu().item())
        self.acc_avg.update(acc.cpu().item())

        return self.loss_avg.avg, self.acc_avg.avg

    def evaluate(self):
        self.model.eval()
        num_query = self.num_query
        feats, pids, camids = [], [], []
        with torch.no_grad():
            for batch in tqdm(self.val_dl, total=len(self.val_dl),
                              leave=False):
                data, pid, camid, _ = batch
                data = data.cuda()

                # ff = torch.FloatTensor(data.size(0), 2048).zero_()
                # for i in range(2):
                #     if i == 1:
                #         data = data.index_select(3, torch.arange(data.size(3) - 1, -1, -1).long().to('cuda'))
                #     outputs = self.model(data)
                #     f = outputs.data.cpu()
                #     ff = ff + f

                ff = self.model(data).data.cpu()
                fnorm = torch.norm(ff, p=2, dim=1, keepdim=True)
                ff = ff.div(fnorm.expand_as(ff))

                feats.append(ff)
                pids.append(pid)
                camids.append(camid)
        feats = torch.cat(feats, dim=0)
        pids = torch.cat(pids, dim=0)
        camids = torch.cat(camids, dim=0)

        query_feat = feats[:num_query]
        query_pid = pids[:num_query]
        query_camid = camids[:num_query]

        gallery_feat = feats[num_query:]
        gallery_pid = pids[num_query:]
        gallery_camid = camids[num_query:]

        distmat = euclidean_dist(query_feat, gallery_feat)


        cmc, mAP, _ = eval_func(distmat.numpy(), query_pid.numpy(), gallery_pid.numpy(),
                                query_camid.numpy(), gallery_camid.numpy(),
                                )
        self.logger.info('Validation Result:')
        self.logger.info('mAP: {:.2%}'.format(mAP))
        for r in self.cfg.TEST.CMC:
            self.logger.info('CMC Rank-{}: {:.2%}'.format(r, cmc[r - 1]))
        
        self.logger.info('average of mAP and rank1: {:.2%}'.format((mAP+cmc[0])/2.0))
        

        self.logger.info('-' * 20)

    def save(self):
        torch.save(self.model.state_dict(), osp.join(self.output_dir,
                                                     self.cfg.MODEL.NAME + '_epoch' + str(self.train_epoch) + '.pth'))
        torch.save(self.optim.state_dict(), osp.join(self.output_dir,
                                                     self.cfg.MODEL.NAME + '_epoch' + str(
                                                         self.train_epoch) + '_optim.pth'))



