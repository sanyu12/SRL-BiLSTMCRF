from __future__ import print_function

__author__ = 'max'
"""
Implementation of Bi-directional LSTM-CNNs-CRF model for POS tagging.
"""

import sys

sys.path.append(".")
sys.path.append("..")

import time
import argparse

import numpy as np
import torch
from torch.optim import Adam, SGD
from neuronlp2.io import get_logger, conllx_data
from neuronlp2.models import BiRecurrentConvCRF
from neuronlp2 import utils


def main():
    parser = argparse.ArgumentParser(description='Tuning with bi-directional RNN-CNN-CRF')
    parser.add_argument('--mode', choices=['RNN', 'LSTM', 'GRU'], help='architecture of rnn', required=True)
    parser.add_argument('--num_epochs', type=int, default=1000, help='Number of training epochs')
    parser.add_argument('--batch_size', type=int, default=16, help='Number of sentences in each batch')
    parser.add_argument('--hidden_size', type=int, default=128, help='Number of hidden units in RNN')
    parser.add_argument('--num_filters', type=int, default=30, help='Number of filters in CNN')
    parser.add_argument('--learning_rate', type=float, default=0.01, help='Learning rate')
    parser.add_argument('--decay_rate', type=float, default=0.1, help='Decay rate of learning rate')
    parser.add_argument('--gamma', type=float, default=0.0, help='weight for regularization')
    parser.add_argument('--dropout', choices=['std', 'variational'], help='type of dropout', required=True)
    parser.add_argument('--p', type=float, default=0.5, help='dropout rate')
    parser.add_argument('--schedule', nargs='+', type=int, help='schedule for learning rate decay')
    parser.add_argument('--output_prediction', action='store_true', help='Output predictions to temp files')
    parser.add_argument('--train')  # "data/POS-penn/wsj/split1/wsj1.train.original"
    parser.add_argument('--dev')  # "data/POS-penn/wsj/split1/wsj1.dev.original"
    parser.add_argument('--test')  # "data/POS-penn/wsj/split1/wsj1.test.original"

    args = parser.parse_args()

    logger = get_logger("POSCRFTagger")

    mode = args.mode
    train_path = args.train
    dev_path = args.dev
    test_path = args.test
    num_epochs = args.num_epochs
    batch_size = args.batch_size
    hidden_size = args.hidden_size
    num_filters = args.num_filters
    learning_rate = args.learning_rate
    momentum = 0.9
    decay_rate = args.decay_rate
    gamma = args.gamma
    schedule = args.schedule
    p = args.p
    output_predict = args.output_prediction

    embedd_dict, embedd_dim = utils.load_word_embedding_dict('glove', "data/glove/glove.6B/glove.6B.100d.gz")
    logger.info("Creating Alphabets")
    word_alphabet, char_alphabet, pos_alphabet, \
    type_alphabet = conllx_data.create_alphabets("data/alphabets/", train_path, data_paths=[dev_path, test_path],
                                                 max_vocabulary_size=50000, embedd_dict=embedd_dict)

    logger.info("Word Alphabet Size: %d" % word_alphabet.size())
    logger.info("Character Alphabet Size: %d" % char_alphabet.size())
    logger.info("POS Alphabet Size: %d" % pos_alphabet.size())

    logger.info("Reading Data")
    use_gpu = torch.cuda.is_available()

    data_train = conllx_data.read_data_to_variable(train_path, word_alphabet, char_alphabet, pos_alphabet,
                                                   type_alphabet, use_gpu=use_gpu)
    # data_train = conllx_data.read_data(train_path, word_alphabet, char_alphabet, pos_alphabet, type_alphabet)
    # num_data = sum([len(bucket) for bucket in data_train])
    num_data = sum(data_train[1])
    num_labels = pos_alphabet.size()

    data_dev = conllx_data.read_data_to_variable(dev_path, word_alphabet, char_alphabet, pos_alphabet, type_alphabet,
                                                 use_gpu=use_gpu)
    data_test = conllx_data.read_data_to_variable(test_path, word_alphabet, char_alphabet, pos_alphabet, type_alphabet,
                                                  use_gpu=use_gpu)

    def construct_word_embedding_table():
        scale = np.sqrt(3.0 / embedd_dim)
        table = np.empty([word_alphabet.size(), embedd_dim], dtype=np.float32)
        table[conllx_data.UNK_ID, :] = np.random.uniform(-scale, scale, [1, embedd_dim]).astype(np.float32)
        oov = 0
        for word, index in word_alphabet.items():
            if word in embedd_dict:
                embedding = embedd_dict[word]
            elif word.lower() in embedd_dict:
                embedding = embedd_dict[word.lower()]
            else:
                embedding = np.random.uniform(-scale, scale, [1, embedd_dim]).astype(np.float32)
                oov += 1
            table[index, :] = embedding
        print('oov: %d' % oov)
        return torch.from_numpy(table)

    word_table = construct_word_embedding_table()
    logger.info("constructing network...")

    char_dim = 30
    window = 3
    num_layers = 1
    if args.dropout == 'std':
        network = BiRecurrentConvCRF(embedd_dim, word_alphabet.size(),
                                     char_dim, char_alphabet.size(),
                                     num_filters, window,
                                     mode, hidden_size, num_layers,
                                     num_labels, embedd_word=word_table, p_rnn=p)
    else:
        raise NotImplementedError

    if use_gpu:
        network.cuda()

    lr = learning_rate
    optim = SGD(network.parameters(), lr=lr, momentum=momentum, weight_decay=gamma)
    logger.info("Network: %s, num_layer=%d, hidden=%d, filter=%d" % (mode, num_layers, hidden_size, num_filters))
    logger.info("training: l2: %f, (#training data: %d, batch: %d, dropout: %.2f)" % (gamma, num_data, batch_size, p))

    num_batches = num_data / batch_size + 1
    dev_correct = 0.0
    best_epoch = 0
    test_correct = 0.0
    test_total = 0
    for epoch in range(1, num_epochs + 1):
        print('Epoch %d (%s(%s), learning rate=%.4f, decay rate=%.4f): ' % (epoch, mode, args.dropout, lr, decay_rate))
        train_err = 0.
        train_total = 0.

        start_time = time.time()
        num_back = 0
        network.train()
        for batch in range(1, num_batches + 1):
            word, char, labels, _, _, masks, lengths = conllx_data.get_batch_variable(data_train, batch_size)

            optim.zero_grad()
            loss = network.loss(word, char, labels, mask=masks)
            loss.backward()
            optim.step()

            num_inst = word.size(0)
            train_err += loss.data[0] * num_inst
            train_total += num_inst

            time_ave = (time.time() - start_time) / batch
            time_left = (num_batches - batch) * time_ave

            # update log
            if batch % 100 == 0:
                sys.stdout.write("\b" * num_back)
                log_info = 'train: %d/%d loss: %.4f, time left (estimated): %.2fs' % (
                    batch, num_batches, train_err / train_total, time_left)
                sys.stdout.write(log_info)
                num_back = len(log_info)

        sys.stdout.write("\b" * num_back)
        print('train: %d loss: %.4f, time: %.2fs' % (
            epoch * num_batches, train_err / train_total, time.time() - start_time))

        # evaluate performance on dev data
        network.eval()
        dev_corr = 0.0
        dev_total = 0
        for batch in conllx_data.iterate_batch_variable(data_dev, batch_size):
            word, char, labels, _, _, masks, lengths = batch
            preds, corr = network.decode(word, char, target=labels, mask=masks,
                                         leading_symbolic=conllx_data.NUM_SYMBOLIC_TAGS)
            num_tokens = masks.data.sum()
            dev_corr += corr.data[0]
            dev_total += num_tokens
        print('dev corr: %d, total: %d, acc: %.2f%%' % (dev_corr, dev_total, dev_corr * 100 / dev_total))

        if dev_correct < dev_corr:
            dev_correct = dev_corr
            best_epoch = epoch

            # evaluate on test data when better performance detected
            test_corr = 0.0
            test_total = 0
            for batch in conllx_data.iterate_batch_variable(data_test, batch_size):
                word, char, labels, _, _, masks, lengths = batch
                preds, corr = network.decode(word, char, target=labels, mask=masks,
                                             leading_symbolic=conllx_data.NUM_SYMBOLIC_TAGS)
                num_tokens = masks.data.sum()
                test_corr += corr.data[0]
                test_total += num_tokens
            test_correct = test_corr
        print("best dev  corr: %d, total: %d, acc: %.2f%% (epoch: %d)" % (
            dev_correct, dev_total, dev_correct * 100 / dev_total, best_epoch))
        print("best test corr: %d, total: %d, acc: %.2f%% (epoch: %d)" % (
            test_correct, test_total, test_correct * 100 / test_total, best_epoch))

        if epoch in schedule:
            lr = learning_rate / (1.0 + epoch * decay_rate)
            optim = SGD(network.parameters(), lr=lr, momentum=momentum, weight_decay=gamma, nesterov=True)


if __name__ == '__main__':
    main()