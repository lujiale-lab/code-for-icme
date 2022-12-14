import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
from torch.autograd import Variable
from lib.pytorch_misc import enumerate_by_image
from lib.word_vectors import obj_edge_vectors
import random
import numpy as np

from dataloaders.visual_genome200_keyrel_captions import VG200_Keyrel_captions
from dataloaders.visual_genome200 import VG200
from lib.get_dataset_counts import get_counts

# from config import XXXXX !!!!!!

def _PadRelFeats(rel_im_inds, rel_feats_all, num_relation, seq_per_img, pred_classes, obj_classes, rels,
                 freq_matrix=None):
    """

    :param rel_im_inds: torch.LongTensor, [num_rels, ]
    :param rel_feats_all: Variable, [num_rels, 4096]
    :param num_relation:
    :param seq_per_img:
    :return: rel_feats, [batch_size*seq_per_img, num_relation, 4096]
    """
    rel_feats = []
    categories_info_all = []
    for i, s, e in enumerate_by_image(rel_im_inds):
        rel_feats_i = rel_feats_all[s:e, :]
        pred_classes_i = pred_classes[s:e][:, None]
        rels_i = rels[s:e, :]
        subj_categories = obj_classes[rels_i[:, 1]][:, None]
        obj_categories = obj_classes[rels_i[:, 2]][:, None]
        categories_info = torch.cat((subj_categories, obj_categories, pred_classes_i), 1)

        # compute freqency baseline: reranking based on triplet frequency
        if freq_matrix is not None:
            categories_info_np = categories_info.data.cpu().numpy()
            freqs = []
            for cat in categories_info_np:
                freqs.append(freq_matrix[cat[0], cat[1], cat[2]])
            sort_index = torch.from_numpy(np.argsort(np.array(freqs) * -1)).cuda(rel_feats_all.get_device())
            rel_feats_i = rel_feats_i[sort_index, :]
            rels_i = rels_i[sort_index, :]
            categories_info = categories_info[sort_index, :]

        this_num_rel = e - s
        if num_relation <= this_num_rel:
            rel_feats_i = rel_feats_i[:num_relation, :]
            categories_info = categories_info[:num_relation]
        else:  # oversample
            sample_inds = torch.from_numpy(
                np.random.choice(np.arange(this_num_rel, dtype=np.int32), num_relation, replace=True)).long().cuda(
                rel_feats_all.get_device())
            rel_feats_i = rel_feats_i[sample_inds]
            categories_info = categories_info[sample_inds]

        rel_feats += [rel_feats_i[None, :, :]] * seq_per_img
        categories_info_all += [categories_info[None, :, :]] * seq_per_img
    rel_feats = torch.cat(rel_feats, 0)
    categories_info_all = torch.cat(categories_info_all, 0)
    return rel_feats, categories_info_all


def _EvalPadRelFeats(rel_im_inds, rel_feats_all, num_relation, pred_classes, obj_classes, rels, freq_matrix=None):
    """

    :param rel_im_inds: torch.LongTensor, [num_rels, ]
    :param rel_feats_all: Variable, [num_rels, 4096]
    :param num_relation:
    :param seq_per_img:
    :return: rel_feats, [batch_size, num_relation, 4096]
    """
    rel_feats = []
    categories_info_all = []
    for i, s, e in enumerate_by_image(rel_im_inds):
        rel_feats_i = rel_feats_all[s:e, :]
        pred_classes_i = pred_classes[s:e][:, None]
        rels_i = rels[s:e, :]
        subj_categories = obj_classes[rels_i[:, 1]][:, None]
        obj_categories = obj_classes[rels_i[:, 2]][:, None]
        categories_info = torch.cat((subj_categories, obj_categories, pred_classes_i), 1)

        # compute freqency baseline: reranking based on triplet frequency
        if freq_matrix is not None:
            categories_info_np = categories_info.data.cpu().numpy()
            freqs = []
            for cat in categories_info_np:
                freqs.append(freq_matrix[cat[0], cat[1], cat[2]])
            sort_index = torch.from_numpy(np.argsort(np.array(freqs) * -1)).cuda(rel_feats_all.get_device())
            rel_feats_i = rel_feats_i[sort_index, :]
            rels_i = rels_i[sort_index, :]
            categories_info = categories_info[sort_index, :]

        this_num_rel = e - s
        if num_relation <= this_num_rel:
            rel_feats_i = rel_feats_i[:num_relation, :]
            categories_info = categories_info[:num_relation]
        else:  # oversample
            sample_inds = torch.from_numpy(
                np.random.choice(np.arange(this_num_rel, dtype=np.int32), num_relation, replace=True)).long().cuda(
                rel_feats_all.get_device())
            rel_feats_i = rel_feats_i[sample_inds]
            categories_info = categories_info[sample_inds]

        rel_feats += [rel_feats_i[None, :, :]]
        categories_info_all += [categories_info[None, :, :]]
    rel_feats = torch.cat(rel_feats, 0)
    categories_info_all = torch.cat(categories_info_all, 0)
    return rel_feats, categories_info_all


class RelCaptionCore(nn.Module):
    def __init__(self, input_encoding_size, rnn_type='lstm', rnn_size=512, num_layers=1, drop_prob_lm=0.5,
                 fc_feat_size=4096, att_feat_size=512, triplet_embed_dim=-1, embed_triplet=True):
        super(RelCaptionCore, self).__init__()
        self.input_encoding_size = input_encoding_size
        self.rnn_type = rnn_type
        self.rnn_size = rnn_size
        self.num_layers = num_layers
        self.drop_prob_lm = drop_prob_lm
        self.fc_feat_size = fc_feat_size
        self.att_feat_size = att_feat_size
        self.triplet_embed_dim = triplet_embed_dim
        self.embed_triplet = embed_triplet

        if self.embed_triplet:
            assert self.triplet_embed_dim > 0
            rel_input_size = self.fc_feat_size + 3 * self.triplet_embed_dim
        else:
            rel_input_size = self.fc_feat_size

        self.rnn_1 = getattr(nn, self.rnn_type.upper())(self.input_encoding_size + rel_input_size,
                                                        self.rnn_size, self.num_layers, bias=False,
                                                        dropout=self.drop_prob_lm)
        self.rnn_2 = getattr(nn, self.rnn_type.upper())(rel_input_size, self.rnn_size, self.num_layers, bias=False,
                                                        dropout=self.drop_prob_lm)
        self.ctx2att = nn.Linear(rel_input_size, self.att_feat_size)
        self.h2att = nn.Linear(self.rnn_size, self.att_feat_size)
        self.att_net = nn.Linear(self.att_feat_size, 1)

    def forward(self, xt, rel_feats, state):
        mean_rel_feats = torch.mean(rel_feats, dim=1)  # batch x fc_feat_size
        out_1, state_1 = self.rnn_1(torch.cat([xt, mean_rel_feats], 1).unsqueeze(0), state)  # 1 x batch x rnn_size
        h_1, _ = state_1
        s_h_1 = h_1.squeeze(0)  # batch x rnn_size

        attention = F.tanh(self.ctx2att(rel_feats) + self.h2att(s_h_1).unsqueeze(1))  # batch x Nr x att_feat_size
        attention = self.att_net(attention).squeeze(2)  # batch x Nr
        weight = F.softmax(attention).unsqueeze(1)  # batch x 1 x Nr
        att_input = torch.bmm(weight, rel_feats).squeeze(1)  # batch x fc_feat_size
        out_2, state_2 = self.rnn_2(att_input.unsqueeze(0), state_1)
        return out_2.squeeze(0), state_2


class RelCaptionModel(nn.Module):
    def __init__(self, vocabs, vocab_size, input_encoding_size, rnn_type='lstm', rnn_size=512, num_layers=1,
                 drop_prob_lm=0.5,
                 seq_length=16, seq_per_img=5, fc_feat_size=4096, att_feat_size=512, num_relation=20,
                 object_classes=None, predicate_classes=None, triplet_embed_dim=-1, embed_triplet=True,
                 freq_bl=False):
        super(RelCaptionModel, self).__init__()
        self.vocabs = vocabs
        self.vocabs['0'] = '__SENTSIGN__'  ## ix
        self.vocabs = {i: self.vocabs[str(i)] for i in range(len(self.vocabs))}
        vocab_list = [self.vocabs[i] for i in range(len(self.vocabs))]
        self.vocab_size = vocab_size + 1  # including all the words and <UNK>, and 0 for <start>/<end>

        self.input_encoding_size = input_encoding_size
        self.rnn_type = rnn_type
        self.rnn_size = rnn_size
        self.num_layers = num_layers
        self.drop_prob_lm = drop_prob_lm
        self.seq_length = seq_length
        self.fc_feat_size = fc_feat_size
        self.ss_prob = 0.0  # Schedule sampling probability
        self.num_relation_per_img = num_relation
        self.seq_per_img = seq_per_img
        self.embed_triplet = embed_triplet
        self.triplet_embed_dim = triplet_embed_dim

        self.freq_bl = freq_bl

        self.linear = nn.Linear(self.fc_feat_size, self.num_layers * self.rnn_size)  # feature to rnn_size
        embed_vec = obj_edge_vectors(vocab_list, wv_dim=self.input_encoding_size)
        self.embed = nn.Embedding(self.vocab_size, self.input_encoding_size)
        self.embed.weight.data = embed_vec.clone()

        if self.embed_triplet:
            assert object_classes is not None and predicate_classes is not None
            object_embed_vec = obj_edge_vectors(object_classes, wv_dim=self.triplet_embed_dim)
            predicate_embed_vec = obj_edge_vectors(predicate_classes, wv_dim=self.triplet_embed_dim)
            self.object_embed = nn.Embedding(len(object_classes), self.triplet_embed_dim)
            self.object_embed.weight.data = object_embed_vec.clone()
            self.predicate_embed = nn.Embedding(len(predicate_classes), self.triplet_embed_dim)
            self.predicate_embed.weight.data = predicate_embed_vec.clone()

        self.logit = nn.Linear(self.rnn_size, self.vocab_size)
        self.dropout = nn.Dropout(self.drop_prob_lm)

        self.core = RelCaptionCore(input_encoding_size, rnn_type, rnn_size, num_layers, drop_prob_lm,
                                   fc_feat_size, att_feat_size, triplet_embed_dim, embed_triplet)

        if self.freq_bl:
            self.freq_matrix, _ = get_counts(train_data=VG200(mode='train', filter_duplicate_rels=False, num_val_im=1000), must_overlap=True)
        else:
            self.freq_matrix = None

        self.init_weights()

    def init_weights(self):
        initrange = 0.1
        self.logit.bias.data.fill_(0)
        self.logit.weight.data.uniform_(-initrange, initrange)

    def init_hidden(self, fc_feats):
        image_map = self.linear(fc_feats).view(-1, self.num_layers, self.rnn_size).transpose(0, 1)
        if self.rnn_type == 'lstm':
            return (image_map, image_map)
        else:
            return image_map

    def forward(self, rel_feats_all, pred_classes, rels, obj_classes, image_fmap, seq_labels, mask_labels):
        # pad the rel_features
        rel_im_inds = rels[:, 0]
        ###
        # rel_feats: (5B) x Nr x 4096
        # categories_info: (5B) x Nr x 3
        rel_feats, categories_info = _PadRelFeats(rel_im_inds, rel_feats_all, self.num_relation_per_img,
                                                  self.seq_per_img,
                                                  pred_classes, obj_classes, rels, self.freq_matrix)
        # adjust the input image_fmap: (5B, 4096)
        fc_feats = image_fmap[None, :, :].repeat(self.seq_per_img, 1, 1).transpose(0, 1).contiguous().view(-1,
                                                                                                           image_fmap.size(
                                                                                                               1))

        state = self.init_hidden(fc_feats)  # num_layer x 5B x 512

        bs = categories_info.size(0)

        if self.embed_triplet:
            subobj_embed_feats = \
                self.object_embed(categories_info.view(-1, 3)[:, :2]).view(bs * self.num_relation_per_img, -1). \
                    view(bs, self.num_relation_per_img, -1)
            predicate_embed_feats = self.predicate_embed(categories_info.view(-1, 3)[:, 2:3]).squeeze(1). \
                view(bs, self.num_relation_per_img, -1)
            rel_feats = torch.cat([rel_feats, subobj_embed_feats, predicate_embed_feats], 2)

        outputs = []
        for i in range(seq_labels.size(1) - 1):
            if self.training and i >= 1 and self.ss_prob > 0.0:
                sample_prob = rel_feats.data.new(bs).uniform_(0, 1)
                sample_mask = sample_prob < self.ss_prob
                if sample_mask.sum() == 0:
                    it = seq_labels[:, i].clone()
                else:
                    sample_ind = sample_mask.nonzero().view(-1)
                    it = seq_labels[:, i].data.clone()
                    prob_prev = torch.exp(outputs[-1].data)
                    it.index_copy_(0, sample_ind, torch.multinomial(prob_prev, 1).view(-1).index_select(0, sample_ind))
                    it = Variable(it, requires_grad=False)
            else:
                it = seq_labels[:, i].clone()
            if i >= 1 and seq_labels[:, i].data.sum() == 0:
                break

            xt = self.embed(it)

            output, state = self.core(xt, rel_feats, state)
            output = F.log_softmax(self.logit(self.dropout(output)))
            outputs.append(output)
        return torch.cat([_.unsqueeze(1) for _ in outputs], 1)  # b x T x vocab_size

    def get_logprobs_state(self, it, tmp_rel_feats, state):
        # 'it' is Variable contraining a word index
        xt = self.embed(it)

        output, state = self.core(xt, tmp_rel_feats, state)
        logprobs = F.log_softmax(self.logit(self.dropout(output)))

        return logprobs, state


    def sample(self, rel_feats_all, pred_classes, rels, obj_classes, image_fmap, beam_size=3, sample_max=1,
               temperature=1.0):
        if beam_size > 1:
            return self.sample_beam(rel_feats_all, pred_classes, rels, obj_classes, image_fmap, beam_size)

        # prepare data, now each image only have one copy instead of seq_per_image
        # pad the rel_features
        rel_im_inds = rels[:, 0]
        ###
        # rel_feats: (B) x Nr x 4096
        # categories_info: (B) x Nr x 3
        rel_feats, categories_info = _EvalPadRelFeats(rel_im_inds, rel_feats_all, self.num_relation_per_img,
                                                      pred_classes, obj_classes, rels, self.freq_matrix)
        # adjust the input image_fmap: (B, 4096)
        fc_feats = image_fmap[None, :, :].transpose(0, 1).contiguous().view(-1, image_fmap.size(1))
        state = self.init_hidden(fc_feats)  # num_layer x 5B x 512
        bs = categories_info.size(0)

        if self.embed_triplet:
            subobj_embed_feats = \
                self.object_embed(categories_info.view(-1, 3)[:, :2]).view(bs * self.num_relation_per_img, -1). \
                    view(bs, self.num_relation_per_img, -1)
            predicate_embed_feats = self.predicate_embed(categories_info.view(-1, 3)[:, 2:3]).squeeze(1). \
                view(bs, self.num_relation_per_img, -1)
            rel_feats = torch.cat([rel_feats, subobj_embed_feats, predicate_embed_feats], 2)

        seq = []
        seqLogprobs = []
        for t in range(self.seq_length + 1):
            if t == 0:  # input <start>
                it = rel_feats.data.new(bs).long().zero_()  # (batch, )
            elif sample_max:
                sampleLogprobs, it = torch.max(logprobs.data, 1)  # (batch, )
                it = it.view(-1).long()  # (batch, )
            else:
                if temperature == 1.0:
                    prob_prev = torch.exp(logprobs.data).cpu()
                else:
                    prob_prev = torch.exp(torch.div(logprobs.data, temperature)).cpu()
                it = torch.multinomial(prob_prev, 1).cuda()
                sampleLogprobs = logprobs.gather(1, Variable(it, requires_grad=False))
                it = it.view(-1).long()
            xt = self.embed(Variable(it, requires_grad=False))

            if t >= 1:
                if t == 1:
                    unfinished = it > 0
                else:
                    unfinished = unfinished * (it > 0)
                if unfinished.sum() == 0:
                    break
                it = it * unfinished.type_as(it)
                seq.append(it)
                seqLogprobs.append(sampleLogprobs.view(-1))
            output, state = self.core(xt, rel_feats, state)
            logprobs = F.log_softmax(self.logit(self.dropout(output)))
        return torch.cat([_.unsqueeze(1) for _ in seq], 1), torch.cat([_.unsqueeze(1) for _ in seqLogprobs], 1)  # B x T

    def sample_beam(self, rel_feats_all, pred_classes, rels, obj_classes, image_fmap, beam_size):
        # prepare data, now each image only have one copy instead of seq_per_image
        # pad the rel_features
        rel_im_inds = rels[:, 0]
        ###
        # rel_feats: (B) x Nr x 4096
        # categories_info: (B) x Nr x 3
        rel_feats, categories_info = _EvalPadRelFeats(rel_im_inds, rel_feats_all, self.num_relation_per_img,
                                                      pred_classes, obj_classes, rels, self.freq_matrix)
        # adjust the input image_fmap: (B, 4096)
        fc_feats = image_fmap[None, :, :].transpose(0, 1).contiguous().view(-1, image_fmap.size(1))
        bs = categories_info.size(0)

        if self.embed_triplet:
            subobj_embed_feats = \
                self.object_embed(categories_info.view(-1, 3)[:, :2]).view(bs * self.num_relation_per_img, -1). \
                    view(bs, self.num_relation_per_img, -1)
            predicate_embed_feats = self.predicate_embed(categories_info.view(-1, 3)[:, 2:3]).squeeze(1). \
                view(bs, self.num_relation_per_img, -1)
            rel_feats = torch.cat([rel_feats, subobj_embed_feats, predicate_embed_feats], 2)

        assert beam_size <= self.vocab_size + 1, 'lets assume this for now, otherwise this corner case causes a ' \
                                                 'few headaches down the road. can be dealt with in future if needed'
        seq = torch.LongTensor(self.seq_length, bs).zero_()                                       # seq_length x bs
        seqLogprobs = torch.FloatTensor(self.seq_length, bs)

        # lets process every image independently for now, for simplicity
        self.done_beams = [[] for _ in range(bs)]
        for k in range(bs):
            tmp_fc_feats = fc_feats[k:k+1].expand(beam_size, self.fc_feat_size)
            tmp_rel_feats = rel_feats[k:k+1].expand(*((beam_size,) + rel_feats.size()[1:])).contiguous()
            state = self.init_hidden(tmp_fc_feats)

            beam_seq = torch.LongTensor(self.seq_length, beam_size).zero_()
            beam_seq_logprobs = torch.FloatTensor(self.seq_length, beam_size).zero_()
            beam_logprobs_sum = torch.zeros(beam_size)  # running sum of logprobs for each beam
            done_beams = []
            for t in range(1):
                if t == 0:  # input <bos>
                    it = fc_feats.data.new(beam_size).long().zero_()
                    xt = self.embed(Variable(it, requires_grad=False))
                output, state = self.core(xt, tmp_rel_feats, state)
                logprobs = F.log_softmax(self.logit(self.dropout(output)))
            self.done_beams[k] = self.beam_search(state, logprobs, tmp_rel_feats, beam_size=beam_size)
            seq[:, k] = self.done_beams[k][0]['seq']  # the first beam has highest cumulative score
            seqLogprobs[:, k] = self.done_beams[k][0]['logps']
        # return the samples and their log likelihoods
        return seq.transpose(0, 1), seqLogprobs.transpose(0, 1)

    def beam_search(self, state, logprobs, *args, **kwargs):

        def beam_step(logprobsf, beam_size, t, beam_seq, beam_seq_logprobs, beam_logprobs_sum, state):
            # INPUTS:
            # logprobsf: probabilities augmented after diversity
            # beam_size: obvious
            # t        : time instant
            # beam_seq : tensor contanining the beams
            # beam_seq_logprobs: tensor contanining the beam logprobs
            # beam_logprobs_sum: tensor contanining joint logprobs
            # OUPUTS:
            # beam_seq : tensor containing the word indices of the decoded captions
            # beam_seq_logprobs : log-probability of each decision made, same size as beam_seq
            # beam_logprobs_sum : joint log-probability of each beam

            ys, ix = torch.sort(logprobsf, 1, True)
            candidates = []
            cols = min(beam_size, ys.size(1))
            rows = beam_size
            if t == 0:
                rows = 1
            for c in range(cols):  # for each column (word, essentially)
                for q in range(rows):  # for each beam expansion
                    # compute logprob of expanding beam q with word in (sorted) position c
                    local_logprob = ys[q, c]
                    candidate_logprob = beam_logprobs_sum[q] + local_logprob
                    candidates.append(dict(c=ix[q, c], q=q,
                                           p=candidate_logprob,
                                           r=local_logprob))
            candidates = sorted(candidates, key=lambda x: -x['p'])

            new_state = [_.clone() for _ in state]
            # beam_seq_prev, beam_seq_logprobs_prev
            if t >= 1:
                # we''ll need these as reference when we fork beams around
                beam_seq_prev = beam_seq[:t].clone()
                beam_seq_logprobs_prev = beam_seq_logprobs[:t].clone()
            for vix in range(beam_size):
                v = candidates[vix]
                # fork beam index q into index vix
                if t >= 1:
                    beam_seq[:t, vix] = beam_seq_prev[:, v['q']]
                    beam_seq_logprobs[:t, vix] = beam_seq_logprobs_prev[:, v['q']]
                # rearrange recurrent states
                for state_ix in range(len(new_state)):
                    #  copy over state in previous beam q to new beam at vix
                    new_state[state_ix][:, vix] = state[state_ix][:, v['q']]  # dimension one is time step
                # append new end terminal at the end of this beam
                beam_seq[t, vix] = v['c']  # c'th word is the continuation
                beam_seq_logprobs[t, vix] = v['r']  # the raw logprob here
                beam_logprobs_sum[vix] = v['p']  # the new (sum) logprob along this beam
            state = new_state
            return beam_seq, beam_seq_logprobs, beam_logprobs_sum, state, candidates


        beam_size = kwargs['beam_size']
        beam_seq = torch.LongTensor(self.seq_length, beam_size).zero_()
        beam_seq_logprobs = torch.FloatTensor(self.seq_length, beam_size).zero_()
        # running sum of logprobs for each beam
        beam_logprobs_sum = torch.zeros(beam_size)
        done_beams = []

        for t in range(self.seq_length):
            """pem a beam merge. that is,
            for every previous beam we now many new possibilities to branch out
            we need to resort our beams to maintain the loop invariant of keeping
            the top beam_size most likely sequences."""
            logprobsf = logprobs.data.float()  # lets go to CPU for more efficiency in indexing operations
            # suppress UNK tokens in the decoding
            logprobsf[:, logprobsf.size(1) - 1] = logprobsf[:, logprobsf.size(1) - 1] - 1000

            beam_seq, \
            beam_seq_logprobs, \
            beam_logprobs_sum, \
            state, \
            candidates_divm = beam_step(logprobsf,
                                        beam_size,
                                        t,
                                        beam_seq,
                                        beam_seq_logprobs,
                                        beam_logprobs_sum,
                                        state)
            for vix in range(beam_size):
                # if time's up... or if end token is reached then copy beams
                if beam_seq[t, vix] == 0 or t == self.seq_length - 1:
                    final_beam = {
                        'seq': beam_seq[:, vix].clone(),
                        'logps': beam_seq_logprobs[:, vix].clone(),
                        'p': beam_logprobs_sum[vix]
                    }
                    done_beams.append(final_beam)
                    # don't continue beams from finished sequences
                    beam_logprobs_sum[vix] = -1000
            # encode as vectors
            it = beam_seq[t]
            logprobs, state = self.get_logprobs_state(Variable(it.cuda()), *(args + (state,)))

        done_beams = sorted(done_beams, key=lambda x: -x['p'])[:beam_size]
        return done_beams