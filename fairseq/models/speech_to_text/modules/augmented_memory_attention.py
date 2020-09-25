# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import torch
from torch import nn, Tensor
from typing import Dict, Optional
from fairseq.modules import MultiheadAttention, TransformerEncoderLayer
from fairseq.models.speech_to_text import (
    ConvTransformerEncoder,
)
from fairseq.models.speech_to_text.utils import (
    lengths_to_encoder_padding_mask
)
import torch.nn.functional as F
from functools import reduce


# ------------------------------------------------------------------------------
#   AugmentedMemoryConvTransformerEncoder
# ------------------------------------------------------------------------------
class AugmentedMemoryConvTransformerEncoder(ConvTransformerEncoder):
    def __init__(self, args):
        super().__init__(args)

        args.encoder_stride = self.stride()

        self.left_context = args.left_context // args.encoder_stride

        self.right_context = args.right_context // args.encoder_stride

        self.left_context_after_stride = (
            args.left_context // args.encoder_stride
        )
        self.right_context_after_stride = (
            args.right_context // args.encoder_stride
        )

        self.transformer_layers = nn.ModuleList([])
        self.transformer_layers.extend(
            [
                AugmentedMemoryTransformerEncoderLayer(args)
                for i in range(args.encoder_layers)
            ]
        )

    def stride(self):
        # Assuse there is only stride in conv layers
        return reduce(
            lambda x, y: x * y,
            (getattr(layer, 'stride', [1])[0] for layer in self.conv)
        )

    def forward(self, src_tokens: Tensor, src_lengths: Tensor, states=None):
        """ The input of this function is a segment of speech features,
        with left and right context
        :param torch.Tensor xs: input tensor
        :param torch.Tensor masks: input mask
        :return: position embedded tensor and mask
        :rtype Tuple[torch.Tensor, torch.Tensor]:
        """
        bsz, max_seq_len, _ = src_tokens.size()
        x = (
            src_tokens.view(bsz, max_seq_len, self.in_channels, self.input_dim)
            .transpose(1, 2)
            .contiguous()
        )
        x = self.conv(x)
        bsz, _, output_seq_len, _ = x.size()
        x = (
            x
            .transpose(1, 2)
            .transpose(0, 1)
            .contiguous()
            .view(output_seq_len, bsz, -1)
        )
        x = self.out(x)
        x = self.embed_scale * x

        subsampling_factor = 1.0 * max_seq_len / output_seq_len
        input_lengths = (
            src_lengths.float() / subsampling_factor
        ).round().long()

        encoder_padding_mask, _ = lengths_to_encoder_padding_mask(
            input_lengths, batch_first=True
        )

        # TODO: fix positional embedding
        positions = self.embed_positions(encoder_padding_mask).transpose(0, 1)

        x += positions
        x = F.dropout(x, p=self.dropout, training=self.training)

        # State to store memory banks etc.
        if states is None:
            states = [
                {
                    "memory_banks": None,
                    "encoder_states": None,
                }
                for i in range(len(self.transformer_layers))
            ]

        for i, layer in enumerate(self.transformer_layers):
            # x size:
            # (self.left_size + self.segment_size + self.right_size)
            # / self.stride, num_heads, dim
            x = layer(x, states[i], encoder_padding_mask=encoder_padding_mask)
            states[i]["encoder_states"] = x[
                self.left_context_after_stride:
                - self.right_context_after_stride
                if self.right_context_after_stride > 0 else None
            ]

        lengths = (
            ~ encoder_padding_mask[
                :,
                self.left_context_after_stride:
                - self.right_context_after_stride
                if self.right_context_after_stride > 0 else None
            ]
        ).sum(dim=1, keepdim=True).long()

        return states[-1]["encoder_states"], lengths, states


# ------------------------------------------------------------------------------
#   AugmentedMemoryTransformerEncoderLayer
# ------------------------------------------------------------------------------
class AugmentedMemoryTransformerEncoderLayer(TransformerEncoderLayer):
    def __init__(self, args):
        super().__init__(args)

        self.left_context = args.left_context // args.encoder_stride
        self.right_context = args.right_context // args.encoder_stride

    def summarize_segment(self, segment: Tensor):
        # TODO explore more options here
        return torch.mean(segment, keepdim=True, dim=0)

    def forward(
        self,
        x: Tensor,
        state: Dict,
        encoder_padding_mask: Optional[Tensor] = None
    ):

        length, batch_size, x_dim = x.size()

        residual = x

        if self.normalize_before:
            x = self.self_attn_layer_norm(x)

        # Init memory banks
        if state.get("memory_banks", None) is None:
            state["memory_banks"] = []

        seg_start = self.left_context
        seg_end = length - self.right_context

        if seg_start < seg_end:
            summarization_query = self.summarize_segment(
                x[seg_start: seg_end]
            )
        else:
            summarization_query = x.new_zeros(
                1, batch_size, x_dim
            )

        x = torch.cat(
            [x, summarization_query], dim=0
        )

        x = self.self_attn(
            input_and_summary=x,
            state=state,
            key_padding_mask=encoder_padding_mask
        )

        x = self.dropout_module(x)
        x = residual + x

        if not self.normalize_before:
            x = self.self_attn_layer_norm(x)

        residual = x
        if self.normalize_before:
            x = self.final_layer_norm(x)

        x = self.activation_fn(self.fc1(x))
        x = self.activation_dropout_module(x)
        x = self.fc2(x)
        x = self.dropout_module(x)
        x = residual + x
        if not self.normalize_before:
            x = self.final_layer_norm(x)

        return x

    def build_self_attention(self, embed_dim, args):
        return AugmentedMemoryMultiheadAttention(
            embed_dim=embed_dim,
            num_heads=args.encoder_attention_heads,
            dropout=args.attention_dropout,
            self_attention=True,
            q_noise=self.quant_noise,
            qn_block_size=self.quant_noise_block_size,
            tanh_on_mem=True,
            max_memory_size=args.max_memory_size,
        )


# ------------------------------------------------------------------------------
#   AugmentedMemoryMultiheadAttention
# ------------------------------------------------------------------------------
class AugmentedMemoryMultiheadAttention(MultiheadAttention):
    """
    Augmented Memory Attention from
    Streaming Transformer-based Acoustic Models
    Using Self-attention with Augmented Memory
    https://arxiv.org/abs/2005.08042
    """
    def __init__(
        self,
        embed_dim,
        num_heads,
        kdim=None,
        vdim=None,
        dropout=0.0,
        bias=True,
        add_bias_kv=False,
        add_zero_attn=False,
        self_attention=False,
        encoder_decoder_attention=False,
        q_noise=0.0,
        qn_block_size=8,
        tanh_on_mem=False,
        memory_dim=None,
        std_scale=0.5,  # 0.5 based on https://arxiv.org/abs/2005.09137
        max_memory_size=-1,
        disable_mem_on_mem_attn=True,
    ):
        super().__init__(
            embed_dim,
            num_heads,
            kdim,
            vdim,
            dropout,
            bias,
            add_bias_kv,
            add_zero_attn,
            self_attention,
            encoder_decoder_attention,
            q_noise,
            qn_block_size,
        )

        self.memory_dim = memory_dim if memory_dim is not None else embed_dim
        self.std_scale = std_scale
        self.disable_mem_on_mem_attn = disable_mem_on_mem_attn

        # This Operator was used for factorization in PySpeech
        self.v2e = lambda x: x

        if tanh_on_mem:
            self.squash_mem = torch.tanh
            self.nonlinear_squash_mem = True
        else:
            self.squash_mem = lambda x: x
            self.nonlinear_squash_mem = False

        self.max_memory_size = max_memory_size

    def forward(self, input_and_summary: Tensor, state: Dict, key_padding_mask: Tensor):
        """
        input_and_summary:
            seg_length + 1, batch_size, dim
            Encoder states of current segment with left or right context,
            plus one summarization query

        state:
            Dictionary contains information about memory banks

        key_padding_mask:
            Mask on paddings

        """

        length, batch_size, _ = input_and_summary.shape
        seg_length = length - 1  # not include sum_query, last index

        # Memory banks, a list
        memory_banks = state["memory_banks"]

        if (
            self.max_memory_size > -1
            and len(memory_banks) > self.max_memory_size
        ):
            if self.max_memory_size == 0:
                memory_banks = []
            else:
                memory_banks = memory_banks[-self.max_memory_size:]

        memory_banks_and_input = torch.cat(
            memory_banks + [input_and_summary[:-1]], dim=0
        )

        q = self.q_proj(self.v2e(input_and_summary))
        k = self.k_proj(self.v2e(memory_banks_and_input))
        v = self.v_proj(self.v2e(memory_banks_and_input))

        q = (
            q.contiguous()
            .view(-1, batch_size * self.num_heads, self.head_dim)
            .transpose(0, 1) * self.scaling
        )
        k = (
            k.contiguous()
            .view(-1, batch_size * self.num_heads, self.head_dim)
            .transpose(0, 1)
        )

        v = (
            v.contiguous()
            .view(-1, batch_size * self.num_heads, self.head_dim)
            .transpose(0, 1)
        )

        attention_weights = torch.bmm(q, k.transpose(1, 2))

        if self.disable_mem_on_mem_attn:
            # Don't let summary attend on previous history
            attention_weights[:, -1, :len(memory_banks)] = float('-inf')

        if self.std_scale is not None:
            attention_weights = attention_suppression(
                attention_weights, self.std_scale)

        assert list(attention_weights.shape) == [
            batch_size * self.num_heads,
            seg_length + 1,
            seg_length + len(memory_banks)
        ]

        if key_padding_mask is not None and key_padding_mask.any():
            # Add zeros at beginning for memory banks
            key_padding_mask = torch.cat(
                [
                    key_padding_mask.new_zeros(batch_size, len(memory_banks)),
                    key_padding_mask,
                ],
                dim=1
            )
            _, tgt_len, src_len = attention_weights.size()
            attention_weights = attention_weights.view(
                batch_size, self.num_heads, tgt_len, src_len
            )
            attention_weights = attention_weights.masked_fill(
                key_padding_mask.unsqueeze(1).unsqueeze(2).to(torch.bool),
                float("-inf")
            )
            attention_weights = attention_weights.view(
                batch_size * self.num_heads,
                tgt_len, src_len
            )

        attention_weights = torch.nn.functional.softmax(
            attention_weights.float(), dim=-1
        ).type_as(attention_weights)

        attention_probs = self.dropout_module(attention_weights)

        # Key padding mask can be all true, which usually happens at the
        # end of a short sequence in a batch.
        # It results nan in attention weights.
        # Replacing the nan with zero should solve the problem.
        attention_probs_nan_mask = torch.isnan(attention_probs)
        if attention_probs_nan_mask.any():
            attention_probs = attention_probs.masked_fill(
                attention_probs_nan_mask, 0
            )

        # [T, T, B, n_head] + [T, B, n_head, d_head] -> [T, B, n_head, d_head]
        attention = torch.bmm(attention_probs, v)

        assert list(attention.shape) == [
            batch_size * self.num_heads,
            seg_length + 1,
            self.head_dim
        ]

        attention = (
            attention
            .transpose(0, 1)
            .contiguous()
            .view(seg_length + 1, batch_size, self.embed_dim)
        )

        output_and_memory = self.out_proj(attention)

        next_m = output_and_memory[-1:]
        next_m = self.squash_mem(next_m)
        output = output_and_memory[:-1]

        state["memory_banks"].append(next_m)

        return output


# ------------------------------------------------------------------------------
#   attention suppression
# ------------------------------------------------------------------------------
def attention_suppression(attention_weights: Tensor, scale: float):
    # B, H, qlen, klen -> B, H, qlen, 1
    attention_prob = torch.nn.functional.softmax(
        attention_weights.float(), dim=-1)
    attention_nozeros = attention_prob.to(torch.bool)
    nozeros_sum = torch.sum(
        attention_nozeros.to(torch.float),
        dim=-1,
        keepdim=True
    )

    # For very sparse situation, we need get round about 0s
    key_sum = torch.sum(attention_prob, dim=-1, keepdim=True)

    # nozeros_sum should > 1
    key_mean = key_sum / (nozeros_sum + 1e-8)

    # std calculation
    dis = (attention_prob - key_mean) * (attention_prob - key_mean)

    # if attention_prob[i] < threshold, then dis_masked[i] = 0; for all i
    dis_masked = torch.where(
        attention_nozeros,
        dis,
        attention_prob.new_zeros(attention_prob.size())
    )

    key_var = torch.sum(dis_masked, dim=-1, keepdim=True)
    key_var = key_var / (nozeros_sum - 1.0 + 1e-8)
    key_std = torch.sqrt(key_var)
    key_thread = key_mean - scale * key_std

    # if attention_prob[i] >= key_thread, then attention_prob[i]
    # , otherwise "-inf"
    inf_tensor = attention_prob.new_zeros(attention_prob.size()).detach()
    inf_tensor[:] = float('-inf')
    attention_weights_float = torch.where(
        attention_prob < key_thread,
        inf_tensor,
        attention_weights.float(),
    )

    return attention_weights_float.type_as(attention_weights)
