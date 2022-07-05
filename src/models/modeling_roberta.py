# coding=utf-8
# Based on:
# HuggingFace Transformers
# See https://github.com/huggingface/transformers/LICENSE for details.
#################################################
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""PyTorch RoBERTa model. """


import logging
import random
import pickle
import time

import torch
import torch.nn as nn
from torch.nn import CrossEntropyLoss, MSELoss, BCEWithLogitsLoss, L1Loss
import torchvision.models as models
import torch.nn.functional as F

from .configuration_roberta import RobertaConfig
from .file_utils import add_start_docstrings, add_start_docstrings_to_callable
from .modeling_bert import (
    BertEmbeddings,
    BertLayerNorm,
    BertModel,
    BertPreTrainedModel,
    gelu,
    BertPooler,
)

ROBERTA_PRETRAINED_MODEL_ARCHIVE_MAP = {}
logger = logging.getLogger(__name__)


log_sigmoid = nn.LogSigmoid()
sigmoid = nn.Sigmoid()


class RobertaEmbeddings(BertEmbeddings):
    """
    Same as BertEmbeddings with a tiny tweak for positional embeddings indexing.
    """

    def __init__(self, config, args):
        super().__init__(config)
        self.padding_idx = 1
        self.args = args

        embed_hidden_size = config.hidden_size

        self.word_embeddings = None

        if not args.no_pos_ids:
            (
                self.inc_position_embeddings,
                self.dec_position_embeddings,
                self.center_position_embeddings,
            ) = [
                nn.Embedding(
                    config.max_position_embeddings,
                    embed_hidden_size,
                    padding_idx=self.padding_idx,
                )
                for _ in range(3)
            ]

        if not args.no_scene_ids:
            (
                self.inc_scene_embeddings,
                self.dec_scene_embeddings,
                self.center_scene_embeddings,
            ) = [
                nn.Embedding(
                    config.max_position_embeddings,
                    embed_hidden_size,
                    padding_idx=self.padding_idx,
                )
                for _ in range(3)
            ]

        if not args.no_link_ids:
            self.link_embeddings = nn.Embedding(
                config.max_position_embeddings,
                embed_hidden_size,
                padding_idx=self.padding_idx,
            )

        self.spatial_embeddings = nn.Linear(5, embed_hidden_size)

        self.LayerNorm = BertLayerNorm(embed_hidden_size, eps=config.layer_norm_eps)
        # self.token_type_embeddings = nn.Embedding(
        #     config.type_vocab_size, embed_hidden_size)

        self.reduction = nn.Linear(args.feat_dim, config.hidden_size)

        self.masked_embedding = nn.ParameterList(
            [nn.Parameter(torch.zeros((args.action_feat_dim,)))]
        )

    def forward(
        self,
        inc_scene_ids=None,
        dec_scene_ids=None,
        center_scene_ids=None,
        link_ids=None,
        token_type_ids=None,
        inc_position_ids=None,
        dec_position_ids=None,
        center_position_ids=None,
        inputs_embeds=None,
        spatial_codes=None,
    ):
        # if position_ids is None:
        #     assert False
        #     if input_ids is not None:
        #         # Create the position ids from the input token ids. Any padded tokens remain padded.
        #         position_ids = self.create_position_ids_from_input_ids(input_ids).to(input_ids.device)
        #     else:
        #         position_ids = self.create_position_ids_from_inputs_embeds(inputs_embeds)

        return super().forward(
            inc_scene_ids=inc_scene_ids,
            dec_scene_ids=dec_scene_ids,
            center_scene_ids=center_scene_ids,
            link_ids=link_ids,
            token_type_ids=token_type_ids,
            inc_position_ids=inc_position_ids,
            dec_position_ids=dec_position_ids,
            center_position_ids=center_position_ids,
            inputs_embeds=inputs_embeds,
            spatial_codes=spatial_codes,
        )

    def create_position_ids_from_input_ids(self, x):
        """Replace non-padding symbols with their position numbers. Position numbers begin at
        padding_idx+1. Padding symbols are ignored. This is modified from fairseq's
        `utils.make_positions`.

        :param torch.Tensor x:
        :return torch.Tensor:
        """
        mask = x.ne(self.padding_idx).long()
        incremental_indicies = torch.cumsum(mask, dim=1) * mask
        return incremental_indicies + self.padding_idx

    def create_position_ids_from_inputs_embeds(self, inputs_embeds):
        """We are provided embeddings directly. We cannot infer which are padded so just generate
        sequential position ids.

        :param torch.Tensor inputs_embeds:
        :return torch.Tensor:
        """
        input_shape = inputs_embeds.size()[:-1]
        sequence_length = input_shape[1]

        position_ids = torch.arange(
            self.padding_idx + 1,
            sequence_length + self.padding_idx + 1,
            dtype=torch.long,
            device=inputs_embeds.device,
        )
        return position_ids.unsqueeze(0).expand(input_shape)


class RobertaModel(BertModel):
    """
    This class overrides :class:`~transformers.BertModel`. Please check the
    superclass for the appropriate documentation alongside usage examples.
    """

    config_class = RobertaConfig
    pretrained_model_archive_map = ROBERTA_PRETRAINED_MODEL_ARCHIVE_MAP
    base_model_prefix = "roberta"

    def __init__(self, config, args):
        super().__init__(config)

        self.embeddings = RobertaEmbeddings(config, args=args)
        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value


bce_with_logits_loss = BCEWithLogitsLoss()


def MaskedBCEWithLogitsLoss(preds, labels):
    ignore = labels == -100

    assert (labels == -100).sum() + (labels == 0).sum() + (
        labels == 1
    ).sum() == labels.shape[0] * labels.shape[1]
    return bce_with_logits_loss(preds[~ignore], labels[~ignore])


class RobertaForMaskedLM(BertPreTrainedModel):
    config_class = RobertaConfig
    pretrained_model_archive_map = ROBERTA_PRETRAINED_MODEL_ARCHIVE_MAP
    base_model_prefix = "roberta"

    def __init__(self, config, args):  ###############   STT-1 #########
        super().__init__(config)
        self.args = args

        config.max_position_embeddings = args.max_position_embeddings
        config.num_hidden_layers = args.num_hidden_layers
        config.num_attention_heads = args.num_attention_heads
        config.feat_dim = args.feat_dim
        config.vocab_size = None
        logger.warn("+" * 10)
        logger.warn(
            "Setting config.max_position_embeddings to {}".format(
                config.max_position_embeddings
            )
        )
        logger.warn(
            "Setting config.num_hidden_layers to {}".format(config.num_hidden_layers)
        )
        logger.warn(
            "Setting config.num_attention_heads to {}".format(
                config.num_attention_heads
            )
        )
        logger.warn("Setting config.feat_dim to {}".format(config.feat_dim))
        logger.warn("Setting config.vocab_size to {}".format(config.vocab_size))
        logger.warn("+" * 10)

        ###############   STT-2 #########
        self.roberta = RobertaModel(config, args=args)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        self.action_lm_head = ActionRecognitionHead(
            config, out_size=80, concat_feature=True, feat_dim=args.feat_dim
        )

        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head.decoder

    def forward(  ###############   STT-2 #########
        self,
        inc_scene_ids=None,
        dec_scene_ids=None,
        center_scene_ids=None,
        link_ids=None,
        token_type_ids=None,
        inc_position_ids=None,
        dec_position_ids=None,
        center_position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        outputs_embeds=None,
        spatial_codes=None,
        action_labels=None,
        long_term_labels=None,
        target_locations=None,
        secs=None,
        boxes=None,
        args=None,
    ):
        r"""
            masked_lm_labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`):
                Labels for computing the masked language modeling loss.
                Indices should be in ``[-100, 0, ..., config.vocab_size]`` (see ``input_ids`` docstring)
                Tokens with indices set to ``-100`` are ignored (masked), the loss is only computed for the tokens with labels
                in ``[0, ..., config.vocab_size]``

        Returns:
            :obj:`tuple(torch.FloatTensor)` comprising various elements depending on the configuration (:class:`~transformers.RobertaConfig`) and inputs:
            masked_lm_loss (`optional`, returned when ``masked_lm_labels`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
                Masked language modeling loss.
            prediction_scores (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, config.vocab_size)`)
                Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
            hidden_states (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_hidden_states=True``):
                Tuple of :obj:`torch.FloatTensor` (one for the output of the embeddings + one for the output of each layer)
                of shape :obj:`(batch_size, sequence_length, hidden_size)`.

                Hidden-states of the model at the output of each layer plus the initial embedding outputs.
            attentions (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_attentions=True``):
                Tuple of :obj:`torch.FloatTensor` (one for each layer) of shape
                :obj:`(batch_size, num_heads, sequence_length, sequence_length)`.

                Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
                heads.

        Examples::

            from transformers import RobertaTokenizer, RobertaForMaskedLM
            import torch

            tokenizer = RobertaTokenizer.from_pretrained('roberta-base')
            model = RobertaForMaskedLM.from_pretrained('roberta-base')
            input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute", add_special_tokens=True)).unsqueeze(0)  # Batch size 1
            outputs = model(input_ids, masked_lm_labels=input_ids)
            loss, prediction_scores = outputs[:2]

        """
        assert inc_position_ids is not None
        attention_mask = inc_position_ids != 1

        outputs = self.roberta(  ###############   STT-2 #########
            inc_scene_ids=inc_scene_ids,
            dec_scene_ids=dec_scene_ids,
            center_scene_ids=center_scene_ids,
            link_ids=link_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            inc_position_ids=inc_position_ids,
            dec_position_ids=dec_position_ids,
            center_position_ids=center_position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            spatial_codes=spatial_codes,
        )
        sequence_output = outputs[0]

        all_loss = {}
        all_outputs = {}

        ignore = ~target_locations

        prediction_scores = self.action_lm_head(
            sequence_output, outputs_embeds[:, :, :2304]
        )
        all_outputs["pred"] = prediction_scores

        masked_lm_loss = MaskedBCEWithLogitsLoss(
            prediction_scores.view(-1, self.args.num_action_classes),
            action_labels.view(-1, self.args.num_action_classes),
        )
        all_loss["action"] = masked_lm_loss
        outputs = (
            all_loss,
            all_outputs,
        )
        return outputs


class ActionRecognitionHead(nn.Module):
    """Roberta Head for masked language modeling."""

    def __init__(self, config, out_size, concat_feature, feat_dim):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.layer_norm = BertLayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.decoder = nn.Linear(config.hidden_size, out_size, bias=True)

        if concat_feature:
            self.decoder_feat = nn.Linear(feat_dim, out_size, bias=True)

        # Need a link between the two variables so that the bias is correctly resized with `resize_token_embeddings`
        self.bias = self.decoder.bias

    def forward(self, features, features_2=None, **kwargs):

        x = self.dense(features)
        x = gelu(x)
        x = self.layer_norm(x)
        x = self.decoder(x)

        if features_2 is not None:
            # project back to size of vocabulary with bias
            x = x + self.decoder_feat(features_2)

        return x
