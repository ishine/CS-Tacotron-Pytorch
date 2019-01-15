# -*- coding: utf-8 -*- #
"""*********************************************************************************************"""
#   FileName     [ train.py ]
#   Synopsis     [ Trainining script for Tacotron speech synthesis model ]
#   Author       [ Ting-Wei Liu (Andi611) ]
#   Copyright    [ Copyleft(c), Speech Lab, NTU, Taiwan ]
"""*********************************************************************************************"""


"""
	Usage: train.py [options]

	Options:
		--data-root=<dir>         Directory contains preprocessed features.
		--checkpoint-dir=<dir>    Directory where to save model checkpoints [default: checkpoints].
		--checkpoint-path=<name>  Restore model from checkpoint path if given.
		--hparams=<parmas>        Hyper parameters [default: ].
		-h, --help                Show this help message and exit
"""


###############
# IMPORTATION #
###############
import os
import sys
import time
from docopt import docopt
#----------------------------------------#
import numpy as np
import librosa.display
from matplotlib import pyplot as plt
#----------------------------------------#
from utils import audio
from utils.plot import plot_alignment
from utils.text import text_to_sequence, symbols

# The tacotron model
#----------------------------------------#
import torch
from torch import nn
from torch import optim
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
from torch.utils import data as data_utils
#----------------------------------------#
from model import Tacotron
from hparams import hparams, hparams_debug_string
#----------------------------------------#
from nnmnkwii.datasets import FileSourceDataset, FileDataSource
from tensorboardX import SummaryWriter
#import tensorboard_logger
#from tensorboard_logger import log_value


####################
# GLOBAL VARIABLES #
####################
global_step = 0
global_epoch = 0
use_cuda = torch.cuda.is_available()
if use_cuda:
	cudnn.benchmark = False


def _pad(seq, max_len):
	return np.pad(seq, (0, max_len - len(seq)),
				  mode='constant', constant_values=0)

def _pad_2d(x, max_len):
	x = np.pad(x, [(0, max_len - len(x)), (0, 0)],
			   mode="constant", constant_values=0)
	return x


class TextDataSource(FileDataSource):
	def __init__(self):
		pass #self._cleaner_names = [x.strip() for x in hparams.cleaners.split(',')]

	def collect_files(self):
		meta = os.path.join(DATA_ROOT, "meta_text.txt")
		with open(meta, 'r', encoding='utf-8') as f:
			lines = f.readlines()
		lines = list(map(lambda l: l.split("|")[-1][:-1], lines))
		return lines

	def collect_features(self, text):
		return np.asarray(text_to_sequence(text), dtype=np.int32)


class _NPYDataSource(FileDataSource):
	def __init__(self, col):
		self.col = col

	def collect_files(self):
		meta = os.path.join(DATA_ROOT, "meta_text.txt")
		with open(meta, 'r', encoding='utf-8') as f:
			lines = f.readlines()
		lines = list(map(lambda l: l.split("|")[self.col], lines))
		paths = list(map(lambda f: os.path.join(DATA_ROOT, f), lines))
		return paths

	def collect_features(self, path):
		return np.load(path)


class MelSpecDataSource(_NPYDataSource):
	def __init__(self):
		super(MelSpecDataSource, self).__init__(1)


class LinearSpecDataSource(_NPYDataSource):
	def __init__(self):
		super(LinearSpecDataSource, self).__init__(0)


class PyTorchDataset(object):
	def __init__(self, X, Mel, Y):
		self.X = X
		self.Mel = Mel
		self.Y = Y

	def __getitem__(self, idx):
		return self.X[idx], self.Mel[idx], self.Y[idx]

	def __len__(self):
		return len(self.X)


def collate_fn(batch):
	"""Create batch"""
	r = hparams.outputs_per_step
	input_lengths = [len(x[0]) for x in batch]
	max_input_len = np.max(input_lengths)
	# Add single zeros frame at least, so plus 1
	max_target_len = np.max([len(x[1]) for x in batch]) + 1
	if max_target_len % r != 0:
		max_target_len += r - max_target_len % r
		assert max_target_len % r == 0

	a = np.array([_pad(x[0], max_input_len) for x in batch], dtype=np.int)
	x_batch = torch.LongTensor(a)

	input_lengths = torch.LongTensor(input_lengths)

	b = np.array([_pad_2d(x[1], max_target_len) for x in batch],
				 dtype=np.float32)
	mel_batch = torch.FloatTensor(b)

	c = np.array([_pad_2d(x[2], max_target_len) for x in batch],
				 dtype=np.float32)
	y_batch = torch.FloatTensor(c)
	return x_batch, input_lengths, mel_batch, y_batch


def save_alignment(path, attn):
	plot_alignment(attn.T, path, info="tacotron, step={}".format(global_step))


def save_spectrogram(path, linear_output):
	plot_spectrogram(path, linear_output)


def _learning_rate_decay(init_lr, global_step):
	warmup_steps = 6000.0
	step = global_step + 1.
	lr = init_lr * warmup_steps**0.5 * np.minimum(
		step * warmup_steps**-1.5, step**-0.5)
	return lr


def save_states(global_step, mel_outputs, linear_outputs, attn, y,
				input_lengths, checkpoint_dir=None):
	print("Save intermediate states at step {}".format(global_step))

	# idx = np.random.randint(0, len(input_lengths))
	idx = min(1, len(input_lengths) - 1)
	input_length = input_lengths[idx]

	# Alignment
	path = os.path.join(checkpoint_dir, "step{}_alignment.png".format(
		global_step))
	# alignment = attn[idx].cpu().data.numpy()[:, :input_length]
	alignment = attn[idx].cpu().data.numpy()
	save_alignment(path, alignment)

	# Predicted spectrogram
	path = os.path.join(checkpoint_dir, "step{}_predicted_spectrogram.png".format(
		global_step))
	linear_output = linear_outputs[idx].cpu().data.numpy()
	save_spectrogram(path, linear_output)

	# Predicted audio signal
	signal = audio.inv_spectrogram(linear_output.T)
	path = os.path.join(checkpoint_dir, "step{}_predicted.wav".format(
		global_step))
	audio.save_wav(signal, path)

	# Target spectrogram
	path = os.path.join(checkpoint_dir, "step{}_target_spectrogram.png".format(
		global_step))
	linear_output = y[idx].cpu().data.numpy()
	save_spectrogram(path, linear_output)


def train(model, data_loader, optimizer,
		  init_lr=0.002,
		  checkpoint_dir=None, checkpoint_interval=None, nepochs=None,
		  clip_thresh=1.0,
		  sample_rate=20000):

	writer = SummaryWriter()
	model.train()
	linear_dim = model.linear_dim

	criterion = nn.L1Loss()

	global global_step, global_epoch
	while global_epoch < nepochs:
		start = time.time()
		running_loss = 0.
		for x, input_lengths, mel, y in data_loader:
			# Decay learning rate
			current_lr = _learning_rate_decay(init_lr, global_step)
			for param_group in optimizer.param_groups:
				param_group['lr'] = current_lr

			optimizer.zero_grad()

			# Sort by length
			sorted_lengths, indices = torch.sort(
				input_lengths.view(-1), dim=0, descending=True)
			sorted_lengths = sorted_lengths.long().numpy()

			x, mel, y = x[indices], mel[indices], y[indices]

			# Feed data
			x, mel, y = Variable(x), Variable(mel), Variable(y)
			if use_cuda:
				x, mel, y = x.cuda(), mel.cuda(), y.cuda()
			mel_outputs, linear_outputs, attn = model(
				x, mel, input_lengths=sorted_lengths)

			# Loss
			mel_loss = criterion(mel_outputs, mel)
			n_priority_freq = int(3000 / (sample_rate * 0.5) * linear_dim)
			linear_loss = 0.5 * criterion(linear_outputs, y) \
				+ 0.5 * criterion(linear_outputs[:, :, :n_priority_freq],
								  y[:, :, :n_priority_freq])
			loss = mel_loss + linear_loss

			if global_step > 0 and global_step % checkpoint_interval == 0:
				save_states(
					global_step, mel_outputs, linear_outputs, attn, y,
					sorted_lengths, checkpoint_dir)
				save_checkpoint(
					model, optimizer, global_step, checkpoint_dir, global_epoch)

			# Update
			loss.backward()
			grad_norm = torch.nn.utils.clip_grad_norm_(
				model.parameters(), clip_thresh)
			optimizer.step()

			# Logs
			writer.add_scalar('total_loss', loss.item(), global_step)
			writer.add_scalar('mel_loss', mel_loss.item(), global_step)
			writer.add_scalar('linear_loss', linear_loss.item(), global_step)
			writer.add_scalar('grad_norm', grad_norm, global_step)
			writer.add_scalar('learning_rate', current_lr, global_step)
			#log_value("loss", float(loss.data[0]), global_step)
			#log_value("mel loss", float(mel_loss.data[0]), global_step)
			#log_value("linear loss", float(linear_loss.data[0]), global_step)
			#log_value("gradient norm", grad_norm, global_step)
			#log_value("learning rate", current_lr, global_step)
			duration = time.time() - start
			if global_step % 5 == 0:
				log = '[{}] total_loss: {:.3f}. mel_loss: {:.3f}, mag_loss: {:.3f}, grad_norm: {:.3f}, lr: {:.5f}, time: {:.2f}s'.format(global_step, loss.item(), mel_loss.item(), linear_loss.item(), grad_norm, current_lr, duration)
				print(log)

			global_step += 1
			running_loss += loss.item()
			start = time.time()

		#averaged_loss = running_loss / (len(data_loader))
		#log_value("loss (per epoch)", averaged_loss, global_epoch)
		#print("Loss: {}".format(running_loss / (len(data_loader))))

		global_epoch += 1


def save_checkpoint(model, optimizer, step, checkpoint_dir, epoch):
	checkpoint_path = os.path.join(
		checkpoint_dir, "checkpoint_step{}.pth".format(global_step))
	torch.save({
		"state_dict": model.state_dict(),
		"optimizer": optimizer.state_dict(),
		"global_step": step,
		"global_epoch": epoch,
	}, checkpoint_path)
	print("Saved checkpoint:", checkpoint_path)


if __name__ == "__main__":
	args = docopt(__doc__)
	print("Command line args:\n", args)
	checkpoint_dir = args["--checkpoint-dir"]
	checkpoint_path = args["--checkpoint-path"]
	data_root = args["--data-root"]
	if data_root:
		DATA_ROOT = data_root

	# Override hyper parameters
	hparams.parse(args["--hparams"])

	os.makedirs(checkpoint_dir, exist_ok=True)

	# Input dataset definitions
	X = FileSourceDataset(TextDataSource())
	Mel = FileSourceDataset(MelSpecDataSource())
	Y = FileSourceDataset(LinearSpecDataSource())

	# Dataset and Dataloader setup
	dataset = PyTorchDataset(X, Mel, Y)
	data_loader = data_utils.DataLoader(
		dataset, batch_size=hparams.batch_size,
		num_workers=hparams.num_workers, shuffle=True,
		collate_fn=collate_fn, pin_memory=hparams.pin_memory)

	# Model
	model = Tacotron(n_vocab=len(symbols),
					 embedding_dim=256,
					 mel_dim=hparams.num_mels,
					 linear_dim=hparams.num_freq,
					 r=hparams.outputs_per_step,
					 padding_idx=hparams.padding_idx,
					 use_memory_mask=hparams.use_memory_mask,
					 )
	if use_cuda:
		model = model.cuda()
	optimizer = optim.Adam(model.parameters(),
						   lr=hparams.initial_learning_rate, betas=(
							   hparams.adam_beta1, hparams.adam_beta2),
						   weight_decay=hparams.weight_decay)

	# Load checkpoint
	if checkpoint_path:
		print("Load checkpoint from: {}".format(checkpoint_path))
		checkpoint = torch.load(checkpoint_path)
		model.load_state_dict(checkpoint["state_dict"])
		optimizer.load_state_dict(checkpoint["optimizer"])
		try:
			global_step = checkpoint["global_step"]
			global_epoch = checkpoint["global_epoch"]
		except:
			# TODO
			pass

	# Setup tensorboard logger
	#tensorboard_logger.configure("log/run-test")

	print(hparams_debug_string())

	# Train!
	try:
		train(model, data_loader, optimizer,
			  init_lr=hparams.initial_learning_rate,
			  checkpoint_dir=checkpoint_dir,
			  checkpoint_interval=hparams.checkpoint_interval,
			  nepochs=hparams.nepochs,
			  clip_thresh=hparams.clip_thresh,
			  sample_rate=hparams.sample_rate)
	except KeyboardInterrupt:
		pass
		#save_checkpoint(
		#    model, optimizer, global_step, checkpoint_dir, global_epoch)

	print("Finished")
	sys.exit(0)
