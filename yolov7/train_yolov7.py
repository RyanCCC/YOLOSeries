import datetime
import os

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

from .nets.yolov7 import YoloBody
from .nets.loss import ModelEMA, YOLOLoss, get_lr_scheduler, set_optimizer_lr, weights_init
from .lib.callbacks import LossHistory
from .lib.dataloader import YoloDataset, yolo_dataset_collate
from .lib.tools import get_anchors, get_classes, show_config
from .lib.tools import fit_one_epoch


def yolov7(config):
    # 设置日志级别
    train_gpu = config.gpus
    Cuda = config.cuda
    distributed = config.distributed
    sync_bn = config.sync_bn
    fp16 = config.fp16
    classes_path = config.classes_path
    anchors_path = config.anchor_path
    anchors_mask = config.ANCHOR_MASK
    model_path  = config.pretrain_weight
    input_shape = config.input_shape
    phi  = config.phi
    mosaic = True
    mosaic_prob = 0.5
    mixup  = True
    mixup_prob = 0.5
    special_aug_ratio = 0.7
    label_smoothing = config.label_smoothing
    Init_Epoch = config.Init_epoch
    Freeze_Epoch  = config.Freeze_epoch
    Freeze_batch_size = config.batch_size
    UnFreeze_Epoch = config.epoch
    Unfreeze_batch_size = 4
    Freeze_Train = config.Freeze_Train
    save_period = 1
    Init_lr = config.learning_rate
    Min_lr = Init_lr * 0.01
    optimizer_type = "sgd"
    momentum = 0.937
    weight_decay = 5e-4
    #lr_decay_type   option:['step'、'cos']
    lr_decay_type = 'cos'
    save_dir = config.logdir
    train_annotation_path = config.train_txt
    val_annotation_path = config.val_txt
    os.environ["CUDA_VISIBLE_DEVICES"]  = train_gpu
    pretrained = False
    _, num_classes = get_classes(classes_path)
    anchors, _ = get_anchors(anchors_path)

    # 设置显卡信息
    ngpus_per_node = torch.cuda.device_count()
    if distributed:
        dist.init_process_group(backend="nccl")
        local_rank = int(os.environ["LOCAL_RANK"])
        rank = int(os.environ["RANK"])
        device = torch.device("cuda", local_rank)
        if local_rank == 0:
            print(f"[{os.getpid()}] (rank = {rank}, local_rank = {local_rank}) training...")
            print("Gpu Device Count : ", ngpus_per_node)
    else:
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        local_rank = 0
        rank = 0

    # init model
    model = YoloBody(anchors_mask, num_classes, phi, pretrained=pretrained)
    # load pretrain weights
    if not pretrained:
        weights_init(model)
    if model_path != '':
        if local_rank == 0:
            print('Load weights {}.'.format(model_path))
        model_dict = model.state_dict()
        pretrained_dict = torch.load(model_path, map_location = device)
        load_key, no_load_key, temp_dict = [], [], {}
        for k, v in pretrained_dict.items():
            if k in model_dict.keys() and np.shape(model_dict[k]) == np.shape(v):
                temp_dict[k] = v
                load_key.append(k)
            else:
                no_load_key.append(k)
        model_dict.update(temp_dict)
        model.load_state_dict(model_dict)
    
    # 设置损失函数
    yolo_loss = YOLOLoss(anchors, num_classes, input_shape, anchors_mask, label_smoothing)
    if local_rank == 0:
        log_dir = os.path.join(save_dir)
        loss_history = LossHistory(log_dir, model, input_shape=input_shape)
    else:
        loss_history = None
    
    # fp16
    if fp16:
        from torch.cuda.amp import GradScaler as GradScaler
        scaler = GradScaler()
    else:
        scaler = None

    model_train = model.train()

    if sync_bn and ngpus_per_node > 1 and distributed:
        model_train = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model_train)
    elif sync_bn:
        print("Sync_bn is not support in one gpu or not distributed.")
    if Cuda:
        if distributed:
            # 多卡平台运行
            model_train = model_train.cuda(local_rank)
            model_train = torch.nn.parallel.DistributedDataParallel(model_train, device_ids=[local_rank], find_unused_parameters=True)
        else:
            model_train = torch.nn.DataParallel(model)
            cudnn.benchmark = True
            model_train = model_train.cuda()

    ema = ModelEMA(model_train)
    
    with open(train_annotation_path, encoding='utf-8') as f:
        train_lines = f.readlines()
    with open(val_annotation_path, encoding='utf-8') as f:
        val_lines = f.readlines()
    num_train = len(train_lines)
    num_val = len(val_lines)

    if local_rank == 0:
        show_config(
            classes_path = classes_path, anchors_path = anchors_path, anchors_mask = anchors_mask, model_path = model_path, input_shape = input_shape, \
            Init_Epoch = Init_Epoch, Freeze_Epoch = Freeze_Epoch, UnFreeze_Epoch = UnFreeze_Epoch, Freeze_batch_size = Freeze_batch_size, Unfreeze_batch_size = Unfreeze_batch_size, Freeze_Train = Freeze_Train, \
            Init_lr = Init_lr, Min_lr = Min_lr, optimizer_type = optimizer_type, momentum = momentum, lr_decay_type = lr_decay_type, \
            save_period = save_period, save_dir = save_dir, num_train = num_train, num_val = num_val
        )

        wanted_step = 5e4 if optimizer_type == "sgd" else 1.5e4
        total_step  = num_train // Unfreeze_batch_size * UnFreeze_Epoch
        if total_step <= wanted_step:
            if num_train // Unfreeze_batch_size == 0:
                raise ValueError('dataset is too small. ')
            wanted_epoch = wanted_step // (num_train // Unfreeze_batch_size) + 1
            print("\n\033[1;33;44m[Warning] 使用%s优化器时，建议将训练总步长设置到%d以上。\033[0m"%(optimizer_type, wanted_step))
            print("\033[1;33;44m[Warning] 本次运行的总训练数据量为%d，Unfreeze_batch_size为%d，共训练%d个Epoch，计算出总训练步长为%d。\033[0m"%(num_train, Unfreeze_batch_size, UnFreeze_Epoch, total_step))
            print("\033[1;33;44m[Warning] 由于总训练步长为%d，小于建议总步长%d，建议设置总世代为%d。\033[0m"%(total_step, wanted_step, wanted_epoch))

    UnFreeze_flag = False
    if Freeze_Train:
        for param in model.backbone.parameters():
            param.requires_grad = False

    batch_size = Freeze_batch_size if Freeze_Train else Unfreeze_batch_size

    nbs = 64
    lr_limit_max = 1e-3 if optimizer_type == 'adam' else 5e-2
    lr_limit_min = 3e-4 if optimizer_type == 'adam' else 5e-4
    Init_lr_fit = min(max(batch_size / nbs * Init_lr, lr_limit_min), lr_limit_max)
    Min_lr_fit = min(max(batch_size / nbs * Min_lr, lr_limit_min * 1e-2), lr_limit_max * 1e-2)

    pg0, pg1, pg2 = [], [], []  
    for k, v in model.named_modules():
        if hasattr(v, "bias") and isinstance(v.bias, nn.Parameter):
            pg2.append(v.bias)    
        if isinstance(v, nn.BatchNorm2d) or "bn" in k:
            pg0.append(v.weight)    
        elif hasattr(v, "weight") and isinstance(v.weight, nn.Parameter):
            pg1.append(v.weight)   
    optimizer = {
        'adam'  : optim.Adam(pg0, Init_lr_fit, betas = (momentum, 0.999)),
        'sgd'   : optim.SGD(pg0, Init_lr_fit, momentum = momentum, nesterov=True)
    }[optimizer_type]
    optimizer.add_param_group({"params": pg1, "weight_decay": weight_decay})
    optimizer.add_param_group({"params": pg2})

    lr_scheduler_func = get_lr_scheduler(lr_decay_type, Init_lr_fit, Min_lr_fit, UnFreeze_Epoch)
        
    epoch_step = num_train // batch_size
    epoch_step_val = num_val // batch_size
        
    if epoch_step == 0 or epoch_step_val == 0:
        raise ValueError("数据集过小，无法继续进行训练，请扩充数据集。")

    if ema:
        ema.updates = epoch_step * Init_Epoch
        
    train_dataset = YoloDataset(train_lines, input_shape, num_classes, anchors, anchors_mask, epoch_length=UnFreeze_Epoch, \
                                        mosaic=mosaic, mixup=mixup, mosaic_prob=mosaic_prob, mixup_prob=mixup_prob, train=True, special_aug_ratio=special_aug_ratio)
    val_dataset = YoloDataset(val_lines, input_shape, num_classes, anchors, anchors_mask, epoch_length=UnFreeze_Epoch, \
                                        mosaic=False, mixup=False, mosaic_prob=0, mixup_prob=0, train=False, special_aug_ratio=0)
        
    if distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset, shuffle=True,)
        val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset, shuffle=False,)
        batch_size = batch_size // ngpus_per_node
        shuffle = False
    else:
        train_sampler = None
        val_sampler = None
        shuffle = True

    gen = DataLoader(train_dataset, shuffle = shuffle, batch_size = batch_size, pin_memory=True,drop_last=True, collate_fn=yolo_dataset_collate, sampler=train_sampler)
    gen_val = DataLoader(val_dataset  , shuffle = shuffle, batch_size = batch_size, pin_memory=True, drop_last=True, collate_fn=yolo_dataset_collate, sampler=val_sampler)

    for epoch in range(Init_Epoch, UnFreeze_Epoch):
        if epoch >= Freeze_Epoch and not UnFreeze_flag and Freeze_Train:
            batch_size = Unfreeze_batch_size
            nbs = 64
            lr_limit_max = 1e-3 if optimizer_type == 'adam' else 5e-2
            lr_limit_min = 3e-4 if optimizer_type == 'adam' else 5e-4
            Init_lr_fit = min(max(batch_size / nbs * Init_lr, lr_limit_min), lr_limit_max)
            Min_lr_fit = min(max(batch_size / nbs * Min_lr, lr_limit_min * 1e-2), lr_limit_max * 1e-2)
            lr_scheduler_func = get_lr_scheduler(lr_decay_type, Init_lr_fit, Min_lr_fit, UnFreeze_Epoch)

            for param in model.backbone.parameters():
                param.requires_grad = True

            epoch_step = num_train // batch_size
            epoch_step_val = num_val // batch_size

            if epoch_step == 0 or epoch_step_val == 0:
                raise ValueError("数据集过小，无法继续进行训练，请扩充数据集。")
                    
            if ema:
                ema.updates = epoch_step * epoch

            if distributed:
                batch_size = batch_size // ngpus_per_node
                    
            gen = DataLoader(train_dataset, shuffle = shuffle, batch_size = batch_size, pin_memory=True,
                                            drop_last=True, collate_fn=yolo_dataset_collate, sampler=train_sampler)
            gen_val = DataLoader(val_dataset, shuffle = shuffle, batch_size = batch_size, pin_memory=True, 
                                            drop_last=True, collate_fn=yolo_dataset_collate, sampler=val_sampler)
            UnFreeze_flag = True

        gen.dataset.epoch_now = epoch
        gen_val.dataset.epoch_now = epoch

        if distributed:
            train_sampler.set_epoch(epoch)

        set_optimizer_lr(optimizer, lr_scheduler_func, epoch)

        # fit_one_epoch(model_train, model, ema, yolo_loss, loss_history, optimizer, epoch, epoch_step, epoch_step_val, gen, gen_val, UnFreeze_Epoch, Cuda, fp16, scaler, save_period, save_dir, local_rank)
        fit_one_epoch(model_train = model_train, model= model, ema= ema, yolo_loss=yolo_loss, loss_history=loss_history, optimizer=optimizer, \
            epoch=epoch, epoch_step=epoch_step, epoch_step_val=epoch_step_val, gen = gen, gen_val = gen_val, \
                UnFreeze_Epoch=UnFreeze_Epoch, cuda=Cuda, fp16=fp16, scaler=scaler, save_period=save_period, save_dir=save_dir, local_rank=local_rank, saved_weight=config.save_weight)
        if distributed:
            dist.barrier()

    if local_rank == 0:
        loss_history.writer.close()
