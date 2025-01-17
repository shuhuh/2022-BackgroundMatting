import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.optim import Adam
from torchvision import transforms as T
from torchvision.utils import make_grid
from PIL import Image
from dataset_utils.image_dataset import ImageDataset
from dataset_utils.concat_rgb_alp import ConcatRGBAlp
from dataset_utils.concat_img_bck import ConcatImgBck
from dataset_utils.small_dataset import SmallDataset
from torch.utils.tensorboard import SummaryWriter
import dataset_utils.augumentation as A
import argparse
import random
import kornia
import os
from tqdm import tqdm
from models.model import BaseNet

import hydra

@hydra.main(config_path="configs", config_name="train_base.yaml")
def main(config):
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    print("device : "+str(device))

    #train dataset
    print("preparing training dataset...")
    train_rgb_data = ImageDataset(config["train_rgb_path"],"RGB") #foreground data
    train_alp_data = ImageDataset(config["train_alp_path"],"L") #alpha matte data
    train_bck_data = ImageDataset(config["train_bck_path"],"RGB",transforms=T.Compose([
                                    A.RandomAffineAndResize((512, 512), degrees=(-5, 5), translate=(0.1, 0.1), scale=(1., 2.), shear=(-5, 5)),
                                    T.RandomHorizontalFlip(),
                                    A.RandomBoxBlur(0.1, 5),
                                    A.RandomSharpn(0.1),
                                    T.ColorJitter(0.15, 0.15, 0.15, 0.05),
                                    T.ToTensor()
                                    ])) #background data
    train_rbg_alp = ConcatRGBAlp(train_rgb_data,train_alp_data,
                                transforms=A.PairCompose([
                                    A.PairRandomAffineAndResize((512, 512), degrees=(-5, 5), translate=(0.1, 0.1), scale=(0.4, 1), shear=(-5, 5)),
                                    A.PairRandomHorizontalFlip(),
                                    A.PairRandomBoxBlur(0.1, 5),
                                    A.PairRandomSharpen(0.1),
                                    A.PairApplyOnlyAtIndices([1], T.ColorJitter(0.15, 0.15, 0.15, 0.05)),
                                    A.PairApply(T.ToTensor())
                                ])) #tuple of foreground & alpha matte with augumentation
    train_rgb_alp_bck = ConcatImgBck(train_rbg_alp, train_bck_data) #tuple of foreground & alpha matte & background with augumentation
    
    training_dataset = DataLoader(train_rgb_alp_bck, shuffle=True, batch_size=config["batch_size"], num_workers=config["num_workers"], pin_memory=True)

    #validation dataset
    print("preparing validation dataset...")
    valid_rgb_data = ImageDataset(config["valid_rgb_path"],"RGB") #foreground data
    valid_alp_data = ImageDataset(config["valid_alp_path"],"L") #alpha matte data
    valid_bck_data = ImageDataset(config["valid_bck_path"],"RGB",transforms=T.Compose([
                                    A.RandomAffineAndResize((512, 512), degrees=(-5, 5), translate=(0.1, 0.1), scale=(1., 1.2), shear=(-5, 5)),
                                    T.ToTensor()
                                    ])) #background data
    valid_rbg_alp = ConcatRGBAlp(valid_rgb_data,valid_alp_data,
                                transforms=A.PairCompose([
                                    A.PairRandomAffineAndResize((512, 512), degrees=(-5, 5), translate=(0.1, 0.1), scale=(0.3, 1.), shear=(-5, 5)),
                                    A.PairApply(T.ToTensor())
                                ])) #tuple of foreground & alpha matte with augumentation
    valid_rgb_alp_bck = ConcatImgBck(valid_rbg_alp, valid_bck_data) #tuple of foreground & alpha matte & background with augumentation

    valid_small_dataset = SmallDataset(valid_rgb_alp_bck, 50)
    valid_dataset = DataLoader(valid_small_dataset, pin_memory=True, batch_size=config["batch_size"], num_workers=config["num_workers"])

    #model
    print("setting up model...")
    model = BaseNet().to(device)
    model.load_deeplabv3_pretrained_state_dict(torch.load(config["pretrained_model"])['model_state'])

    #optimizer
    optimizer = Adam([
        {'params': model.backbone.parameters(), 'lr': 1e-4},
        {'params': model.aspp.parameters(), 'lr': 5e-4},
        {'params': model.decoder.parameters(), 'lr': 5e-4},
    ])

    #checkpoint dir
    if not os.path.exists(config["checkpoint_path"]):
            os.makedirs(config["checkpoint_path"])
    #logging
    writer = SummaryWriter(config["logging_path"])


    print("============start training=============")
    for epoch in range(config["epochs"]):
        print(f'epoch : {epoch}')
        print("training epoch")
        for i, ((fgr_in, alp_in), bck_in) in enumerate(tqdm(training_dataset)):
            #training
            model.train()
            step = epoch * len(training_dataset) + i

            fgr_in = fgr_in.to(device)
            alp_in = alp_in.to(device)
            bck_in = bck_in.to(device)
            fgr_in, alp_in, bck_in = random_corp(fgr_in, alp_in, bck_in)

            src_in = bck_in.clone()

            #background shadow augumentation (same as original code)
            aug_shadow_index = torch.rand(len(src_in)) < 0.3
            if aug_shadow_index.any():
                aug_shadow = alp_in[aug_shadow_index].mul(0.3 * random.random())
                aug_shadow = T.RandomAffine(degrees=(-5, 5), translate=(0.2, 0.2), scale=(0.5, 1.5), shear=(-5, 5))(aug_shadow)
                aug_shadow = kornia.filters.box_blur(aug_shadow, (random.choice(range(20, 40)),) * 2)
                src_in[aug_shadow_index] = src_in[aug_shadow_index].sub_(aug_shadow).clamp_(0, 1)
                del aug_shadow
            del aug_shadow_index
            
            #composite foreground residual onto background
            src_in = fgr_in * alp_in + src_in * (1 - alp_in)

            #noise augumentation (same as original code)
            aug_noise_index = torch.rand(len(src_in)) < 0.4
            if aug_noise_index.any():
                src_in[aug_noise_index] = src_in[aug_noise_index].add_(torch.randn_like(src_in[aug_noise_index]).mul_(0.03 * random.random())).clamp_(0, 1)
                bck_in[aug_noise_index] = bck_in[aug_noise_index].add_(torch.randn_like(bck_in[aug_noise_index]).mul_(0.03 * random.random())).clamp_(0, 1)
            del aug_noise_index

            #background jitter augumentation
            aug_jitter_index = torch.rand(len(src_in)) < 0.8
            if aug_jitter_index.any():
                bck_in[aug_jitter_index] = kornia.augmentation.ColorJitter(0.18, 0.18, 0.18, 0.1)(bck_in[aug_jitter_index])
            del aug_jitter_index

            #background affine augumentation
            aug_affine_index = torch.rand(len(bck_in)) < 0.3
            if aug_affine_index.any():
                bck_in[aug_affine_index] = T.RandomAffine(degrees=(-1, 1), translate=(0.01, 0.01))(bck_in[aug_affine_index])
            del aug_affine_index

            alp_pred, fgr_pred, err_pred = model(src_in, bck_in)[:3]
            loss = calc_loss(alp_pred, fgr_pred, err_pred, alp_in, fgr_in)

            #backprop
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if (i+1) % 10 == 0:
                writer.add_scalar('loss', loss, step)
            if (i+1) % 2000 == 0:
                writer.add_image('train_alp_pred', make_grid(alp_pred, nrow=5), step)
                writer.add_image('train_fgr_pred', make_grid(fgr_pred, nrow=5), step)
                writer.add_image('train_com_pred', make_grid(fgr_pred * alp_pred, nrow=5), step)
                writer.add_image('train_err_pred', make_grid(err_pred, nrow=5), step)
                writer.add_image('train_src_in', make_grid(src_in, nrow=5), step)
                writer.add_image('train_bgr_in', make_grid(bck_in, nrow=5), step)

            del alp_in, fgr_in, bck_in
            del alp_pred, fgr_pred, err_pred

            #validation
            if (i+1) % 5000 == 0:             
                model.eval()
                loss_total = 0
                count = 0
                print("validating")
                with torch.no_grad():
                    for j, ((fgr_in, alp_in), bck_in) in enumerate(valid_dataset):
                        fgr_in = fgr_in.to(device)
                        alp_in = alp_in.to(device)
                        bck_in = bck_in.to(device)

                        #composite foreground residual onto background
                        src_in = fgr_in * alp_in + bck_in * (1 - alp_in)

                        alp_pred, fgr_pred, err_pred = model(src_in, bck_in)[:3]
                        loss = calc_loss(alp_pred, fgr_pred, err_pred, alp_in, fgr_in)
                        loss_total += loss.cpu().item() * config["batch_size"]
                        count += config["batch_size"]
                writer.add_scalar('valid_loss', loss_total / count, step)

            if (step + 1) % 5000 == 0:
                torch.save(model.state_dict(), config["checkpoint_path"] + f'/checkpoint_epoch{epoch}_iter{step}.pth')

        torch.save(model.state_dict(), config["checkpoint_path"] + f'/checkpoint_epoch{epoch}.pth')

#calculate loss
def calc_loss(alp_pred, fgr_pred, err_pred, alp_in, fgr_in):
    err_true = torch.abs(alp_pred.detach() - alp_in) #detach not to calculate grad
    msk_true = alp_in > 0
    l_alp = F.l1_loss(alp_pred, alp_in) + F.l1_loss(kornia.filters.sobel(alp_pred), kornia.filters.sobel(alp_in))
    l_F = F.l1_loss(fgr_pred * msk_true, fgr_in * msk_true)
    l_E = F.mse_loss(err_pred, err_true)
    return l_alp  + l_F + l_E

#random_crop function using kornia to make it differentiable
def random_corp(*images):
    width = random.choice(range(256, 512))
    height = random.choice(range(256, 512))
    results = []
    for img in images:
        img = kornia.geometry.transform.resize(img, (max(height,width), max(height,width)))
        img = kornia.geometry.transform.center_crop(img, (height, width))
        results.append(img)
    return results
    

if __name__ == "__main__":
    main()