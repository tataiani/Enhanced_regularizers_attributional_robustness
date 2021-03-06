"""Trains a model, saving checkpoints and tensorboard summaries along
   the way."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import json
import os
import shutil
from timeit import default_timer as timer

import tensorflow as tf
import numpy as np
import re

import flower_input
from model import Model

from pgd_attack import LinfPGDAttack

os.environ["CUDA_VISIBLE_DEVICES"]="2"

with open('IG-SUM-NORM-config.json') as config_file:
    config = json.load(config_file)

# seeding randomness
tf.set_random_seed(config['tf_random_seed'])
np.random.seed(config['np_random_seed'])

# Setting up training parameters
max_num_training_steps = config['max_num_training_steps']
num_output_steps = config['num_output_steps']
num_summary_steps = config['num_summary_steps']
num_checkpoint_steps = config['num_checkpoint_steps']
step_size_schedule = config['step_size_schedule']
weight_decay = config['weight_decay']
data_path = config['data_path']
training_objective = config['training_objective']
batch_size = config['training_batch_size']
momentum = config['momentum']
m = config["m"]
lamb = config["lambda"]
approx_factor = config["approx_factor"]
continue_train = config["continue_train"]

# Setting up the data and the model
flower = flower_input.FlowerData(data_path)
global_step = tf.contrib.framework.get_or_create_global_step()
model = Model(mode = 'train', m = m, lamb = lamb, approx_factor = approx_factor)

# Setting up the optimizer
boundaries = [int(sss[0]) for sss in step_size_schedule]
boundaries = boundaries[1:]
values = [sss[1] for sss in step_size_schedule]
learning_rate = tf.train.piecewise_constant(
    tf.cast(global_step, tf.int32),
    boundaries,
    values)

if training_objective == 'ar':
    re_term = model.IG_regularization_term
    total_loss = model.loss_with_IG_regularizer + weight_decay * model.weight_decay_loss
elif training_objective == 'adv_ar':
    re_term = model.IG_regularization_term
    total_loss = model.adv_loss_with_IG_regularizer + weight_decay * model.weight_decay_loss
else:
    total_loss = model.mean_xent + weight_decay * model.weight_decay_loss

train_step = tf.train.MomentumOptimizer(learning_rate, momentum).minimize(
    total_loss,
    global_step=global_step)

# Setting up the Tensorboard and checkpoint outputs
model_dir = config['model_dir']
if not os.path.exists(model_dir):
  os.makedirs(model_dir)

# We add accuracy and xent twice so we can easily make three types of
# comparisons in Tensorboard:
# - train vs eval (for a single run)
# - train of different runs
# - eval of different runs

saver = tf.train.Saver(max_to_keep=30)
tf.summary.scalar('accuracy adv train', model.accuracy)
tf.summary.scalar('accuracy adv', model.accuracy)
tf.summary.scalar('xent adv train', model.xent / batch_size)
tf.summary.scalar('xent adv', model.xent / batch_size)
merged_summaries = tf.summary.merge_all()

# keep the configuration file with the model for reproducibility
shutil.copy('config.json', model_dir)

tf_config = tf.ConfigProto()
tf_config.gpu_options.allow_growth = True

def gather_batch(y_batch):
    gather_label = np.zeros((y_batch.shape[0],2),dtype = int)
    for i in range(y_batch.shape[0]):
        gather_label[i][0] = i
        gather_label[i][1] = y_batch[i]
    return (gather_label)



with tf.Session(config = tf_config) as sess:

  # Set up adversary
  attack = LinfPGDAttack(sess, model,
                       config['epsilon'],
                       config['num_steps'],
                       config['step_size'],
                       config['random_start'],
                       config['loss_func'])

  # Initialize the summary writer, global variables, and our time counter.
  summary_writer = tf.summary.FileWriter(model_dir, sess.graph)

  if continue_train:
    checkpoint = tf.train.latest_checkpoint(model_dir)
    saver.restore(sess, checkpoint)
    curr_step = int(re.search(r'\d+', checkpoint).group(0))
    curr_step = int(checkpoint.split('-')[1])
    sess.run(global_step.assign(curr_step))
  else:
    curr_step = 0
    sess.run(tf.global_variables_initializer())

  training_time = 0.0

  # Main training loop
  for ii in range(curr_step, max_num_training_steps):
    x_batch, y_batch = flower.train_data.get_next_batch(batch_size,
                                                       multiple_passes=True)

    x_batch = x_batch.reshape((-1,128,128,3))
    # create batch input with all zero pixels
    x_batch_base = np.zeros_like(x_batch, dtype= float) #dtype=float
    ## create gather batch level for extracting TCI softmax output
    y_gather_batch = gather_batch(y_batch)
    ## crate batched uniform distribution
    uniform_dist_batch = np.ones_like(x_batch).reshape((batch_size, 128*128, 3)) * (1/16384)

    # Compute Adversarial Perturbations
    start = timer()
    x_batch_adv = attack.perturb(x_batch, y_batch)
    end = timer()
    training_time += end - start

    nat_dict = {model.input: x_batch,
                model.label: y_batch}

    adv_dict = {model.input: x_batch_adv,
                model.label: y_batch}

    ig_dict = {model.input: x_batch,
               model.adv_input: x_batch_adv,
               model.label: y_batch}

    # Output to stdout
    if ii % num_output_steps == 0:
      nat_acc, nat_loss = sess.run([model.accuracy, model.xent], feed_dict=nat_dict)
      adv_acc, adv_loss = sess.run([model.accuracy,  model.xent], feed_dict=adv_dict)
      IG_re = sess.run(re_term, feed_dict={model.input: x_batch, model.adv_input: x_batch_adv, model.label: y_batch, model.base_input: x_batch_base, model.gather_label: y_gather_batch, model.uniform_dist_batch: uniform_dist_batch})

      print('Step {}:    ({})'.format(ii, datetime.now()), flush = True)
      print('    training nat accuracy {:.4}%, loss {:.4}'.format(nat_acc * 100, nat_loss), flush = True)
      print('    training adv accuracy {:.4}%, loss {:.4}'.format(adv_acc * 100, adv_loss), flush = True)
      print('    training IG term {:.4}'.format(IG_re), flush = True)
      if ii != 0:
        print('    {} examples per second'.format(
            num_output_steps * batch_size / training_time), flush = True)
        training_time = 0.0
    # Tensorboard summaries
    if ii % num_summary_steps == 0:
      summary = sess.run(merged_summaries, feed_dict=adv_dict)
      summary_writer.add_summary(summary, global_step.eval(sess))

    # Write a checkpoint
    if ii % num_checkpoint_steps == 0:
      saver.save(sess,
                 os.path.join(model_dir, 'checkpoint'),
                 global_step=global_step)

    # Actual training step
    start = timer()
    sess.run(train_step, feed_dict={model.input: x_batch, model.adv_input: x_batch_adv, model.label: y_batch, model.base_input: x_batch_base, model.gather_label: y_gather_batch, model.uniform_dist_batch: uniform_dist_batch})
    end = timer()
    training_time += end - start
