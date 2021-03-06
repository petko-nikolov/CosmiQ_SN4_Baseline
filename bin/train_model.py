import os
import argparse
import warnings
import numpy as np

parser = argparse.ArgumentParser(description='Train the baseline model.')
parser.add_argument('--data_path', '-d', type=str, default='',
                    help='Path to the directory containing the `train` and ' +
                    '`val` data folders. Defaults to the current working ' +
                    'directory. If using `--data_format files`, this must ' +
                    'be a directory which contains the rgb files (or ' +
                    'subdirs whose only .tif-format contents are rgb .tifs)')
parser.add_argument('--mask_path', '-k', type=str, default='',
                    help='Path to the directory containing the `masks` data ' +
                    'folder. If using `--data_format files`, this must be ' +
                    'the directory containing the masks. If not passed, it ' +
                    'is assumed that `data_path` and `mask_path` are the same.'
                    )
parser.add_argument('--data_format', '-f', type=str, default='array',
                    help='Is data stored in a NumPy array (default) or as ' +
                    'image files? To use image files, pass `files` here. ' +
                    'Currently only supports 8-bit RGB TIFFs as image files.' +
                    'If passing files, --data_path must point to the' +
                    ' train_rgb directory (or its equivalent).')
parser.add_argument('--recursive', '-r', action='store_const', const=True,
                    default=False,
                    help='Recursively traverse subdirs in the ')
parser.add_argument('--output_path', '-o', type=str, default='model.hdf5',
                    help='Path for saving trained model. ' +
                    'Defaults to model.hdf5 in the working directory.')
parser.add_argument('--subset', '-s', type=str, default='all',
                    help='Data to train the model on. Options are ' +
                    '`all`, `nadir`, `offnadir`, or `faroffnadir`.')
parser.add_argument('--seed', '-e', type=int, default=42,
                    help='Randomization seed for initialization and datagen.')
parser.add_argument('--model', '-m', type=str, default='ternausnetv1',
                    help='Model architecture. Either `ternausnetv1` or `unet`.' +
                    ' See cosmiq_sn4_baseline.model for architecture details.')
parser.add_argument('--tensorboard_dir', '-t', type=str, default='',
                    help='Path to save logs for TensorBoard. ' +
                    'If not provided, no TensorBoard logs are saved.')

args = parser.parse_args()

# set random seed for numpy and tensorflow
np.random.seed(args.seed)
import tensorflow as tf
tf.set_random_seed(args.seed)
from keras.callbacks import ModelCheckpoint, TensorBoard, EarlyStopping
from keras.callbacks import ReduceLROnPlateau
from cosmiq_sn4_baseline.DataGenerator import FlatDataGenerator, FileDataGenerator
from cosmiq_sn4_baseline.DataGenerator import get_files_recursively
from cosmiq_sn4_baseline.callbacks import TerminateOnMetricNaN
from cosmiq_sn4_baseline.losses import hybrid_bce_jaccard
from cosmiq_sn4_baseline.metrics import precision, recall
from cosmiq_sn4_baseline.models import compile_model
import cosmiq_sn4_baseline as space_base


def main(dataset, model='ternausnetv1', data_path='', mask_path='',
         recursive=False, output_path='model.hdf5', tb_dir='',
         data_format='array'):

    # create a few variables needed later.
    output_dir, model_name = os.path.split(output_path)
    tmp_model_path = output_path.rstrip('.hdf5') + '_ckpt_best.hdf5'
    tmp_weights_path = os.path.join(output_dir, 'tmp_weights.h5')
    if not mask_path:
        mask_path = data_path

    # make sure everything is clean to start:
    if os.path.exists(tmp_model_path):
        warnings.warn('Temp file {} existed before starting. Deleted.'.format(
            tmp_model_path))
        os.remove(tmp_model_path)
    if os.path.exists(tmp_weights_path):
        warnings.warn('Temp file {} existed before starting. Deleted.'.format(
            tmp_weights_path))
        os.remove(tmp_weights_path)

    # specify the path to the training and validation data files
    train_im_path = os.path.join(data_path, 'train',
                                 dataset + '_train_ims.npy')
    val_im_path = os.path.join(data_path, 'validate',
                               dataset + '_val_ims.npy')
    train_mask_path = os.path.join(mask_path, 'train',
                                   dataset + '_train_masks.npy')
    val_mask_path = os.path.join(mask_path, 'validate',
                                 dataset + '_val_masks.npy')

    batch_size = 4
    early_stopping_patience = 15
    model_args = {
        'optimizer': 'Nadam',
        'input_shape': (512, 512, 3),
        'base_depth': 64,
        'lr': 0.0002
    }
    # reduce base_depth to 32 if using vanilla unet
    if model == 'unet':
        model_args['base_depth'] = 32

    if data_format == 'array':
        # load in data. don't read entirely into memory - too big.
        train_im_arr = np.load(train_im_path, mmap_mode='r')
        val_im_arr = np.load(val_im_path, mmap_mode='r')
        train_mask_arr = np.load(train_mask_path, mmap_mode='r')
        val_mask_arr = np.load(val_mask_path, mmap_mode='r')

        # create generators for training and validation
        training_gen = FlatDataGenerator(
            train_im_arr, train_mask_arr, batch_size=batch_size, crop=True,
            output_x=model_args['input_shape'][1],
            output_y=model_args['input_shape'][0],
            flip_x=True, flip_y=True, rotate=True
            )
        validation_gen = FlatDataGenerator(
            val_im_arr, val_mask_arr, batch_size=batch_size, crop=True,
            output_x=model_args['input_shape'][1],
            output_y=model_args['input_shape'][0]
            )
        n_train_ims = train_im_arr.shape[0]
        n_val_ims = val_im_arr.shape[0]
    elif data_format == 'files':
        unique_chips = [f.lstrip('mask_').rstrip('.tif')
                        for f in os.listdir(mask_path) if f.endswith('.tif')]
        np.random.shuffle(unique_chips)
        number_train_chips = int(len(unique_chips)*0.8)
        train_chips = unique_chips[:number_train_chips]
        val_chips = unique_chips[number_train_chips:]
        im_fnames = get_files_recursively(data_path,
                                          traverse_subdirs=recursive)
        if dataset != 'all':
            if dataset == 'nadir':
                collect_subset = space_base.COLLECTS[0:11]
            elif dataset == 'offnadir':
                collect_subset = space_base.COLLECTS[11:18]
            elif dataset == 'faroffnadir':
                collect_subset = space_base.COLLECTS[18:]
            im_fnames = [f for f in im_fnames if
                         any(c in f for c in collect_subset)]
            print(collect_subset)
        n_ims = len(im_fnames)
        print('n_ims: {}'.format(n_ims))
        n_train_ims = np.floor(n_ims*0.8)
        n_val_ims = np.floor(n_ims*0.2)

        training_gen = FileDataGenerator(
            im_fnames, mask_path, (900, 900, 3), chip_subset=train_chips,
            batch_size=batch_size, crop=True, traverse_subdirs=recursive,
            output_x=model_args['input_shape'][1],
            output_y=model_args['input_shape'][0],
            flip_x=True, flip_y=True, rotate=True)
        validation_gen = FileDataGenerator(
            im_fnames, mask_path, (900, 900, 3), chip_subset=val_chips,
            batch_size=batch_size, crop=True, traverse_subdirs=recursive,
            output_x=model_args['input_shape'][1],
            output_y=model_args['input_shape'][0])
    monitor = 'val_loss'
    print()
    print("<><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>")
    print("                 BEGINNING MODEL TRAINING")
    print("                 MODEL ARCHITECTURE: {}".format(model))
    print("                   OPTIMIZER: {}".format(model_args['optimizer']))
    print("                     DATASET: {}".format(dataset))
    print("                 INPUT SHAPE: {}".format(model_args['input_shape']))
    print("                      BATCH SIZE: {}".format(batch_size))
    print("                   LEARNING RATE: {}".format(model_args['lr']))
    print("<><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>")
    print()

    callbax = []
    callbax.append(ReduceLROnPlateau(factor=0.2, patience=3, verbose=1,
                                     min_delta=0.01))
    callbax.append(ModelCheckpoint(tmp_model_path, monitor=monitor,
                                   save_best_only=True))
    callbax.append(TerminateOnMetricNaN('precision'))
    callbax.append(EarlyStopping(monitor=monitor,
                                 patience=early_stopping_patience,
                                 mode='auto'))
    if tb_dir:  # if saving tensorboard logs
        callbax.append(TensorBoard(
            log_dir=os.path.join(tb_dir, model_name)))
    lf = hybrid_bce_jaccard
    am = [precision,
          recall]
    model = compile_model(arch=model, loss_func=lf,
                          additional_metrics=am,
                          verbose=True, **model_args)
    model.fit_generator(
        training_gen, validation_data=validation_gen,
        validation_steps=np.floor(n_val_ims/batch_size),
        steps_per_epoch=np.floor(n_train_ims/batch_size),
        epochs=1000, callbacks=callbax
        )
    model.save(output_path)
    print("<><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>")
    print("                   MODEL TRAINING COMPLETE!                 ")
    print("   Model located at {}".format(output_path))
    print("<><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>")


if __name__ == '__main__':
    main(args.subset, model=args.model, data_path=args.data_path,
         recursive=args.recursive, mask_path=args.mask_path,
         output_path=args.output_path, tb_dir=args.tensorboard_dir,
         data_format=args.data_format)
