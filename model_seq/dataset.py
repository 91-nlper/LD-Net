"""
.. module:: dataset
    :synopsis: dataset for sequence labeling
 
.. moduleauthor:: Liyuan Liu
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd

import sys
import pickle
import random
import functools
import itertools
from tqdm import tqdm

class SeqDataset(object):

    def __init__(self, dataset, flm_pad, blm_pad, w_pad, c_con, c_pad, y_start, y_pad, y_size, batch_size):
        super(SeqDataset, self).__init__()

        self.flm_pad = flm_pad
        self.blm_pad = blm_pad
        self.w_pad = w_pad
        self.c_con = c_con
        self.c_pad = c_pad
        self.y_pad = y_pad
        self.y_size = y_size
        self.y_start = y_start
        self.batch_size = batch_size

        self.construct_index(dataset)
        self.shuffle()
        self.cur_idx = 0

    def shuffle(self):
        random.shuffle(self.shuffle_list)

    def get_tqdm(self):
        return tqdm(self, mininterval=2, total=self.index_length // self.batch_size, leave=False, file=sys.stdout, ncols=80)

    def construct_index(self, dataset):

        for instance in dataset:
            c_len = [len(tup)+1 for tup in instance[3]]
            c_ins = [tup for ins in instance[3] for tup in (ins + [self.c_con])]
            instance[3] = c_ins
            instance.append(c_len)

        self.dataset = dataset
        self.index_length = len(dataset)
        self.shuffle_list = list(range(0, self.index_length))

    def batchify(self, batch):
        
        cur_batch_size = len(batch)

        char_padded_len = max([len(tup[3]) for tup in batch])
        word_padded_len = max([len(tup[0]) for tup in batch])

        tmp_batch =  [list() for ind in range(11)]

        for instance_ind in range(cur_batch_size):

            instance = batch[instance_ind]

            char_padded_len_ins = char_padded_len - len(instance[3])
            word_padded_len_ins = word_padded_len - len(instance[0])

            tmp_batch[0].append(instance[3] + [self.c_pad] + [self.c_pad] * char_padded_len_ins)
            tmp_batch[2].append([self.c_pad] + instance[3][::-1] + [self.c_pad] * char_padded_len_ins)

            tmp_p = list( itertools.accumulate(instance[5]+[1]+[0]* word_padded_len_ins) )
            tmp_batch[1].append([(x - 1) * cur_batch_size + instance_ind for x in tmp_p])
            tmp_p = list(itertools.accumulate([1]+instance[5][::-1]))[::-1] + [1]*word_padded_len_ins
            tmp_batch[3].append([(x - 1) * cur_batch_size + instance_ind for x in tmp_p])

            tmp_batch[4].append(instance[0] + [self.flm_pad] + [self.flm_pad] * word_padded_len_ins)
            tmp_batch[5].append([self.blm_pad] + instance[1][::-1] + [self.blm_pad] * word_padded_len_ins)

            tmp_p = list(range(len(instance[1]), -1, -1)) + list(range(len(instance[1])+1, word_padded_len+1))
            tmp_batch[6].append([x * cur_batch_size + instance_ind for x in tmp_p])

            tmp_batch[7].append(instance[2] + [self.w_pad] + [self.w_pad] * word_padded_len_ins)

            tmp_batch[8].append([self.y_start * self.y_size + instance[4][0]] + [instance[4][ind] * self.y_size + instance[4][ind+1] for ind in range(len(instance[4]) - 1)] + [instance[4][-1] * self.y_size + self.y_pad] + [self.y_pad * self.y_size + self.y_pad] * word_padded_len_ins)

            tmp_batch[9].append([1] * len(instance[4]) + [1] + [0] * word_padded_len_ins)

            tmp_batch[10].append(instance[4])
                
        tbt = [torch.LongTensor(v).transpose(0, 1).contiguous() for v in tmp_batch[0:9]] + [torch.ByteTensor(tmp_batch[9]).transpose(0, 1).contiguous()]

        tbt[1] = tbt[1].view(-1)
        tbt[3] = tbt[3].view(-1)
        tbt[6] = tbt[6].view(-1)

        return [autograd.Variable(ten).cuda() for ten in tbt] + [tmp_batch[10]]

    def __iter__(self):
        return self

    def __next__(self):
        if self.cur_idx == self.index_length:
            self.cur_idx = 0
            self.shuffle()
            raise StopIteration

        end_index = min(self.cur_idx + self.batch_size, self.index_length)

        batch = [self.dataset[self.shuffle_list[index]] for index in range(self.cur_idx, end_index)]

        self.cur_idx = end_index

        return self.batchify(batch)