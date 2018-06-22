"""
.. module:: sparse_lm
    :synopsis: sparse language model for sequence labeling
 
.. moduleauthor:: Liyuan Liu
"""
import time

import torch
import torch.nn as nn
import torch.nn.functional as F
import model_seq.utils as utils

import torch
import torch.nn as nn
import torch.nn.functional as F

class SBUnit(nn.Module):
    def __init__(self, ori_unit, droprate, fix_rate):
        super(SBUnit, self).__init__()

        self.unit = ori_unit.unit

        self.layer = ori_unit.layer

        self.droprate = droprate

        self.input_dim = ori_unit.input_dim
        self.increase_rate = ori_unit.increase_rate
        self.output_dim = ori_unit.input_dim + ori_unit.increase_rate

    def prune_rnn(self, mask):
        mask_index = mask.nonzero().squeeze(1)
        self.layer.weight_ih_l0 = nn.Parameter(self.layer.weight_ih_l0.data.index_select(1, mask_index).contiguous())
        self.layer.input_size = self.layer.weight_ih_l0.size(1)

    def forward(self, x, weight=1):

        if self.droprate > 0:
            new_x = F.dropout(x, p=self.droprate, training=self.training)
        else:
            new_x = x

        out, _ = self.layer(new_x)

        out = weight * out

        return torch.cat([x, out], 2)

class SDRNN(nn.Module):
    def __init__(self, ori_drnn, droprate, fix_rate):
        super(SDRNN, self).__init__()

        self.layer_list = [SBUnit(ori_unit, droprate, fix_rate) for ori_unit in ori_drnn.layer._modules.values()]

        self.weight_list = nn.Parameter(torch.FloatTensor([1.0] * len(self.layer_list)))
        self.weight_list.requires_grad = not fix_rate

        # self.layer = nn.Sequential(*self.layer_list)
        self.layer = nn.ModuleList(self.layer_list)

        for param in self.layer.parameters():
            param.requires_grad = False

        self.output_dim = self.layer_list[-1].output_dim

    def prune_dense_rnn(self):

        prune_mask = torch.ones(self.layer_list[0].input_dim)
        increase_mask_one = torch.ones(self.layer_list[0].increase_rate)
        increase_mask_zero = torch.zeros(self.layer_list[0].increase_rate)

        new_layer_list = list()
        new_weight_list = list()
        for ind in range(0, len(self.layer_list)):
            if self.weight_list.data[ind] > 0:
                new_weight_list.append(self.weight_list.data[ind])

                self.layer_list[ind].prune_rnn(prune_mask)
                new_layer_list.append(self.layer_list[ind])

                prune_mask = torch.cat([prune_mask, increase_mask_one], dim = 0)
            else:
                prune_mask = torch.cat([prune_mask, increase_mask_zero], dim = 0)

        if not new_layer_list:
            self.output_dim = self.layer_list[0].input_dim
            self.layer = None
            self.weight_list = None
            self.layer_list = None
        else:
            self.layer_list = new_layer_list
            self.layer = nn.ModuleList(self.layer_list)
            self.weight_list = nn.Parameter(torch.FloatTensor(new_weight_list))
            self.weight_list.requires_grad = False

            for param in self.layer.parameters():
                param.requires_grad = False

        return prune_mask

    # def prox(self, lambda0, lambda1):
    #     none_zero_count = (self.weight_list.data > 0).sum()
    #     if none_zero_count > lambda1:
    #         self.weight_list.data -= lambda0
    #     self.weight_list.data.masked_fill_(self.weight_list.data < 0, 0)
    #     self.weight_list.data.masked_fill_(self.weight_list.data > 1, 1)
    #     if none_zero_count > lambda1:
    #         none_zero_count = (self.weight_list.data > 0).sum()
    #     return none_zero_count

    def prox(self, lambda0, lambda1):
        self.weight_list.data.masked_fill_(self.weight_list.data < 0, 0)
        self.weight_list.data.masked_fill_(self.weight_list.data > 1, 1)
        none_zero_count = (self.weight_list.data > 0).sum()
        return none_zero_count

    def regularizer(self, lambda1):
        reg3 = (self.weight_list * (1 - self.weight_list)).sum()
        none_zero = self.weight_list.data > 0
        none_zero_count = none_zero.sum()
        reg0 = none_zero_count
        reg1 = self.weight_list[none_zero].sum()
        return reg0, reg1, reg3

    def forward(self, x):
        if self.layer_list is not None:
            for ind in range(len(self.layer_list)):
                x = self.layer[ind](x, self.weight_list[ind])
        return x
        # return self.layer(x)

class SparseSeqLM(nn.Module):

    def __init__(self, ori_lm, backward, droprate, fix_rate):
        super(SparseSeqLM, self).__init__()

        # self.rnn = SeqDDRNN(ori_lm.rnn, droprate, fix_rate)
        self.rnn = SDRNN(ori_lm.rnn, droprate, fix_rate)

        self.w_num = ori_lm.w_num
        self.w_dim = ori_lm.w_dim
        self.word_embed = ori_lm.word_embed
        self.word_embed.weight.requires_grad = False

        # self.output_dim = ori_lm.rnn_output

        # self.add_proj = ori_lm.add_proj
        # if ori_lm.add_proj:
        #     self.project = ori_lm.project
        #     self.project.weight.requires_grad = False
        #     self.relu = nn.ReLU()
        #     self.output_dim = self.project.weight.size(0)
        # else:
        #     self.output_dim = ori_lm.rnn_output
        self.output_dim = ori_lm.rnn_output

        # self.drop = nn.Dropout(p=droprate)
        self.backward = backward

    def prune_dense_rnn(self):
        prune_mask = self.rnn.prune_dense_rnn()
        self.output_dim = self.rnn.output_dim
        return prune_mask

    def init_hidden(self):
        return

    def regularizer(self, lambda1):
        return self.rnn.regularizer(lambda1)

    def prox(self, lambda0, lambda1):
        return self.rnn.prox(lambda0, lambda1)

    def forward(self, w_in, ind=None):
        w_emb = self.word_embed(w_in)
        
        out = self.rnn(w_emb)
        # out = self.drop(out)

        # if self.add_proj:
        #     out = self.relu(self.project(out))

        if self.backward:
            out_size = out.size()
            out = out.view(out_size[0] * out_size[1], out_size[2]).index_select(0, ind).contiguous().view(out_size)

        return out