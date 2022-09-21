# modified from https://github.com/rowanz/neural-motifs
import numpy as np
import torch
from torch import nn
from torch.nn.utils.rnn import PackedSequence
from torch.nn import functional as F
from .make_layers import make_fc
from ..motif.utils_motifs import obj_edge_vectors, center_x, sort_by_score, to_onehot, get_dropout_mask, encode_box_info, cat


class IMPContext(nn.Module):
    def __init__(self, config, num_obj, num_rel, in_channels, hidden_dim=512, num_iter=3):
        super(IMPContext, self).__init__()
        self.cfg = config
        self.num_obj = num_obj + 1
        self.num_rel = num_rel + 1
        self.pooling_dim = config.MODEL.ROI_SCENEGRAPH_HEAD.CONTEXT_POOLING_DIM
        self.hidden_dim = hidden_dim
        self.num_iter = num_iter
        # mode
        if self.cfg.MODEL.ROI_SCENEGRAPH_HEAD.USE_GT_BOX:
            if self.cfg.MODEL.ROI_SCENEGRAPH_HEAD.USE_GT_OBJECT_LABEL:
                self.mode = 'predcls'
            else:
                self.mode = 'sgcls'
        else:
            self.mode = 'sgdet'

        # self.rel_fc = make_fc(hidden_dim, self.num_rel)
        # self.obj_fc = make_fc(hidden_dim, self.num_obj)

        # self.obj_unary = make_fc(in_channels, hidden_dim)
        # self.edge_unary = make_fc(self.pooling_dim, hidden_dim)

        self.rel_fc = nn.Linear(hidden_dim, self.num_rel)
        self.obj_fc = nn.Linear(hidden_dim, self.num_obj)

        self.obj_unary = nn.Linear(in_channels, hidden_dim)
        self.edge_unary = nn.Linear(self.pooling_dim, hidden_dim)

        self.edge_gru = nn.GRUCell(input_size=hidden_dim, hidden_size=hidden_dim)
        self.node_gru = nn.GRUCell(input_size=hidden_dim, hidden_size=hidden_dim)

        self.sub_vert_w_fc = nn.Sequential(nn.Linear(hidden_dim*2, 1), nn.Sigmoid())
        self.obj_vert_w_fc = nn.Sequential(nn.Linear(hidden_dim*2, 1), nn.Sigmoid())
        self.out_edge_w_fc = nn.Sequential(nn.Linear(hidden_dim*2, 1), nn.Sigmoid())
        self.in_edge_w_fc = nn.Sequential(nn.Linear(hidden_dim*2, 1), nn.Sigmoid())

    
    def forward(self, x, proposals, union_features, rel_pair_idxs, logger=None):
        num_objs = [len(b) for b in proposals]

        obj_rep = self.obj_unary(x)
        rel_rep = F.relu(self.edge_unary(union_features))

        obj_count = obj_rep.shape[0]
        rel_count = rel_rep.shape[0]

        # generate sub-rel-obj mapping
        sub2rel = torch.zeros(obj_count, rel_count).to(obj_rep.device).float()
        obj2rel = torch.zeros(obj_count, rel_count).to(obj_rep.device).float()
        obj_offset = 0
        rel_offset = 0
        sub_global_inds = []
        obj_global_inds = []
        for pair_idx, num_obj in zip(rel_pair_idxs, num_objs):
            num_rel = pair_idx.shape[0]
            sub_idx = pair_idx[:,0].contiguous().long().view(-1) + obj_offset
            obj_idx = pair_idx[:,1].contiguous().long().view(-1) + obj_offset
            rel_idx = torch.arange(num_rel).to(obj_rep.device).long().view(-1) + rel_offset

            sub_global_inds.append(sub_idx)
            obj_global_inds.append(obj_idx)

            sub2rel[sub_idx, rel_idx] = 1.0
            obj2rel[obj_idx, rel_idx] = 1.0

            obj_offset += num_obj
            rel_offset += num_rel

        sub_global_inds = torch.cat(sub_global_inds, dim=0)
        obj_global_inds = torch.cat(obj_global_inds, dim=0)

        # iterative message passing
        hx_obj = torch.zeros(obj_count, self.hidden_dim, requires_grad=False).to(obj_rep.device).float()
        hx_rel = torch.zeros(rel_count, self.hidden_dim, requires_grad=False).to(obj_rep.device).float()

        vert_factor = [self.node_gru(obj_rep, hx_obj)]
        edge_factor = [self.edge_gru(rel_rep, hx_rel)]

        for i in range(self.num_iter):
            # compute edge context
            sub_vert = vert_factor[i][sub_global_inds]
            obj_vert = vert_factor[i][obj_global_inds]
            weighted_sub = self.sub_vert_w_fc(
                torch.cat((sub_vert, edge_factor[i]), 1)) * sub_vert
            weighted_obj = self.obj_vert_w_fc(
                torch.cat((obj_vert, edge_factor[i]), 1)) * obj_vert

            edge_factor.append(self.edge_gru(weighted_sub + weighted_obj, edge_factor[i]))

            # Compute vertex context
            pre_out = self.out_edge_w_fc(torch.cat((sub_vert, edge_factor[i]), 1)) * edge_factor[i]
            pre_in = self.in_edge_w_fc(torch.cat((obj_vert, edge_factor[i]), 1)) * edge_factor[i]
            vert_ctx = sub2rel @ pre_out + obj2rel @ pre_in
            vert_factor.append(self.node_gru(vert_ctx, vert_factor[i]))

        if self.mode == 'predcls':
            obj_labels = cat([proposal.pred_classes for proposal in proposals], dim=0)
            obj_dists = to_onehot(obj_labels, self.num_obj)
        else:
            obj_dists = self.obj_fc(vert_factor[-1])

        rel_dists = self.rel_fc(edge_factor[-1])

        return obj_dists, rel_dists



class IMPSegmentationContext(IMPContext):
    def __init__(self, config, num_obj, num_rel, in_channels, hidden_dim=512, num_iter=3, mask_obj_classes=None):
        super(IMPSegmentationContext, self).__init__(config, num_obj, num_rel, in_channels, hidden_dim=hidden_dim, num_iter=num_iter)

    def forward(self, x, proposals, union_features, rel_pair_idxs, logger=None, mask_box_features=None, masks=None, segmentation_step=False, return_masks=False):
        num_objs = [len(b) for b in proposals]
        import ipdb; ipdb.set_trace()
        obj_rep = self.obj_unary(x)
        obj_count = obj_rep.shape[0]
        if not segmentation_step:
            rel_rep = F.relu(self.edge_unary(union_features))
            rel_count = rel_rep.shape[0]

            # generate sub-rel-obj mapping
            sub2rel = torch.zeros(obj_count, rel_count).to(obj_rep.device).float()
            obj2rel = torch.zeros(obj_count, rel_count).to(obj_rep.device).float()
            obj_offset = 0
            rel_offset = 0
            sub_global_inds = []
            obj_global_inds = []
            for pair_idx, num_obj in zip(rel_pair_idxs, num_objs):
                num_rel = pair_idx.shape[0]
                sub_idx = pair_idx[:,0].contiguous().long().view(-1) + obj_offset
                obj_idx = pair_idx[:,1].contiguous().long().view(-1) + obj_offset
                rel_idx = torch.arange(num_rel).to(obj_rep.device).long().view(-1) + rel_offset

                sub_global_inds.append(sub_idx)
                obj_global_inds.append(obj_idx)

                sub2rel[sub_idx, rel_idx] = 1.0
                obj2rel[obj_idx, rel_idx] = 1.0

                obj_offset += num_obj
                rel_offset += num_rel

            sub_global_inds = torch.cat(sub_global_inds, dim=0)
            obj_global_inds = torch.cat(obj_global_inds, dim=0)

        # iterative message passing
        hx_obj = torch.zeros(obj_count, self.hidden_dim, requires_grad=False).to(obj_rep.device).float()
        vert_factor = [self.node_gru(obj_rep, hx_obj)]
        if not segmentation_step:
            hx_rel = torch.zeros(rel_count, self.hidden_dim, requires_grad=False).to(obj_rep.device).float()
            edge_factor = [self.edge_gru(rel_rep, hx_rel)]

        for i in range(self.num_iter):
            # compute edge context
            sub_vert = vert_factor[i][sub_global_inds]
            obj_vert = vert_factor[i][obj_global_inds]
            weighted_sub = self.sub_vert_w_fc(
                torch.cat((sub_vert, edge_factor[i]), 1)) * sub_vert
            weighted_obj = self.obj_vert_w_fc(
                torch.cat((obj_vert, edge_factor[i]), 1)) * obj_vert

            edge_factor.append(self.edge_gru(weighted_sub + weighted_obj, edge_factor[i]))

            # Compute vertex context
            pre_out = self.out_edge_w_fc(torch.cat((sub_vert, edge_factor[i]), 1)) * edge_factor[i]
            pre_in = self.in_edge_w_fc(torch.cat((obj_vert, edge_factor[i]), 1)) * edge_factor[i]
            vert_ctx = sub2rel @ pre_out + obj2rel @ pre_in
            vert_factor.append(self.node_gru(vert_ctx, vert_factor[i]))

        if self.mode == 'predcls':
            obj_labels = cat([proposal.pred_classes for proposal in proposals], dim=0)
            obj_dists = to_onehot(obj_labels, self.num_obj)
        else:
            obj_dists = self.obj_fc(vert_factor[-1])

        rel_dists = self.rel_fc(edge_factor[-1])

        return obj_dists, rel_dists