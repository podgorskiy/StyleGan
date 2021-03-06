# Copyright 2019 Stanislav Pidhorskyi
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import os
import sys
import argparse
import logging
import torch
from defaults import get_cfg_defaults
import dnnlib
import dnnlib.tflib
import dnnlib.tflib as tflib
import pickle
from model import Model
#import stylegan
#import stylegan.training.networks_stylegan
import numpy as np
from torchvision.utils import save_image
import PIL
from dnnlib import EasyDict
from checkpointer import Checkpointer


def save_sample(model, sample):
    with torch.no_grad():
        model.eval()
        x_rec = model.generate(model.generator.layer_count - 1, 1, z=sample)

        def save_pic(x_rec):
            resultsample = x_rec * 0.5 + 0.5
            resultsample = resultsample.cpu()
            save_image(resultsample,
                       'sample.png', nrow=16)

        save_pic(x_rec)


def load_from(name, cfg):
    dnnlib.tflib.init_tf()
    with open(name, 'rb') as f:
        m = pickle.load(f)

    Gs = m[2]

    #Gs_ = tflib.Network('G', func_name='stylegan.training.networks_stylegan.G_style', num_channels=3, resolution=1024)
    #D = tflib.Network('D', func_name='stylegan.training.networks_stylegan.D_basic', num_channels=3, resolution=1024)

    #Gs_.copy_vars_from(Gs)

    model = Model(
        startf=cfg.MODEL.START_CHANNEL_COUNT,
        layer_count= cfg.MODEL.LAYER_COUNT,
        maxf=cfg.MODEL.MAX_CHANNEL_COUNT,
        latent_size=cfg.MODEL.LATENT_SPACE_SIZE,
        mapping_layers=cfg.MODEL.MAPPING_LAYERS,
        truncation_psi=0.7, #cfg.MODEL.TRUNCATIOM_PSI,
        channels=3)

    def tensor(x, transpose=None):
        x = Gs.vars[x].eval()
        if transpose:
            x = np.transpose(x, transpose)
        return torch.tensor(x)

    for i in range(cfg.MODEL.MAPPING_LAYERS):
        block = getattr(model.mapping, "block_%d" % (i + 1))
        block.fc.weight[:] = tensor('G_mapping/Dense%d/weight' % i, (1, 0))
        block.fc.bias[:] = tensor('G_mapping/Dense%d/bias' % i)

    model.dlatent_avg.buff[:] = tensor('dlatent_avg')
    model.generator.const[:] = tensor('G_synthesis/4x4/Const/const')

    for i in range(model.generator.layer_count):
        j = model.discriminator.layer_count - i - 1
        name = '%dx%d' % (2 ** (2 + i), 2 ** (2 + i))
        block = model.generator.decode_block[i]

        prefix = 'G_synthesis/%s' % name

        if not block.has_first_conv:
            prefix_1 = '%s/Const' % prefix
            prefix_2 = '%s/Conv' % prefix
        else:
            prefix_1 = '%s/Conv0_up' % prefix
            prefix_2 = '%s/Conv1' % prefix

        block.noise_weight_1[0, :, 0, 0] = tensor('%s/Noise/weight' % prefix_1)
        block.noise_weight_2[0, :, 0, 0] = tensor('%s/Noise/weight' % prefix_2)

        if block.has_first_conv:
            if block.fused_scale:
                block.conv_1.weight[:] = tensor('%s/weight' % prefix_1, (2, 3, 0, 1))
            else:
                block.conv_1.weight[:] = tensor('%s/weight' % prefix_1, (3, 2, 0, 1))

        block.conv_2.weight[:] = tensor('%s/weight' % prefix_2, (3, 2, 0, 1))
        block.bias_1[0, :, 0, 0] = tensor('%s/bias' % prefix_1)
        block.bias_2[0, :, 0, 0] = tensor('%s/bias' % prefix_2)
        block.style_1.weight[:] = tensor('%s/StyleMod/weight' % prefix_1, (1, 0))
        block.style_1.bias[:] = tensor('%s/StyleMod/bias' % prefix_1)
        block.style_2.weight[:] = tensor('%s/StyleMod/weight' % prefix_2, (1, 0))
        block.style_2.bias[:] = tensor('%s/StyleMod/bias' % prefix_2)

        model.generator.to_rgb[i].to_rgb.weight[:] = tensor('G_synthesis/ToRGB_lod%d/weight' % (j), (3, 2, 0, 1))
        model.generator.to_rgb[i].to_rgb.bias[:] = tensor('G_synthesis/ToRGB_lod%d/bias' % (j))

    def tensor(x, transpose=None):
        x = m[1].vars[x].eval()
        if transpose:
            x = np.transpose(x, transpose)
        return torch.tensor(x)

    for i in range(model.discriminator.layer_count):
        j = model.discriminator.layer_count - i - 1
        prefix = '%dx%d' % (2 ** (2 + j), 2 ** (2 + j))
        block = model.discriminator.encode_block[i]

        if not block.last:
            prefix_1 = '%s/Conv0' % prefix
            prefix_2 = '%s/Conv1_down' % prefix
        else:
            prefix_1 = '%s/Conv' % prefix
            prefix_2 = '%s/Dense0' % prefix

        block.conv_1.weight[:] = tensor('%s/weight' % prefix_1, (3, 2, 0, 1))

        if not block.last:
            block.conv_2.weight[:] = tensor('%s/weight' % prefix_2, (3, 2, 0, 1))
        else:
            block.dense.weight[:] = tensor('%s/weight' % prefix_2, (1, 0))

        block.bias_1[0, :, 0, 0] = tensor('%s/bias' % prefix_1)
        block.bias_2[0, :, 0, 0] = tensor('%s/bias' % prefix_2)

        model.discriminator.from_rgb[j].from_rgb.weight[:] = tensor('FromRGB_lod%d/weight' % (j), (3, 2, 0, 1))
        model.discriminator.from_rgb[j].from_rgb.bias[:] = tensor('FromRGB_lod%d/bias' % (j))

    model.discriminator.fc2.weight[:] = tensor('4x4/Dense1/weight', (1, 0))
    model.discriminator.fc2.bias[:] = tensor('4x4/Dense1/bias')
    return model #, Gs_


def train_net(args):
    torch.cuda.set_device(0)

    cfg = get_cfg_defaults()
    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    cfg.freeze()

    logger = logging.getLogger("logger")
    logger.setLevel(logging.DEBUG)

    output_dir = cfg.OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)

    ch = logging.StreamHandler(stream=sys.stdout)
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    logger.info(args)

    logger.info("Loaded configuration file {}".format(args.config_file))
    with open(args.config_file, "r") as cf:
        config_str = "\n" + cf.read()
        logger.info(config_str)
    logger.info("Running with config:\n{}".format(cfg))

    torch.set_default_tensor_type('torch.cuda.FloatTensor')

    model = load_from('karras2019stylegan-ffhq-1024x1024.pkl', cfg)
    #model, Gs = load_from('karras2019stylegan-ffhq-1024x1024.pkl', cfg)

    # Generate image.
    #fmt = dict(func=tflib.convert_images_to_uint8, nchw_to_nhwc=True)
    #images = Gs.run(sample.cpu().detach().numpy(), None, truncation_psi=0.7, randomize_noise=True, output_transform=None)

    rnd = np.random.RandomState(5)
    latents = rnd.randn(1, cfg.MODEL.LATENT_SPACE_SIZE)
    sample = torch.tensor(latents).float().cuda()
    save_sample(model, sample, )

    #png_filename = os.path.join('example.png')
    #PIL.Image.fromarray(images[0], 'RGB').save(png_filename)


    model_dict = {
        'generator_s': model.generator,
        'mapping_s': model.mapping,
        'dlatent_avg': model.dlatent_avg,
    }

    checkpointer = Checkpointer(cfg,
                                model_dict,
                                logger=logger,
                                save=True)

    checkpointer.save('karras2019stylegan-ffhq')


def run():
    parser = argparse.ArgumentParser(description="Adversarial, hierarchical style VAE")
    parser.add_argument(
        "--config-file",
        default="configs/experiment_stylegan.yaml",
        metavar="FILE",
        help="path to config file",
        type=str,
    )
    parser.add_argument(
        "opts",
        help="Modify config options using the command-line",
        default=None,
        nargs=argparse.REMAINDER,
    )

    args = parser.parse_args()

    train_net(args)


if __name__ == '__main__':
    run()
