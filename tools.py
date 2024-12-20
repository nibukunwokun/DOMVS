import torch
import os
import math
import numpy as np
import torchvision.utils as vutils
import torch.distributed as dist
from torch.optim.lr_scheduler import LambdaLR
import torch.nn as nn

class DictAverageMeter(object):
    def __init__(self):
        self.sum_data = {}
        self.avg_data = {}
        self.count = 0

    def update(self, new_input):
        self.count += 1
        if len(self.sum_data) == 0:
            for k, v in new_input.items():
                if not isinstance(v, float):
                    raise NotImplementedError("invalid data {}: {}".format(k, type(v)))
                self.sum_data[k] = v
                self.avg_data[k] = v
        else:
            for k, v in new_input.items():
                if not isinstance(v, float):
                    raise NotImplementedError("invalid data {}: {}".format(k, type(v)))
                self.sum_data[k] += v
                self.avg_data[k] = self.sum_data[k] / self.count


def write_cam(file, cam):
    f = open(file, "w")
    f.write('extrinsic\n')
    for i in range(0, 4):
        for j in range(0, 4):
            f.write(str(cam[0][i][j]) + ' ')
        f.write('\n')
    f.write('\n')

    f.write('intrinsic\n')
    for i in range(0, 3):
        for j in range(0, 3):
            f.write(str(cam[1][i][j]) + ' ')
        f.write('\n')

    f.write('\n' + str(cam[1][3][0]) + ' ' + str(cam[1][3][1]) + ' ' + str(cam[1][3][2]) + ' ' + str(cam[1][3][3]) + '\n')

    f.close()


# convert a function into recursive style to handle nested dict/list/tuple variables
def make_recursive_func(func):
    def wrapper(vars):
        if isinstance(vars, list):
            return [wrapper(x) for x in vars]
        elif isinstance(vars, tuple):
            return tuple([wrapper(x) for x in vars])
        elif isinstance(vars, dict):
            return {k: wrapper(v) for k, v in vars.items()}
        else:
            return func(vars)

    return wrapper


def save_scalars(logger, mode, scalar_dict, global_step):
    scalar_dict = tensor2float(scalar_dict)
    for key, value in scalar_dict.items():
        if not isinstance(value, (list, tuple)):
            name = '{}/{}'.format(mode, key)
            logger.add_scalar(name, value, global_step)
        else:
            for idx in range(len(value)):
                name = '{}/{}_{}'.format(mode, key, idx)
                logger.add_scalar(name, value[idx], global_step)


def save_images(logger, mode, images_dict, global_step):
    images_dict = tensor2numpy(images_dict)

    def preprocess(name, img):
        if not (len(img.shape) == 3 or len(img.shape) == 4):
            raise NotImplementedError("invalid img shape {}:{} in save_images".format(name, img.shape))
        if len(img.shape) == 3:
            img = img[:, np.newaxis, :, :]
        img = torch.from_numpy(img[:1])
        return vutils.make_grid(img, padding=0, nrow=1, normalize=True, scale_each=True)

    for key, value in images_dict.items():
        if not isinstance(value, (list, tuple)):
            name = '{}/{}'.format(mode, key)
            logger.add_image(name, preprocess(name, value), global_step)
        else:
            for idx in range(len(value)):
                name = '{}/{}_{}'.format(mode, key, idx)
                logger.add_image(name, preprocess(name, value[idx]), global_step)

@make_recursive_func
def tensor2numpy_str(vars):
    if isinstance(vars, np.ndarray):
        return vars
    elif isinstance(vars, torch.Tensor):
        return vars.detach().cpu().numpy().copy()
    elif isinstance(vars, str):
        return vars
    else:
        raise NotImplementedError("invalid input type {} for tensor2numpy".format(type(vars)))

@make_recursive_func
def tensor2numpy(vars):
    if isinstance(vars, np.ndarray):
        return vars
    elif isinstance(vars, torch.Tensor):
        return vars.detach().cpu().numpy().copy()
    else:
        raise NotImplementedError("invalid input type {} for tensor2numpy".format(type(vars)))


@make_recursive_func
def tensor2float(vars):
    if isinstance(vars, float):
        return vars
    elif isinstance(vars, torch.Tensor):
        return vars.data.item()
    else:
        raise NotImplementedError("invalid input type {} for tensor2float".format(type(vars)))


def reduce_scalar_outputs(scalar_outputs):
    world_size = get_world_size()
    if world_size < 2:
        return scalar_outputs
    with torch.no_grad():
        names = []
        scalars = []
        for k in sorted(scalar_outputs.keys()):
            names.append(k)
            scalars.append(scalar_outputs[k])
        scalars = torch.stack(scalars, dim=0)
        dist.reduce(scalars, dst=0)
        if dist.get_rank() == 0:
            # only main process gets accumulated, so only divide by
            # world_size in this case
            scalars /= world_size
        reduced_scalars = {k: v for k, v in zip(names, scalars)}

    return reduced_scalars


@make_recursive_func
def tocuda(vars):
    if isinstance(vars, torch.Tensor):
        return vars.to(torch.device("cuda"))
    elif isinstance(vars, str):
        return vars
    else:
        raise NotImplementedError("invalid input type {} for tensor2numpy".format(type(vars)))


# a wrapper to compute metrics for each image individually
def compute_metrics_for_each_image(metric_func):
    def wrapper(depth_est, depth_gt, mask, *args):
        batch_size = depth_gt.shape[0]
        results = []
        # compute result one by one
        for idx in range(batch_size):
            ret = metric_func(depth_est[idx], depth_gt[idx], mask[idx], *args)
            results.append(ret)
        return torch.stack(results).mean()

    return wrapper


@torch.no_grad()
@compute_metrics_for_each_image
def AbsDepthError_metrics(depth_est, depth_gt, mask, thres=None):
    depth_est, depth_gt = depth_est[mask], depth_gt[mask]
    error = (depth_est - depth_gt).abs()
    if thres is not None:
        error = error[(error >= float(thres[0])) & (error <= float(thres[1]))]
        if error.shape[0] == 0:
            return torch.tensor(0, device=error.device, dtype=error.dtype)
    return torch.mean(error)


@torch.no_grad()
@compute_metrics_for_each_image
def Thres_metrics(depth_est, depth_gt, mask, thres):
    assert isinstance(thres, (int, float))
    depth_est, depth_gt = depth_est[mask], depth_gt[mask]
    errors = torch.abs(depth_est - depth_gt)
    err_mask = errors > thres
    return torch.mean(err_mask.float())


def generate_pointcloud(rgb, depth, ply_file, intr, scale=1.0):
    """
    Generate a colored point cloud in PLY format from a color and a depth image.

    Input:
    rgb_file -- filename of color image
    depth_file -- filename of depth image
    ply_file -- filename of ply file

    """
    fx, fy, cx, cy = intr[0, 0], intr[1, 1], intr[0, 2], intr[1, 2]
    points = []
    for v in range(rgb.shape[0]):
        for u in range(rgb.shape[1]):
            color = rgb[v, u]  # rgb.getpixel((u, v))
            Z = depth[v, u] / scale
            if Z == 0: continue
            X = (u - cx) * Z / fx
            Y = (v - cy) * Z / fy
            points.append("%f %f %f %d %d %d 0\n" % (X, Y, Z, color[0], color[1], color[2]))
    file = open(ply_file, "w")
    file.write('''ply
            format ascii 1.0
            element vertex %d
            property float x
            property float y
            property float z
            property uchar red
            property uchar green
            property uchar blue
            property uchar alpha
            end_header
            %s
            ''' % (len(points), "".join(points)))
    file.close()
    print("save ply, fx:{}, fy:{}, cx:{}, cy:{}".format(fx, fy, cx, cy))


def get_schedular(optimizer, args):
    warmup = args.warmup
    milestones = np.array(args.milestones)
    decay = args.lr_decay
    if args.scheduler == "steplr":
        lambda_func = lambda step: 1 / 3 * (1 - step / warmup) + step / warmup if step < warmup \
            else (decay ** (milestones <= step).sum())
    elif args.scheduler == "cosinelr":
        max_lr = args.lr
        min_lr = max_lr * (args.lr_decay ** 3)
        T_max = args.epochs
        lambda_func = lambda step: 1 / 3 * (1 - step / warmup) + step / warmup if step < warmup else \
            (min_lr + 0.5 * (max_lr - min_lr) * (1.0 + math.cos((step - warmup) / (T_max - warmup) * math.pi))) / max_lr

    scheduler = LambdaLR(optimizer, lambda_func)
    return scheduler


def is_dist_avail_and_initialized():
    if not dist.is_available():
        return False
    if not dist.is_initialized():
        return False
    return True


def get_world_size():
    if not is_dist_avail_and_initialized():
        return 1
    return dist.get_world_size()


def get_rank():
    if not is_dist_avail_and_initialized():
        return 0
    return dist.get_rank()


def is_main_process():
    return get_rank() == 0


def setup_for_distributed(is_master):
    """
    This function disables printing when not in master process
    """
    import builtins as __builtin__
    builtin_print = __builtin__.print

    def print(*args, **kwargs):
        force = kwargs.pop('force', False)
        if is_master or force:
            builtin_print(*args, **kwargs)

    __builtin__.print = print


def init_distributed_mode(args):
    if 'RANK' in os.environ and 'WORLD_SIZE' in os.environ:
        args.rank = int(os.environ["RANK"])
        args.world_size = int(os.environ['WORLD_SIZE'])
        args.gpu = int(os.environ['LOCAL_RANK'])
    elif 'SLURM_PROCID' in os.environ:
        args.rank = int(os.environ['SLURM_PROCID'])
        args.gpu = args.rank % torch.cuda.device_count()
    elif hasattr(args, "rank"):
        pass
    else:
        print('Not using distributed mode')
        args.distributed = False
        return

    args.distributed = True

    torch.cuda.set_device(args.gpu)
    args.dist_backend = 'nccl'
    print('| distributed init (rank {}): {}'.format(
        args.rank, args.dist_url), flush=True)
    torch.distributed.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                         world_size=args.world_size, rank=args.rank)
    setup_for_distributed(args.rank == 0)
    
def smooth_item_l0_5(x,beta):
    mask = x<beta
    if not mask.sum()== 0:
        x[mask] = 32768*torch.square(x[mask])
    if not (~mask).sum()== 0:
        x[~mask] = torch.sqrt(x[~mask])
    return x

def smooth_l0_5(pred, gt, beta=0.00097656):
    assert pred.shape == gt.shape, "the shapes of pred and gt are not matched."
    error = pred - gt
    abs_error = torch.abs(error)
    
    smooth_sqrt_abs_error = smooth_item_l0_5(abs_error, beta)
    loss = torch.mean(smooth_sqrt_abs_error)
    return loss

class SSIM(nn.Module):
    """Layer to compute the SSIM loss between a pair of images
    """
    def __init__(self):
        super(SSIM, self).__init__()
        self.mu_x_pool   = nn.AvgPool2d(3, 1)
        self.mu_y_pool   = nn.AvgPool2d(3, 1)
        self.sig_x_pool  = nn.AvgPool2d(3, 1)
        self.sig_y_pool  = nn.AvgPool2d(3, 1)
        self.sig_xy_pool = nn.AvgPool2d(3, 1)
        self.mask_pool = nn.AvgPool2d(3, 1)
        self.C1 = 0.01 ** 2
        self.C2 = 0.03 ** 2

    def forward(self, x, y, mask):
        x = x.permute(0, 3, 1, 2)  # [B, H, W, C] --> [B, C, H, W]
        y = y.permute(0, 3, 1, 2)
        mask = mask.permute(0, 3, 1, 2)
        mu_x = self.mu_x_pool(x)
        mu_y = self.mu_y_pool(y)
        sigma_x  = self.sig_x_pool(x ** 2) - mu_x ** 2
        sigma_y  = self.sig_y_pool(y ** 2) - mu_y ** 2
        sigma_xy = self.sig_xy_pool(x * y) - mu_x * mu_y
        SSIM_n = (2 * mu_x * mu_y + self.C1) * (2 * sigma_xy + self.C2)
        SSIM_d = (mu_x ** 2 + mu_y ** 2 + self.C1) * (sigma_x + sigma_y + self.C2) 
  
        SSIM_mask = self.mask_pool(mask)

        output = SSIM_mask * (torch.clamp((1 - SSIM_n / SSIM_d) / 2, 0, 1) + 1e-6)

        if torch.sum(SSIM_mask.type(torch.float32)) == 0:
            output = torch.zeros_like(output,dtype=torch.float32,device=output.device)
        return output.permute(0, 2, 3, 1)  # [B, C, H, W] --> [B, H, W, C]


def gradient_x(img):
    return img[:, :, :-1, :] - img[:, :, 1:, :]

def gradient_y(img):
    return img[:, :-1, :, :] - img[:, 1:, :, :]

def gradient(pred):
    D_dy = pred[:, 1:, :, :] - pred[:, :-1, :, :]
    D_dx = pred[:, :, 1:, :] - pred[:, :, :-1, :]
    return D_dx, D_dy


def depth_smoothness(depth, img,lambda_wt=1):
    """Computes image-aware depth smoothness loss."""
    # print('depth: {} img: {}'.format(depth.shape, img.shape))
    depth_dx = gradient_x(depth)
    depth_dy = gradient_y(depth)
    image_dx = gradient_x(img)
    image_dy = gradient_y(img)
    weights_x = torch.exp(-(lambda_wt * torch.mean(torch.abs(image_dx), 3, keepdim=True)))
    weights_y = torch.exp(-(lambda_wt * torch.mean(torch.abs(image_dy), 3, keepdim=True)))
    smoothness_x = depth_dx * weights_x
    smoothness_y = depth_dy * weights_y
    return torch.mean(torch.abs(smoothness_x)) + torch.mean(torch.abs(smoothness_y))


def compute_reconstr_loss_l0_5(warped, ref, mask, simple=True):
    if simple:
        return smooth_l0_5(warped*mask, ref*mask)
    else:
        alpha = 0.5
        ref_dx, ref_dy = gradient(ref * mask)
        warped_dx, warped_dy = gradient(warped * mask)
        photo_loss = smooth_l0_5(warped*mask, ref*mask)
        grad_loss = smooth_l0_5(warped_dx, ref_dx) + \
                    smooth_l0_5(warped_dy, ref_dy)
        return (1 - alpha) * photo_loss + alpha * grad_loss

    
def inverse_warping(img, left_cam, right_cam, depth):
    # img: [batch_size, height, width, channels]
    # cameras (K, R, t)
    R_left = left_cam[:, 0:1, 0:3, 0:3]  # [B, 1, 3, 3]
    R_right = right_cam[:, 0:1, 0:3, 0:3]  # [B, 1, 3, 3]
    t_left = left_cam[:, 0:1, 0:3, 3:4]  # [B, 1, 3, 1]
    t_right = right_cam[:, 0:1, 0:3, 3:4]  # [B, 1, 3, 1]
    K_left = left_cam[:, 1:2, 0:3, 0:3]  # [B, 1, 3, 3]
    K_right = right_cam[:, 1:2, 0:3, 0:3]  # [B, 1, 3, 3]

    K_left = K_left.squeeze(1)  # [B, 3, 3]
    K_left_inv = torch.inverse(K_left)  # [B, 3, 3]
    R_left_trans = R_left.squeeze(1).permute(0, 2, 1)  # [B, 3, 3]
    R_right_trans = R_right.squeeze(1).permute(0, 2, 1)  # [B, 3, 3]

    R_left = R_left.squeeze(1)
    t_left = t_left.squeeze(1)
    R_right = R_right.squeeze(1)
    t_right = t_right.squeeze(1)

    # estimate egomotion by inverse composing R1,R2 and t1,t2
    R_rel = torch.matmul(R_right, R_left_trans)  # [B, 3, 3]    
    t_rel = t_right - torch.matmul(R_rel, t_left)  # [B, 3, 1]  
    # now convert R and t to transform mat, as in SFMlearner
    batch_size = R_left.shape[0]
    filler = torch.Tensor([0.0, 0.0, 0.0, 1.0]).cuda().reshape(1, 1, 4)  # [1, 1, 4]
    filler = filler.repeat(batch_size, 1, 1)  # [B, 1, 4]
    transform_mat = torch.cat([R_rel, t_rel], dim=2)  # [B, 3, 4]
    transform_mat = torch.cat([transform_mat.float(), filler.float()], dim=1)  # [B, 4, 4]
    batch_size, img_height, img_width, _ = img.shape
    depth = depth.reshape(batch_size, 1, img_height * img_width)  # [batch_size, 1, height * width]

    grid = _meshgrid_abs(img_height, img_width)  # [3, height * width]
    grid = grid.unsqueeze(0).repeat(batch_size, 1, 1)  # [batch_size, 3, height * width]
    cam_coords = _pixel2cam(depth, grid, K_left_inv)  # [batch_size, 3, height * width]
    ones = torch.ones([batch_size, 1, img_height * img_width]).cuda()  # [batch_size, 1, height * width]
    cam_coords_hom = torch.cat([cam_coords, ones], dim=1)  # [batch_size, 4, height * width]

    # Get projection matrix for target camera frame to source pixel frame
    hom_filler = torch.Tensor([0.0, 0.0, 0.0, 1.0]).cuda().reshape(1, 1, 4)  # [1, 1, 4]
    hom_filler = hom_filler.repeat(batch_size, 1, 1)  # [B, 1, 4]
    intrinsic_mat_hom = torch.cat([K_left.float(), torch.zeros([batch_size, 3, 1]).cuda()], dim=2)  # [B, 3, 4]
    intrinsic_mat_hom = torch.cat([intrinsic_mat_hom, hom_filler], dim=1)  # [B, 4, 4]
    proj_target_cam_to_source_pixel = torch.matmul(intrinsic_mat_hom, transform_mat)  # [B, 4, 4]
    source_pixel_coords = _cam2pixel(cam_coords_hom, proj_target_cam_to_source_pixel)  # [batch_size, 2, height * width]
    source_pixel_coords = source_pixel_coords.reshape(batch_size, 2, img_height, img_width)   # [batch_size, 2, height, width]
    source_pixel_coords = source_pixel_coords.permute(0, 2, 3, 1)  # [batch_size, height, width, 2]
    warped_right, mask = _spatial_transformer(img, source_pixel_coords)
    return warped_right, mask


def _meshgrid_abs(height, width):
    """Meshgrid in the absolute coordinates."""
    x_t = torch.matmul(
        torch.ones([height, 1]),
        torch.linspace(-1.0, 1.0, width).unsqueeze(1).permute(1, 0)
    )  # [height, width]
    y_t = torch.matmul(
        torch.linspace(-1.0, 1.0, height).unsqueeze(1),
        torch.ones([1, width])
    )
    x_t = (x_t + 1.0) * 0.5 * (width - 1)
    y_t = (y_t + 1.0) * 0.5 * (height - 1)
    x_t_flat = x_t.reshape(1, -1)
    y_t_flat = y_t.reshape(1, -1)
    ones = torch.ones_like(x_t_flat)
    grid = torch.cat([x_t_flat, y_t_flat, ones], dim=0)  # [3, height * width]
    # return grid.to(device)
    return grid.cuda()


def _pixel2cam(depth, pixel_coords, intrinsic_mat_inv):
    """Transform coordinates in the pixel frame to the camera frame."""
    cam_coords = torch.matmul(intrinsic_mat_inv.float(), pixel_coords.float()) * depth.float()
    return cam_coords


def _cam2pixel(cam_coords, proj_c2p):
    """Transform coordinates in the camera frame to the pixel frame."""
    pcoords = torch.matmul(proj_c2p, cam_coords)  # [batch_size, 4, height * width]
    x = pcoords[:, 0:1, :]  # [batch_size, 1, height * width]
    y = pcoords[:, 1:2, :]  # [batch_size, 1, height * width]
    z = pcoords[:, 2:3, :]  # [batch_size, 1, height * width]
    x_norm = x / (z + 1e-10)
    y_norm = y / (z + 1e-10)
    pixel_coords = torch.cat([x_norm, y_norm], dim=1)
    return pixel_coords  # [batch_size, 2, height * width]


def _spatial_transformer(img, coords):
    """A wrapper over binlinear_sampler(), taking absolute coords as input."""
    # img: [B, H, W, C]
    img_height = img.shape[1]
    img_width = img.shape[2]
    px = coords[:, :, :, :1]  # [batch_size, height, width, 1]
    py = coords[:, :, :, 1:]  # [batch_size, height, width, 1]
    # Normalize coordinates to [-1, 1] to send to _bilinear_sampler.
    px = px / (img_width - 1) * 2.0 - 1.0  # [batch_size, height, width, 1]
    py = py / (img_height - 1) * 2.0 - 1.0  # [batch_size, height, width, 1]
    output_img, mask = _bilinear_sample(img, px, py)
    return output_img, mask


def _bilinear_sample(im, x, y, name='bilinear_sampler'):
    """Perform bilinear sampling on im given list of x, y coordinates.
    Implements the differentiable sampling mechanism with bilinear kernel
    in https://arxiv.org/abs/1506.02025.
    x,y are tensors specifying normalized coordinates [-1, 1] to be sampled on im.
    For example, (-1, -1) in (x, y) corresponds to pixel location (0, 0) in im,
    and (1, 1) in (x, y) corresponds to the bottom right pixel in im.
    Args:
        im: Batch of images with shape [B, h, w, channels].
        x: Tensor of normalized x coordinates in [-1, 1], with shape [B, h, w, 1].
        y: Tensor of normalized y coordinates in [-1, 1], with shape [B, h, w, 1].
        name: Name scope for ops.
    Returns:
        Sampled image with shape [B, h, w, channels].
        Principled mask with shape [B, h, w, 1], dtype:float32.  A value of 1.0
        in the mask indicates that the corresponding coordinate in the sampled
        image is valid.
      """
    x = x.reshape(-1)  # [batch_size * height * width]
    y = y.reshape(-1)  # [batch_size * height * width]

    # Constants.
    batch_size, height, width, channels = im.shape

    x, y = x.float(), y.float()
    max_y = int(height - 1)
    max_x = int(width - 1)

    # Scale indices from [-1, 1] to [0, width - 1] or [0, height - 1].
    x = (x + 1.0) * (width - 1.0) / 2.0
    y = (y + 1.0) * (height - 1.0) / 2.0

    # Compute the coordinates of the 4 pixels to sample from.
    x0 = torch.floor(x).int()
    x1 = x0 + 1
    y0 = torch.floor(y).int()
    y1 = y0 + 1

    mask = (x0 >= 0) & (x1 <= max_x) & (y0 >= 0) & (y0 <= max_y)
    mask = mask.float()

    x0 = torch.clamp(x0, 0, max_x)
    x1 = torch.clamp(x1, 0, max_x)
    y0 = torch.clamp(y0, 0, max_y)
    y1 = torch.clamp(y1, 0, max_y)
    dim2 = width
    dim1 = width * height

    # Create base index.
    base = torch.arange(batch_size) * dim1
    base = base.reshape(-1, 1)
    base = base.repeat(1, height * width)
    base = base.reshape(-1)  # [batch_size * height * width]
    # base = base.long().to(device)
    base = base.long().cuda()

    base_y0 = base + y0.long() * dim2
    base_y1 = base + y1.long() * dim2
    idx_a = base_y0 + x0.long()
    idx_b = base_y1 + x0.long()
    idx_c = base_y0 + x1.long()
    idx_d = base_y1 + x1.long()

    # Use indices to lookup pixels in the flat image and restore channels dim.
    im_flat = im.reshape(-1, channels).float()  # [batch_size * height * width, channels]
    pixel_a = im_flat[idx_a]
    pixel_b = im_flat[idx_b]
    pixel_c = im_flat[idx_c]
    pixel_d = im_flat[idx_d]

    wa = (x1.float() - x) * (y1.float() - y)
    wb = (x1.float() - x) * (1.0 - (y1.float() - y))
    wc = (1.0 - (x1.float() - x)) * (y1.float() - y)
    wd = (1.0 - (x1.float() - x)) * (1.0 - (y1.float() - y))
    wa, wb, wc, wd = wa.unsqueeze(1), wb.unsqueeze(1), wc.unsqueeze(1), wd.unsqueeze(1)

    output = wa * pixel_a + wb * pixel_b + wc * pixel_c + wd * pixel_d
    output = output.reshape(batch_size, height, width, channels)
    mask = mask.reshape(batch_size, height, width, 1)
    return output, mask

def adjust_w_icc(epoch_idx, w_icc, max_w_icc):
    if epoch_idx >= 2 - 1:   # 2
        w_icc *= 2

    if epoch_idx >= 4 - 1:   # 4
        w_icc *= 2

    if epoch_idx >= 6 - 1:   # 6
        w_icc *= 2

    if epoch_idx >= 8 - 1:   # 8
        w_icc *= 2

    if epoch_idx >= 10 - 1:  # 0.32
        w_icc *= 2
    w_icc = min(w_icc, max_w_icc)

    return w_icc

def random_image_mask(img, filter_size):
    '''
    :param img: [B x 3 x H x W]
    :param crop_size:
    :return:
    '''
    fh, fw = filter_size
    _, _, h, w = img.size()

    if fh == h and fw == w:
        return img, None

    x = np.random.randint(0, w - fw)
    y = np.random.randint(0, h - fh)
    filter_mask = torch.ones_like(img)    # B x 3 x H x W
    filter_mask[:, :, y:y+fh, x:x+fw] = 0.0    # B x 3 x H x W
    img = img * filter_mask    # B x 3 x H x W
    return img, filter_mask

import torchvision
import torch
import torch.nn as nn
import numpy as np
from torch.autograd import Variable

"""
医学图像分割已通过 Transformer 模型取得了显著的进步，该模型擅长掌握深远的上下文和全局上下文信息。然而，这些模型的计算需求与 token 数量的平方成正比，限制了它们的深度和分辨率能力。
大多数当前方法都是逐片（称为伪 3D）处理 3D 体积图像数据，缺少关键的切片间信息，从而降低了模型的整体性能。
为了应对这些挑战，我们引入了可变形大核注意力 (D-LKA Attention) 的概念，这是一种精简的注意力机制，采用大型卷积核来充分理解体积上下文。该机制在类似于自注意力的接受场内运行，同时避开了计算开销。
此外，我们提出的注意力机制受益于可变形卷积，可以灵活地扭曲采样网格，使模型能够适当地适应不同的数据模式。我们设计了 D-LKA Attention 的 2D 和 3D 改编版，后者在跨深度数据理解方面表现出色。
"""


def flatten(a):
    return a.contiguous().view(a.nelement())


def repeat(a, repeats, axis=0):
    assert len(a.size()) == 1
    return flatten(torch.transpose(a.repeat(repeats, 1), 0, 1))


def batch_map_coordinates(input, coords, order=1):
    """Interpolate (trilinear) input values given coordinates
    Notations:
        l - left
        r - right
        t - top
        b - bottom
        a - anterior (front)
        p - posterior (back)

    ltp------rtp
    |\        |\
    | lta------rta
    | |       | |
    lbp------rbp|
     \|        \|
      lba------rba

    """

    batch_size = input.size(0)
    input_depth = input.size(1)
    input_height = input.size(2)
    input_width = input.size(3)

    n_coords = coords.size(1)

    coords = torch.cat((
        torch.clamp(coords.narrow(2, 0, 1), 0, input_depth - 1),
        torch.clamp(coords.narrow(2, 1, 1), 0, input_height - 1),
        torch.clamp(coords.narrow(2, 2, 1), 0, input_width - 1)), 2)

    assert (coords.size(1) == n_coords)

    coords_lta = coords.floor().long()
    coords_rbp = coords.ceil().long()

    coords_ltp = torch.stack([coords_lta[..., 0], coords_lta[..., 1], coords_rbp[..., 2]], 2)
    coords_rtp = torch.stack([coords_rbp[..., 0], coords_lta[..., 1], coords_rbp[..., 2]], 2)
    coords_rta = torch.stack([coords_rbp[..., 0], coords_lta[..., 1], coords_lta[..., 2]], 2)
    coords_lba = torch.stack([coords_lta[..., 0], coords_rbp[..., 1], coords_lta[..., 2]], 2)
    coords_lbp = torch.stack([coords_lta[..., 0], coords_rbp[..., 1], coords_rbp[..., 2]], 2)
    coords_rba = torch.stack([coords_rbp[..., 0], coords_rbp[..., 1], coords_lta[..., 2]], 2)

    idx = repeat(torch.arange(0, batch_size), n_coords).long()
    idx = Variable(idx, requires_grad=False)
    if input.is_cuda:
        idx = idx.cuda()

    def _get_vals_by_coords(input, coords):
        indices = torch.stack([
            idx, flatten(coords[..., 0]), flatten(coords[..., 1]), flatten(coords[..., 2])
        ], 1)
        inds = indices[:, 0] * input.size(1) * input.size(2) * input.size(3) \
               + indices[:, 1] * input.size(2) * input.size(3) + indices[:, 2] * input.size(3) + indices[:, 3]

        vals = flatten(input).index_select(0, inds)
        vals = vals.view(batch_size, n_coords)
        return vals

    vals_lta = _get_vals_by_coords(input, coords_lta.detach())
    vals_rbp = _get_vals_by_coords(input, coords_rbp.detach())
    vals_ltp = _get_vals_by_coords(input, coords_ltp.detach())
    vals_rtp = _get_vals_by_coords(input, coords_rtp.detach())
    vals_rta = _get_vals_by_coords(input, coords_rta.detach())
    vals_lba = _get_vals_by_coords(input, coords_lba.detach())
    vals_lbp = _get_vals_by_coords(input, coords_lbp.detach())
    vals_rba = _get_vals_by_coords(input, coords_rba.detach())

    # trilinear interpolation
    # https://en.wikipedia.org/wiki/Trilinear_interpolation
    coords_offset_lta = coords - coords_lta.type(coords.data.type())
    coords_offset_rbp = coords - coords_rbp.type(coords.data.type())

    vals_ta = coords_offset_lta[..., 0] * (vals_rta - vals_lta) + vals_lta
    vals_ba = coords_offset_lta[..., 0] * (vals_rba - vals_lba) + vals_lba

    vals_tp = coords_offset_rbp[..., 0] * (vals_rtp - vals_ltp) + vals_ltp
    vals_bp = coords_offset_rbp[..., 0] * (vals_rbp - vals_lbp) + vals_lbp

    # interpolate top
    vals_t = coords_offset_lta[..., 2] * (vals_tp - vals_ta) + vals_ta

    # interpolate bottom
    vals_b = coords_offset_rbp[..., 2] * (vals_bp - vals_ba) + vals_ba

    mapped_vals = coords_offset_lta[..., 1] * (vals_b - vals_t) + vals_t
    return mapped_vals


def generate_grid(batch_size, input_depth, input_height, input_width, dtype, cuda):
    """Generate grid for coordinates of the input
    Parameters
    ---------
    batch_size : int
    input_depth : int
    input_height : int
    input_width : int
    dtype : torch.dtype
    cuda : boolean

    Returns
    -------
    torch.Tensor. shape = (b, d*h*w, 3)
    """
    grid = np.meshgrid(
        range(input_depth), range(input_height), range(input_width), indexing='ij'
    )
    grid = np.stack(grid, axis=-1)
    grid = grid.reshape(-1, 3)

    grid = np.expand_dims(grid, 0)
    grid = np.tile(grid, [batch_size, 1, 1])

    grid = torch.from_numpy(grid).type(dtype)
    if cuda:
        grid = grid.cuda()
    return Variable(grid, requires_grad=False)


def batch_map_offsets(input, offsets, grid=None, order=1):
    """(Batch) map offsets into input
    Parameters
    ---------
    input : torch.Tensor. shape = (b, s, s, s)
    offsets: torch.Tensor. shape = (b, s, s, s, 3)

    Returns
    -------
    torch.Tensor. shape = (b, s, s, s)
    """
    batch_size = input.size(0)
    input_depth = input.size(1)
    input_height = input.size(2)
    input_width = input.size(3)

    offsets = offsets.view(batch_size, -1, 3)

    if grid is None:
        grid = generate_grid(batch_size,
                             input_depth,
                             input_height,
                             input_width,
                             offsets.data.type(),
                             offsets.data.is_cuda)

    coords = offsets + grid

    mapped_vals = batch_map_coordinates(input, coords)
    return mapped_vals


class ConvOffset3D(nn.Conv3d):
    """Convolutional layer responsible for learning the 3D offsets and output the
    deformed feature map using trilinear interpolation. This layer does not perform
    convolution on the deformed map.
    """

    def __init__(self, in_channels, init_normal_stddev=0.01, **kwargs):
        """Init
        Parameters
        ----------
        filters : int
            Number of channel of the input feature map
        init_normal_stddev : float
            Normal kernel initialization
        **kwargs:
            Pass to superclass. See Con2d layer in pytorch
        """
        self.filters = in_channels
        self._grid_param = None
        super(ConvOffset3D, self).__init__(self.filters, self.filters * 3, 3, padding=1, bias=False, **kwargs)
        self.weight.data.copy_(self._init_weights(self.weight, init_normal_stddev))

    def forward(self, x):
        x_shape = x.size()
        offsets = super(ConvOffset3D, self).forward(x)

        # offsets: (b*c, d, h, w, 3)
        offsets = self._to_bc_d_h_w_3(offsets, x_shape)

        # x: (b*c, d, h, w)
        x = self._to_bc_d_h_w(x, x_shape)

        # X_offset: (b*c, d, h, w)
        x_offset = batch_map_offsets(x, offsets, grid=self._get_grid(self, x))

        # x_offset: (b, d, h, w, c)
        x_offset = self._to_b_c_d_h_w(x_offset, x_shape)

        return x_offset

    @staticmethod
    def _get_grid(self, x):
        batch_size, input_depth, input_height, input_width = x.size(0), x.size(1), x.size(2), x.size(3)
        dtype, cuda = x.data.type(), x.data.is_cuda
        if self._grid_param == (batch_size, input_depth, input_height, input_width, dtype, cuda):
            return self._grid
        self._grid_param = (batch_size, input_depth, input_height, input_width, dtype, cuda)
        self._grid = generate_grid(batch_size, input_depth, input_height, input_width, dtype, cuda)
        return self._grid

    @staticmethod
    def _init_weights(weights, std):
        fan_out = weights.size(0)
        fan_in = weights.size(1) * weights.size(2) * weights.size(3) * weights.size(4)
        w = np.random.normal(0.0, std, (fan_out, fan_in))
        return torch.from_numpy(w.reshape(weights.size()))

    @staticmethod
    def _to_bc_d_h_w_3(x, x_shape):
        """(b, c, d, h, w) -> (b*c, d, h, w, 3)"""
        x = x.contiguous().view(-1, int(x_shape[2]), int(x_shape[3]), int(x_shape[4]), 3)
        return x

    @staticmethod
    def _to_bc_d_h_w(x, x_shape):
        """(b, c, d, h, w) -> (b*c, d, h, w)"""
        x = x.contiguous().view(-1, int(x_shape[2]), int(x_shape[3]), int(x_shape[4]))
        return x

    @staticmethod
    def _to_b_c_d_h_w(x, x_shape):
        """(b*c, d, h, w) -> (b, c, d, h, w)"""
        x = x.contiguous().view(-1, int(x_shape[1]), int(x_shape[2]), int(x_shape[3]), int(x_shape[4]))
        return x


def deform_conv3d(in_c, out_c, kernel_size, **kwargs):
    """Deformable convolution layer: convolution + offset"""
    return nn.Sequential(
        ConvOffset3D(out_c),
        nn.Conv3d(in_c, out_c, kernel_size, **kwargs)

    )

class DeformConv3d(nn.Module):  # TODO: 3d version of this

    def __init__(self, in_channels, groups, kernel_size=(3, 3), padding=1, stride=1, dilation=1, bias=True):
        super(DeformConv3d, self).__init__()

        self.offset_net = nn.Conv2d(in_channels=in_channels,
                                    out_channels=2 * kernel_size[0] * kernel_size[1],
                                    kernel_size=3,
                                    padding=1,
                                    stride=1,
                                    bias=True)

        self.deform_conv = torchvision.ops.DeformConv2d(in_channels=in_channels,  # 3d version of this?
                                                        out_channels=in_channels,
                                                        kernel_size=kernel_size,
                                                        padding=padding,
                                                        groups=groups,
                                                        stride=stride,
                                                        dilation=dilation,
                                                        bias=False)

    def forward(self, x):
        offsets = self.offset_net(x)
        out = self.deform_conv(x, offsets)
        return out


class LKA3d(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.conv0 = nn.Conv3d(dim, dim, 5, padding=2, groups=dim)
        self.conv_spatial = nn.Conv3d(dim, dim, 7, stride=1, padding=9, groups=dim, dilation=3)
        self.conv1 = nn.Conv3d(dim, dim, 1)

    def forward(self, x):
        u = x.clone()
        attn = self.conv0(x)
        attn = self.conv_spatial(attn)
        attn = self.conv1(attn)

        return u * attn


class LKA_Attention3d(nn.Module):
    def __init__(self, d_model):
        super().__init__()

        self.proj_1 = nn.Conv3d(d_model, d_model, 1)
        self.activation = nn.GELU()
        self.spatial_gating_unit = LKA3d(d_model)
        self.proj_2 = nn.Conv3d(d_model, d_model, 1)

    def forward(self, x):
        shortcut = x.clone()
        x = self.proj_1(x)
        x = self.activation(x)
        x = self.spatial_gating_unit(x)
        x = self.proj_2(x)
        x = x + shortcut
        return x