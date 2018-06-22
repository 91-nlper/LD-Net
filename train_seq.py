from __future__ import print_function
import datetime
import time
import torch
import torch.nn as nn
import torch.optim as optim
import codecs
import pickle
import math

from model_word_ada.LM import LM
from model_word_ada.basic import BasicRNN
from model_word_ada.densenet import DenseRNN
from model_word_ada.ldnet import LDRNN

from model_seq.crf import CRFLoss, CRFDecode
from model_seq.dataset import SeqDataset
from model_seq.evaluator import eval_wc
from model_seq.seqlabel import SeqLabel, Vanilla_SeqLabel
from model_seq.seqlm import BasicSeqLM
from model_seq.sparse_lm import SparseSeqLM
import model_seq.utils as utils

from tensorboardX import SummaryWriter

import argparse
import json
import os
import sys
import itertools
import functools


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--corpus', default='./data/ner_dataset.pk')
    parser.add_argument('--forward_lm', default='./checkpoint/basic_3.model')
    parser.add_argument('--backward_lm', default='./checkpoint/basic_4.model')

    parser.add_argument('--log_dir', default='one_0')
    parser.add_argument('--checkpoint', default='./checkpoint/ner/basic_0.model')
    parser.add_argument('--gpu', type=int, default=1)

    parser.add_argument('--lm_hid_dim', type=int, default=2048)
    parser.add_argument('--lm_word_dim', type=int, default=300)
    parser.add_argument('--lm_label_dim', type=int, default=-1)
    parser.add_argument('--lm_layer_num', type=int, default=2)
    parser.add_argument('--lm_droprate', type=float, default=0.5)
    parser.add_argument('--lm_rnn_layer', choices=['Basic', 'DDNet', 'DenseNet', 'LDNet'], default='Basic')
    parser.add_argument('--lm_rnn_unit', choices=['gru', 'lstm', 'rnn', 'bnlstm'], default='lstm')

    parser.add_argument('--seq_c_dim', type=int, default=30)
    parser.add_argument('--seq_c_hid', type=int, default=150)
    parser.add_argument('--seq_c_layer', type=int, default=1)
    parser.add_argument('--seq_w_dim', type=int, default=100)
    parser.add_argument('--seq_w_hid', type=int, default=300)
    parser.add_argument('--seq_w_layer', type=int, default=1)
    parser.add_argument('--seq_droprate', type=float, default=0.5)
    parser.add_argument('--seq_rnn_unit', choices=['gru', 'lstm', 'rnn'], default='lstm')
    parser.add_argument('--seq_lm_model', choices=['vanilla', 'sparse-lm'], default='vanilla')
    parser.add_argument('--seq_model', choices=['vanilla', 'lm-aug'], default='lm-aug')

    parser.add_argument('--update', choices=['Adam', 'Adagrad', 'Adadelta', 'SGD'], default='Adam')
    parser.add_argument('--batch_size', type=int, default=10)
    parser.add_argument('--patience', type=int, default=15)
    parser.add_argument('--epoch', type=int, default=200)
    parser.add_argument('--clip', type=float, default=5)
    parser.add_argument('--lr', type=float, default=-1)
    parser.add_argument('--lr_decay', type=float, default=0.05)

    parser.add_argument('--use_writer', action='store_true')
    args = parser.parse_args()

    device = torch.device("cuda:" + str(args.gpu) if args.gpu >= 0 else "cpu")
    
    print('loading data')
    dataset = pickle.load(open(args.corpus, 'rb'))
    name_list = ['flm_map', 'blm_map', 'gw_map', 'c_map', 'y_map', 'emb_array', 'train_data', 'test_data', 'dev_data']

    flm_map, blm_map, gw_map, c_map, y_map, emb_array, train_data, test_data, dev_data = [dataset[tup] for tup in name_list ]

    print('loading language model')
    rnn_map = {'Basic': BasicRNN, 'DDNet': DDRNN, 'DenseNet': DenseRNN, 'LDNet': functools.partial(LDRNN, layer_drop = 0)}
    flm_rnn_layer = rnn_map[args.lm_rnn_layer](args.lm_layer_num, args.lm_rnn_unit, args.lm_word_dim, args.lm_hid_dim, args.lm_droprate)
    blm_rnn_layer = rnn_map[args.lm_rnn_layer](args.lm_layer_num, args.lm_rnn_unit, args.lm_word_dim, args.lm_hid_dim, args.lm_droprate)
    flm_model = LM(flm_rnn_layer, None, len(flm_map), args.lm_word_dim, args.lm_droprate, label_dim = args.lm_label_dim)
    blm_model = LM(blm_rnn_layer, None, len(blm_map), args.lm_word_dim, args.lm_droprate, label_dim = args.lm_label_dim)

    flm_file = torch.load(args.forward_lm, map_location=lambda storage, loc: storage)
    flm_model.load_state_dict(flm_file['lm_model'], False)
    blm_file = torch.load(args.backward_lm, map_location=lambda storage, loc: storage)
    blm_model.load_state_dict(blm_file['lm_model'], False)

    slm_map = {'vanilla': BasicSeqLM, 'sparse-lm': SparseSeqLM}
    flm_model_seq = slm_map[args.seq_lm_model](flm_model, False, args.lm_droprate, True)
    blm_model_seq = slm_map[args.seq_lm_model](blm_model, True, args.lm_droprate, True)

    print('building model')

    SL_map = {'vanilla':Vanilla_SeqLabel, 'lm-aug': SeqLabel}
    seq_model = SL_map[args.seq_model](flm_model_seq, blm_model_seq, len(c_map), args.seq_c_dim, args.seq_c_hid, args.seq_c_layer, len(gw_map), args.seq_w_dim, args.seq_w_hid, args.seq_w_layer, len(y_map), args.seq_droprate, unit=args.seq_rnn_unit)

    seq_model.rand_init()
    seq_model.load_pretrained_word_embedding(torch.FloatTensor(emb_array))
    seq_model.to(device)

    crit = CRFLoss(y_map)
    decoder = CRFDecode(y_map)
    evaluator = eval_wc(decoder, 'f1')

    print('constructing dataset')
    train_dataset, test_dataset, dev_dataset = [SeqDataset(tup_data, flm_map['\n'], blm_map['\n'], gw_map['<\n>'], c_map[' '], c_map['\n'], y_map['<s>'], y_map['<eof>'], len(y_map), args.batch_size) for tup_data in [train_data, test_data, dev_data]]

    print('constructing optimizer')
    param_dict = filter(lambda t: t.requires_grad, seq_model.parameters())
    optim_map = {'Adam' : optim.Adam, 'Adagrad': optim.Adagrad, 'Adadelta': optim.Adadelta, 'SGD': functools.partial(optim.SGD, momentum=0.9)}
    if args.lr > 0:
        optimizer=optim_map[args.update](param_dict, lr=args.lr)
    else:
        optimizer=optim_map[args.update](param_dict)

    if args.use_writer:
        writer = SummaryWriter(log_dir='./np/'+args.log_dir)
        name_list = ['train_loss', 'dev_f1', 'test_f1']
        tloss, df1, tf1 = [args.log_dir+'/'+tup for tup in name_list]
    
    best_f1 = float('-inf')
    patience_count = 0
    batch_index = 0
    normalizer=0
    tot_loss = 0

    for indexs in range(args.epoch):

        seq_model.train()
        for f_c, f_p, b_c, b_p, flm_w, blm_w, blm_ind, f_w, f_y, f_y_m, _ in train_dataset.get_tqdm(device):

            seq_model.zero_grad()
            output = seq_model(f_c, f_p, b_c, b_p, flm_w, blm_w, blm_ind, f_w)
            loss = crit(output, f_y, f_y_m)

            tot_loss += utils.to_scalar(loss)
            normalizer += 1

            loss.backward()
            torch.nn.utils.clip_grad_norm_(seq_model.parameters(), args.clip)
            optimizer.step()

            if args.use_writer and 0 == (batch_index + 1) % 100:
                tot_loss = tot_loss / normalizer
                writer.add_scalar(tloss, tot_loss, batch_index)
                tot_loss = 0
                normalizer = 0
        
            batch_index += 1

        if args.update == 'SGD':
            current_lr = args.lr / (1 + (indexs + 1) * args.lr_decay)
            utils.adjust_learning_rate(optimizer, current_lr)

        dev_f1, dev_pre, dev_rec, dev_acc = evaluator.calc_score(seq_model, dev_dataset.get_tqdm(device))

        if args.use_writer:
            writer.add_scalar(df1, dev_f1, indexs)

        if dev_f1 > best_f1:
            test_f1, test_pre, test_rec, test_acc = evaluator.calc_score(seq_model, test_dataset.get_tqdm(device))
            best_f1, best_dev_pre, best_dev_rec, best_dev_acc = dev_f1, dev_pre, dev_rec, dev_acc

            print('tot_loss: %.4f dev_f1: %.4f dev_rec: %.4f dev_pre: %.4f dev_acc: %.4f test_f1: %.4f test_rec: %.4f test_pre: %.4f test_acc: %.4f' % (tot_loss/(normalizer+0.001), dev_f1, dev_rec, dev_pre, dev_acc, test_f1, test_rec, test_pre, test_acc))

            patience_count = 0
            if args.use_writer:
                writer.add_scalar(tf1, test_f1, indexs)

        else:
            print('tot_loss: %.4f dev_f1: %.4f dev_rec: %.4f dev_pre: %.4f dev_acc: %.4f' % (tot_loss/(normalizer+0.0001), dev_f1, dev_rec, dev_pre, dev_acc))
            patience_count += 1
            if patience_count >= args.patience:
                break
    
    print(' dev_f1: %.4f dev_rec: %.4f dev_pre: %.4f dev_acc: %.4f test_f1: %.4f test_rec: %.4f test_pre: %.4f test_acc: %.4f\n' % (best_f1, best_dev_rec, best_dev_pre, best_dev_acc, test_f1, test_rec, test_pre, test_acc))

    if args.use_writer:
        writer.close()