import argparse
import torch
import numpy as np
import os
import sys
import time
import math
import json

import logging
from datetime import datetime

from data_loader import DataLoader
from model import CaptionModel, CrossEntropyCriterion
from train import test

import utils
import opts

logger = logging.getLogger(__name__)

if __name__ == '__main__':

    opt = opts.parse_opts()

    logging.basicConfig(level=getattr(logging, opt.loglevel.upper()),
                        format='%(asctime)s:%(levelname)s: %(message)s')

    logger.info(
        'Input arguments: %s',
        json.dumps(
            vars(opt),
            sort_keys=True,
            indent=4))

    start = datetime.now()

    val_opt = {'label_h5': opt.val_label_h5,
               'node_lmdb': opt.val_node_lmdb,
               'batch_size': opt.test_batch_size,
               'feat_h5': opt.val_feat_h5,
               'cocofmt_file': opt.val_cocofmt_file,
               'seq_per_img': opt.test_seq_per_img,
               'num_chunks': opt.num_chunks,
               'total_node': opt.total_node,
               'scene_graph_path': opt.scene_graph_path,
               'mode': 'test'
               }

    test_opt = {'label_h5': opt.test_label_h5,
                'node_lmdb': opt.test_node_lmdb,
                'batch_size': opt.test_batch_size,
                'feat_h5': opt.test_feat_h5,
                'cocofmt_file': opt.test_cocofmt_file,
                'seq_per_img': opt.test_seq_per_img,
                'num_chunks': opt.num_chunks,
                'total_node': opt.total_node,
                'scene_graph_path': opt.scene_graph_path,
                'mode': 'test'
                }

    val_loader = DataLoader(val_opt)
    test_loader = DataLoader(test_opt)

    logger.info('Loading model: %s', opt.model_file)
    checkpoint = torch.load(opt.model_file)
    checkpoint_opt = checkpoint['opt']

    opt.model_type = checkpoint_opt.model_type
    opt.vocab = checkpoint_opt.vocab
    opt.vocab_size = checkpoint_opt.vocab_size
    opt.seq_length = checkpoint_opt.seq_length
    opt.feat_dims = checkpoint_opt.feat_dims

    assert opt.vocab_size == test_loader.get_vocab_size()
    assert opt.seq_length == test_loader.get_seq_length()
    assert opt.feat_dims == test_loader.get_feat_dims()

    logger.info('Building model...')
    model = CaptionModel(opt)
    logger.info('Loading state from the checkpoint...')
    model.load_state_dict(checkpoint['model'])

    xe_criterion = CrossEntropyCriterion()

    if torch.cuda.is_available():
        model.cuda()
        xe_criterion.cuda()

    logger.info('Start testing...')
    # test(model, xe_criterion, val_loader, opt)
    test(model, xe_criterion, test_loader, opt)
    logger.info('Time: %s', datetime.now() - start)
