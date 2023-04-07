"""
Module Name: stardistwrapper.py
Author: Chentao Wen
Modification Description: This module is a wrapper of 3D StarDist modified according to 2_training.ipynb in GitHub StarDist repository
"""


from __future__ import print_function, unicode_literals, absolute_import, division

import sys
from typing import List

import matplotlib
import numpy as np
from numpy import ndarray
from stardist.utils import _normalize_grid

UP_LIMIT = 400000
matplotlib.rcParams["image.interpolation"] = None
import matplotlib.pyplot as plt

from glob import glob
from tqdm import tqdm
from tifffile import imread
from csbdeep.utils import Path, normalize

from stardist import fill_label_holes, random_label_cmap, calculate_extents, gputools_available
from stardist import Rays_GoldenSpiral
from stardist.models import Config3D, StarDist3D

np.random.seed(42)
lbl_cmap = random_label_cmap()


def load_images(path_train_images: str, path_train_labels: str, max_projection: bool):
    X = sorted(glob(path_train_images))
    Y = sorted(glob(path_train_labels))
    assert len(X) > 0 and len(Y) > 0
    assert all(Path(x).name == Path(y).name for x, y in zip(X,Y))
    X = list(map(imread, X))
    Y = list(map(imread, Y))
    n_channel = 1 if X[0].ndim == 3 else X[0].shape[-1]
    axis_norm = (0, 1, 2)  # normalize channels independently
    # axis_norm = (0,1,2,3) # normalize channels jointly
    if n_channel > 1:
        print(
            "Normalizing image channels %s." % ('jointly' if axis_norm is None or 3 in axis_norm else 'independently'))
        sys.stdout.flush()

    X = [normalize(x, 1, 99.8, axis=axis_norm) for x in tqdm(X)]
    Y = [fill_label_holes(y) for y in tqdm(Y)]
    if len(X) == 1:
        print(
            "Warning: only one training data was provided! It will be used for both training and validation purposes!")
        X = [X[0], X[0]]
        Y = [Y[0], Y[0]]
    rng = np.random.RandomState(42)
    ind = rng.permutation(len(X))
    n_val = max(1, int(round(0.15 * len(ind))))
    ind_train, ind_val = ind[:-n_val], ind[-n_val:]
    X_val, Y_val = [X[i] for i in ind_val], [Y[i] for i in ind_val]
    X_trn, Y_trn = [X[i] for i in ind_train], [Y[i] for i in ind_train]
    print('number of images: %3d' % len(X))
    print('- training:       %3d' % len(X_trn))
    print('- validation:     %3d' % len(X_val))
    print(f"{X[0].shape=}")
    i = 0
    img, lbl = X[i], Y[i]
    assert img.ndim in (3, 4)
    img = img if img.ndim == 3 else img[..., :3]
    if max_projection:
        plot_img_label_max_projection(img, lbl)
    else:
        plot_img_label_center_slice(img, lbl)

    return X, Y, X_trn, Y_trn, X_val, Y_val, n_channel


def configure(Y: List[ndarray], n_channel: int, up_limit: int = UP_LIMIT):
    extents = calculate_extents(Y)
    anisotropy = tuple(np.max(extents) / extents)
    print('empirical anisotropy of labeled objects = %s' % str(anisotropy))

    # 96 is a good default choice (see 1_data.ipynb)
    n_rays = 96

    # Use OpenCL-based computations for data generator during training (requires 'gputools')
    use_gpu = False and gputools_available()

    # Predict on subsampled grid for increased efficiency and larger field of view
    grid = tuple(1 if a > 1.5 else 2 for a in anisotropy)

    # Use rays on a Fibonacci lattice adjusted for measured anisotropy of the training data
    rays = Rays_GoldenSpiral(n_rays, anisotropy=anisotropy)

    # Set train_patch_size which should
    # 1. match anisotropy and under a predefined limitation
    a, b, c = anisotropy
    train_patch_size = np.cbrt(up_limit * a * b * c) / np.array([a, b, c])
    # 2. less than the image size
    up_limit_xyz = Y[0].shape[0], np.min(Y[0].shape[1:3]), np.min(Y[0].shape[1:3])
    scaling = np.min(np.asarray(up_limit_xyz) / train_patch_size)
    if scaling < 1:
        train_patch_size = train_patch_size * scaling
    # 3. can be divided by div_by (related to unet architecture)
    unet_n_depth = 2 #
    grid_norm = _normalize_grid(grid, 3)
    unet_pool = 2,2,2
    div_by = tuple(p ** unet_n_depth * g for p, g in zip(unet_pool, grid_norm))
    print(f"{div_by=}")
    train_patch_size = [int(d*(i//d)) for i, d in zip(train_patch_size, div_by)]
    # 4. size of x and y should be the same (since augmentation will flip x-y axes)
    train_patch_size[1] = train_patch_size[2] = min(train_patch_size[1:])

    conf = Config3D(
        rays=rays,
        grid=grid,
        anisotropy=anisotropy,
        use_gpu=use_gpu,
        n_channel_in=n_channel,
        # adjust for your data below (make patch size as large as possible)
        train_patch_size=train_patch_size,
        train_batch_size=2,
    )
    print_dict(vars(conf))
    assert conf.unet_n_depth == unet_n_depth
    assert conf.grid == grid_norm
    assert conf.unet_pool == unet_pool

    if use_gpu:
        from csbdeep.utils.tf import limit_gpu_memory
        # adjust as necessary: limit GPU memory to be used by TensorFlow to leave some to OpenCL-based computations
        limit_gpu_memory(0.8)
        # alternatively, try this:
        # limit_gpu_memory(None, allow_growth=True)

    model = StarDist3D(conf, name='stardist', basedir='models')

    median_size = calculate_extents(Y, np.median)
    fov = np.array(model._axes_tile_overlap('ZYX'))
    print(f"median object size:      {median_size}")
    print(f"network field of view :  {fov}")
    if any(median_size > fov):
        print("WARNING: median object size larger than field of view of the neural network.")

    return model


def print_dict(my_dict: dict):
    for key, value in my_dict.items():
        print(f"{key}: {value}")


def plot_img_label_center_slice(img, lbl, img_title="image (XY slice)", lbl_title="label (XY slice)", z=None):
    if z is None:
        z = img.shape[0] // 2
    fig, (ai,al) = plt.subplots(1,2, figsize=(15,7), gridspec_kw=dict(width_ratios=(1.25,1)))
    im = ai.imshow(img[z], cmap='gray', clim=(0,1))
    ai.set_title(img_title)
    fig.colorbar(im, ax=ai)
    al.imshow(lbl[z], cmap=lbl_cmap)
    al.set_title(lbl_title)
    plt.tight_layout()


def plot_img_label_max_projection(img, lbl, img_title="image (max projection)", lbl_title="label (max projection)"):
    fig, (ai,al) = plt.subplots(1,2, figsize=(15, 7), gridspec_kw=dict(width_ratios=(1.25,1)))
    im = ai.imshow(img.max(axis=0), cmap='gray', clim=(0,1), vmin=0, vmax=1)
    ai.set_title(img_title)
    fig.colorbar(im, ax=ai)
    al.imshow(lbl.max(axis=0), cmap=lbl_cmap)
    al.set_title(lbl_title)
    plt.tight_layout()


def random_fliprot(img, mask, axis=None):
    if axis is None:
        axis = tuple(range(mask.ndim))
    axis = tuple(axis)

    assert img.ndim >= mask.ndim
    perm = tuple(np.random.permutation(axis))
    transpose_axis = np.arange(mask.ndim)
    for a, p in zip(axis, perm):
        transpose_axis[a] = p
    transpose_axis = tuple(transpose_axis)
    img = img.transpose(transpose_axis + tuple(range(mask.ndim, img.ndim)))
    mask = mask.transpose(transpose_axis)
    for ax in axis:
        if np.random.rand() > 0.5:
            img = np.flip(img, axis=ax)
            mask = np.flip(mask, axis=ax)
    return img, mask


def random_intensity_change(img):
    img = img * np.random.uniform(0.6, 2) + np.random.uniform(-0.2, 0.2)
    return img


def augmenter(x, y):
    """Augmentation of a single input/label image pair.
    x is an input image
    y is the corresponding ground-truth label image
    """
    # Note that we only use fliprots along axis=(1,2), i.e. the yx axis
    # as 3D microscopy acquisitions are usually not axially symmetric
    x, y = random_fliprot(x, y, axis=(1, 2))
    x = random_intensity_change(x)
    return x, y