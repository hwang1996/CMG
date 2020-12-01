import argparse
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_
import numpy as np
import os
import sys
import time
import math
import json
import uuid
import logging
from datetime import datetime
from six.moves import cPickle

from data_loader import DataLoader
from model import CaptionModel, CrossEntropyCriterion, RewardCriterion

import utils
import opts

import sys
sys.path.append("cider")
from pyciderevalcap.cider.cider import Cider
from pyciderevalcap.ciderD.ciderD import CiderD

sys.path.append('coco-caption')
from pycocoevalcap.bleu.bleu import Bleu
from pycocoevalcap.meteor.meteor import Meteor
from pycocoevalcap.rouge.rouge import Rouge


logger = logging.getLogger(__name__)


def language_eval(predictions, cocofmt_file, opt):
    logger.info('>>> Language evaluating ...')
    tmp_checkpoint_json = os.path.join(
        opt.model_file + str(uuid.uuid4()) + '.json')
    json.dump(predictions, open(tmp_checkpoint_json, 'w'))
    lang_stats = utils.language_eval(cocofmt_file, tmp_checkpoint_json)
    os.remove(tmp_checkpoint_json)
    return lang_stats


def train(
        model,
        criterion,
        optimizer,
        train_loader,
        val_loader,
        opt,
        rl_criterion=None):

    infos = {'iter': 0,
             'epoch': 0,
             'start_epoch': 0,
             'best_score': float('-inf'),
             'best_iter': 0,
             'best_epoch': opt.max_epochs
             }

    checkpoint_checked = False
    rl_training = False
    seq_per_img = train_loader.get_seq_per_img()     
    infos_history = {}
    
    if os.path.exists(opt.start_from):
        if os.path.isdir(opt.start_from):
            # loading the same model file at a different experiment dir
            start_from_file = os.path.join(
                opt.start_from, os.path.basename(
                    opt.model_file))
        else:
            start_from_file = opt.start_from
        logger.info('Loading state from: %s', start_from_file)
        checkpoint = torch.load(start_from_file)
        model.load_state_dict(checkpoint['model'])
        infos = checkpoint['infos']
        infos['start_epoch'] = infos['epoch']
        checkpoint_checked = True # this epoch is already checked
    else:
        logger.info('No checkpoint found! Training from the scratch')
        
    if opt.use_rl == 1 and opt.use_rl_after == 0:
        opt.use_rl_after = infos['epoch']
        opt.use_cst_after = infos['epoch']
        train_loader.set_current_epoch(infos['epoch'])

    while True:
        t_start = time.time()
        model.train()
        data = train_loader.get_batch()
        feats = [feat for feat in data['feats']]
        labels = data['labels']
        masks = data['masks']
        sg_adj = data['sg_adj']
        sg_feat = data['sg_feat']
        sg_mask = data['sg_mask']
        seg_fea = data['seg_fea']
        seg_label = data['seg_label']
        sg_adj_g = data['sg_adj_g']
        sg_feat_g = data['sg_feat_g']

        if torch.cuda.is_available():
            feats = [feat.cuda() for feat in feats]
            labels = labels.cuda()
            masks = masks.cuda()
            sg_adj = sg_adj.cuda()
            sg_feat = sg_feat.cuda()
            sg_mask = sg_mask.cuda()
            seg_fea = seg_fea.cuda()
            seg_label = seg_label.cuda()
            sg_adj_g = sg_adj_g.cuda()
            sg_feat_g = sg_feat_g.cuda()

        frame_sg = [sg_adj, sg_feat, sg_mask]
        video_sg = [sg_adj_g, sg_feat_g]
        seg_out = [seg_fea, seg_label]

        # implement scheduled sampling
        opt.ss_prob = 0
        if opt.use_ss == 1 and infos['epoch'] >= opt.use_ss_after:
            annealing_prob = opt.ss_k / \
                (opt.ss_k + np.exp((infos['epoch'] - opt.use_ss_after) / opt.ss_k))
            opt.ss_prob = min(1 - annealing_prob, opt.ss_max_prob)
            model.set_ss_prob(opt.ss_prob)
            
        if opt.use_rl == 1 and infos[
                'epoch'] >= opt.use_rl_after and not rl_training:
            logger.info('Using RL objective...')
            rl_training = True
            bcmr_scorer = {
                'Bleu_4': Bleu(),
                'CIDEr': CiderD(df=opt.train_cached_tokens),
                'METEOR': Meteor(),
                'ROUGE_L': Rouge()
                }[opt.eval_metric]

        mixer_from = opt.mixer_from
        if opt.use_mixer == 1 and rl_training:
            
            # -1 for annealing
            if opt.mixer_from == -1:
                annealing_mixer = opt.seq_length - int(np.ceil((infos['epoch']-opt.use_rl_after+1)/float(opt.mixer_descrease_every)))
                mixer_from = max(1, annealing_mixer)
                
            model.set_mixer_from(mixer_from)
        
        scb_captions = opt.scb_captions
        if opt.use_cst == 1 and rl_training:
                
            # if opt.scb_captions is -1, then use the annealing value, 
            # otherwise, use the set value
            if opt.scb_captions == -1:
                annealing_robust = int(np.ceil((infos['epoch']-opt.use_cst_after+1)/float(opt.cst_increase_every)))
                scb_captions = min(annealing_robust, seq_per_img-1)
            
        optimizer.zero_grad()
        model.set_seq_per_img(seq_per_img)


        if rl_training:
            
            # using mixer
            pred, model_res, logprobs = model(feats, labels, frame_sg, video_sg, seg_out)
            
            if opt.use_cst == 0:
                # greedy decoding baseline in SCST paper
                greedy_baseline, _ = model.sample(feats, frame_sg, video_sg, seg_out, \
                                           {'sample_max': 1, 'expand_feat': opt.expand_feat})

 
            if opt.use_cst == 1:
                bcmrscores = data['bcmrscores']    
                reward, m_score, g_score = utils.get_cst_reward(model_res, data['gts'], bcmr_scorer,
                                                                            bcmrscores=bcmrscores,
                                                                            expand_feat=opt.expand_feat,
                                                                            seq_per_img=train_loader.get_seq_per_img(),
                                                                            scb_captions=scb_captions,
                                                                            scb_baseline=opt.scb_baseline,
                                                                            use_eos=opt.use_eos,
                                                                            use_mixer=opt.use_mixer
                                                                         )
            else:
                # use greedy baseline by default, compute self-critical reward
                reward, m_score, g_score = utils.get_self_critical_reward(model_res, greedy_baseline, data['gts'], bcmr_scorer,
                                                                          expand_feat=opt.expand_feat,
                                                                          seq_per_img=train_loader.get_seq_per_img(),
                                                                          use_eos=opt.use_eos)
                
            
            loss = rl_criterion(
                    model_res,
                    logprobs,
                    Variable(
                        torch.from_numpy(reward).float().cuda(),
                        requires_grad=False))
            
        else:

            pred = model(feats, labels, frame_sg, video_sg, seg_out)[0]
            loss = criterion(pred, labels[:, 1:], masks[:, 1:])
            

        loss.backward()
        clip_grad_norm_(model.parameters(), opt.grad_clip)
        optimizer.step()
        infos['TrainLoss'] = loss.item()
        infos['mixer_from'] = mixer_from
        infos['scb_captions'] = scb_captions
        
        if infos['iter'] % opt.print_log_interval == 0:
            elapsed_time = time.time() - t_start

            log_info = [('Epoch', infos['epoch']),
                        ('Iter', infos['iter']),
                        ('Loss', infos['TrainLoss'])]

            if rl_training:
                log_info += [('Reward', np.mean(reward[:, 0])),
                             ('{} (m)'.format(opt.eval_metric), m_score),
                             ('{} (b)'.format(opt.eval_metric), g_score)]
            
            if opt.use_ss == 1:
                log_info += [('ss_prob', opt.ss_prob)]
                
            if opt.use_mixer == 1:
                log_info += [('mixer_from', mixer_from)]    
                
            if opt.use_cst == 1:
                log_info += [('scb_captions', scb_captions)]
                
            log_info += [('Time', elapsed_time)]
            logger.info('%s', '\t'.join(
                ['{}: {}'.format(k, v) for (k, v) in log_info]))

        infos['iter'] += 1

        if infos['epoch'] < train_loader.get_current_epoch():
            infos['epoch'] = train_loader.get_current_epoch()
            checkpoint_checked = False
            learning_rate = utils.adjust_learning_rate(
                opt, optimizer, infos['epoch'] - infos['start_epoch'])
            logger.info('===> Learning rate: %f: ', learning_rate)
        
        with torch.no_grad():
            if (infos['epoch'] >= opt.save_checkpoint_from and
                    infos['epoch'] % opt.save_checkpoint_every == 0 and
                    not checkpoint_checked):
                # evaluate the validation performance
                results = validate(model, criterion, val_loader, opt)
                logger.info(
                    'Validation output: %s',
                    json.dumps(
                        results['scores'],
                        indent=4,
                        sort_keys=True))
                infos.update(results['scores'])

                check_model(model, opt, infos, infos_history)
                checkpoint_checked = True

        if (infos['epoch'] >= opt.max_epochs or
                infos['epoch'] - infos['best_epoch'] > opt.max_patience):
            logger.info('>>> Terminating...')
            break

    return infos


def validate(model, criterion, loader, opt):

    model.eval()
    loader.reset()

    num_videos = loader.get_num_videos()
    batch_size = loader.get_batch_size()
    num_iters = int(math.ceil(num_videos * 1.0 / batch_size))
    last_batch_size = num_videos % batch_size
    seq_per_img = loader.get_seq_per_img()
    model.set_seq_per_img(seq_per_img)

    loss_sum = 0
    logger.info(
        '#num_iters: %d, batch_size: %d, seg_per_image: %d',
        num_iters,
        batch_size,
        seq_per_img)
    predictions = []
    gt_avglogps = []
    test_avglogps = []
    for ii in range(num_iters):
        data = loader.get_batch()
        feats = [feat for feat in data['feats']]

        if loader.has_label:
            labels = data['labels']
            masks = data['masks']
            sg_adj = data['sg_adj']
            sg_feat = data['sg_feat']
            sg_mask = data['sg_mask']
            seg_fea = data['seg_fea']
            seg_label = data['seg_label']
            sg_adj_g = data['sg_adj_g']
            sg_feat_g = data['sg_feat_g']

        if ii == (num_iters - 1) and last_batch_size > 0:
            feats = [f[:last_batch_size] for f in feats]
            sg_adj = sg_adj[:last_batch_size]
            sg_feat = sg_feat[:last_batch_size]
            sg_mask = sg_mask[:last_batch_size]
            seg_fea = seg_fea[:last_batch_size]
            seg_label = seg_label[:last_batch_size]
            sg_adj_g = sg_adj_g[:last_batch_size]
            sg_feat_g = sg_feat_g[:last_batch_size]
            if loader.has_label:
                labels = labels[
                    :last_batch_size *
                    seq_per_img]  # labels shape is DxN
                masks = masks[:last_batch_size * seq_per_img]

        if torch.cuda.is_available():
            feats = [feat.cuda() for feat in feats]
            sg_adj = sg_adj.cuda()
            sg_feat = sg_feat.cuda()
            sg_mask = sg_mask.cuda()
            seg_fea = seg_fea.cuda()
            seg_label = seg_label.cuda()
            sg_adj_g = sg_adj_g.cuda()
            sg_feat_g = sg_feat_g.cuda()
            if loader.has_label:
                labels = labels.cuda()
                masks = masks.cuda()

        frame_sg = [sg_adj, sg_feat, sg_mask]
        video_sg = [sg_adj_g, sg_feat_g]
        seg_out = [seg_fea, seg_label]

        if loader.has_label:

            pred, gt_seq, _ = model(feats, labels, frame_sg, video_sg, seg_out)
            loss = criterion(pred, labels[:, 1:], masks[:, 1:])

            if opt.output_logp == 1:
                gt_avglogp = utils.compute_avglogp(gt_seq, outputs.data)
                gt_avglogps.extend(gt_avglogp)
                
            loss_sum += loss.item()

        seq, logseq = model.sample(feats, frame_sg, video_sg, seg_out, {'beam_size': opt.beam_size})
        
        sents = utils.decode_sequence(opt.vocab, seq)
        if opt.output_logp == 1:
            test_avglogp = utils.compute_avglogp(seq, logseq)
            test_avglogps.extend(test_avglogp)
        
        for jj, sent in enumerate(sents):
            if opt.output_logp == 1:
                entry = {'image_id': data['ids'][jj], 'caption': sent, 'avglogp': test_avglogp[jj]}
            else:
                entry = {'image_id': data['ids'][jj], 'caption': sent}
            predictions.append(entry)
            logger.info('[%d] video %s: %s' %
                         (jj, entry['image_id'], entry['caption']))

    loss = round(loss_sum / num_iters, 3)
    results = {}
    lang_stats = {}

    if opt.language_eval == 1 and loader.has_label:
        logger.info('>>> Language evaluating ...')
        tmp_checkpoint_json = os.path.join(
            opt.model_file + str(uuid.uuid4()) + '.json')
        json.dump(predictions, open(tmp_checkpoint_json, 'w'))
        lang_stats = utils.language_eval(
            loader.cocofmt_file, tmp_checkpoint_json)
        os.remove(tmp_checkpoint_json)

    results['predictions'] = predictions
    results['scores'] = {'Loss': -loss}
    results['scores'].update(lang_stats)
    
    if opt.output_logp == 1:
        avglogp = sum(test_avglogps)/float(len(test_avglogps))
        results['scores'].update({'avglogp': avglogp})
        
        gt_avglogps = np.array(gt_avglogps).reshape(-1, seq_per_img)
        assert num_videos == gt_avglogps.shape[0]
        
        gt_avglogps_file = opt.model_file.replace('.pth', '_gt_avglogps.pkl', 1)
        cPickle.dump(gt_avglogps, open(
            gt_avglogps_file, 'w'), protocol=cPickle.HIGHEST_PROTOCOL)
        
        logger.info('Wrote GT logp to: %s', gt_avglogps_file)
        
    return results


def test(model, criterion, loader, opt):
    with torch.no_grad():
        results = validate(model, criterion, loader, opt)
    logger.info('Test output: %s', json.dumps(results['scores'], indent=4))

    json.dump(results, open(opt.result_file, 'w'))
    logger.info('Wrote output caption to: %s ', opt.result_file)


def check_model(model, opt, infos, infos_history):

    if opt.eval_metric == 'MSRVTT':
        current_score = infos['Bleu_4'] + \
            infos['METEOR'] + infos['ROUGE_L'] + infos['CIDEr']
    else:
        current_score = infos[opt.eval_metric]

    # write the full model checkpoint as well if we did better than ever
    if current_score >= infos['best_score']:
        infos['best_score'] = current_score
        infos['best_iter'] = infos['iter']
        infos['best_epoch'] = infos['epoch']
        
        logger.info(
            '>>> Found new best [%s] score: %f, at iter: %d, epoch %d',
            opt.eval_metric,
            current_score,
            infos['iter'],
            infos['epoch'])

        torch.save({'model': model.state_dict(),
                    'infos': infos,
                    'opt': opt
                    }, opt.best_model_file)
        logger.info('Wrote checkpoint to: %s', opt.best_model_file)

    else:
        logger.info('>>> Current best [%s] score: %f, at iter %d, epoch %d',
                    opt.eval_metric, infos['best_score'],
                    infos['best_iter'],
                    infos['best_epoch'])

        torch.save({'model': model.state_dict(),
                    'infos': infos,
                    'opt': opt
                    }, opt.model_file)
        logger.info('Wrote checkpoint to: %s', opt.model_file)

    infos_history[infos['epoch']] = infos.copy()
    with open(opt.history_file, 'w') as of:
        json.dump(infos_history, of)
    logger.info('Updated history to: %s', opt.history_file)
    
if __name__ == '__main__':

    opt = opts.parse_opts()

    if not os.path.exists(opt.checkpoint_path):
        os.makedirs(opt.checkpoint_path)

    logging.basicConfig(level=getattr(logging, opt.loglevel.upper()),
                        format='%(asctime)s:%(levelname)s: %(message)s')

    logger.info(
        'Input arguments: %s',
        json.dumps(
            vars(opt),
            sort_keys=True,
            indent=4))

    # Set the random seed manually for reproducibility.
    np.random.seed(opt.seed)
    torch.manual_seed(opt.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(opt.seed)

    train_opt = {'label_h5': opt.train_label_h5,
                 'node_lmdb': opt.train_node_lmdb,
                 'batch_size': opt.batch_size,
                 'feat_h5': opt.train_feat_h5,
                 'cocofmt_file': opt.train_cocofmt_file,
                 'bcmrscores_pkl': opt.train_bcmrscores_pkl,
                 'eval_metric': opt.eval_metric,
                 'seq_per_img': opt.train_seq_per_img,
                 'num_chunks': opt.num_chunks,
                 'total_node': opt.total_node,
                 'scene_graph_path': opt.scene_graph_path,
                 'mode': 'train'
                 }

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

    train_loader = DataLoader(train_opt)
    val_loader = DataLoader(val_opt)
    test_loader = DataLoader(test_opt)

    opt.vocab = train_loader.get_vocab()
    opt.vocab_size = train_loader.get_vocab_size()
    opt.seq_length = train_loader.get_seq_length()
    opt.feat_dims = train_loader.get_feat_dims()
    opt.history_file = opt.model_file.replace('.pth', '_history.json', 1)
    
    logger.info('Building model...')
    model = CaptionModel(opt)

    xe_criterion = CrossEntropyCriterion()
    rl_criterion = RewardCriterion()


    if torch.cuda.is_available():
        model.cuda()

    logger.info('Start training...')
    start = datetime.now()

    optimizer = optim.Adam(model.parameters(), lr=opt.learning_rate)

    
    infos = train(
        model,
        xe_criterion,
        optimizer,
        train_loader,
        val_loader,
        opt,
        rl_criterion=rl_criterion)
    logger.info(
        'Best val %s score: %f. Best iter: %d. Best epoch: %d',
        opt.eval_metric,
        infos['best_score'],
        infos['best_iter'],
        infos['best_epoch'])

    logger.info('Training time: %s', datetime.now() - start)

    if opt.result_file:
        logger.info('Start testing...')
        start = datetime.now()

        logger.info('Loading model: %s', opt.model_file)
        checkpoint = torch.load(opt.model_file)
        model.load_state_dict(checkpoint['model'])

        test(model, xe_criterion, test_loader, opt)
        logger.info('Testing time: %s', datetime.now() - start)
