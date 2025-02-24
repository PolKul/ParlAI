#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


"""
Generic PyTorch-based Generator agent.

Implements quite a bit of boilerplate, including forced-decoding loss and a tree search.

Contains the following utilities:

* `ref:TorchGeneratorAgent` class, which serves as a useful parent for generative torch
  agents.
* Beam class which provides some generic beam functionality for classes to use
"""

from parlai.core.params import ParlaiParser
from abc import ABC, abstractmethod
from typing import TypeVar, List, Dict, Optional, Tuple, Set, Iterable
import math
from operator import attrgetter

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor

from parlai.core.opt import Opt
from parlai.core.search import LexicallyConstrainedBeamSearch
from parlai.core.token_generation_constraints import pack_constraints
from parlai.utils.distributed import is_distributed, sync_parameters
from parlai.core.torch_agent import TorchAgent, Batch, Output, DictionaryAgent
from parlai.utils.misc import warn_once
from parlai.utils.io import PathManager
import parlai.utils.logging as logging
from parlai.core.metrics import SumMetric, AverageMetric, FairseqBleuMetric
from parlai.utils.fp16 import FP16SafeCrossEntropy
from parlai.utils.torch import (
    neginf,
    total_parameters,
    trainable_parameters,
    PipelineHelper,
)


class SearchBlocklist(object):
    """
    Search block list facilitates blocking ngrams from being generated.
    """

    def __init__(self, dict_agent: DictionaryAgent) -> None:
        self.dict = dict_agent
        self._phrases: Set[str] = set()
        self._phrase_ngrams: Dict[int, List[List[int]]] = {}

    def __bool__(self):
        return bool(self._phrases)

    def clear(self) -> None:
        self._phrases = set()
        self._phrase_ngrams = {}

    def _add_literal(self, phrase_literal: str):
        if phrase_literal in self._phrases:
            return
        ngram = self.dict.txt2vec(phrase_literal)
        self._phrases.add(phrase_literal)
        logging.debug(f"Adding '{phrase_literal}' to the beam block_list {ngram}")
        l = len(ngram)
        if l not in self._phrase_ngrams:
            self._phrase_ngrams[l] = []
        self._phrase_ngrams[l].append(ngram)

    def add(self, phrase: str):
        phrase = phrase.strip()
        if not phrase:
            return
        self._add_literal(phrase)
        self._add_literal(phrase + "s")
        self._add_literal(phrase.lower())
        self._add_literal(phrase.lower() + "s")
        self._add_literal(phrase.upper())
        self._add_literal(phrase.upper() + "S")
        self._add_literal(phrase.title())
        self._add_literal(phrase.title() + "S")
        self._add_literal(phrase[0].upper() + phrase[1:])
        self._add_literal(phrase[0].upper() + phrase[1:] + "s")
        self._add_literal(phrase[0].upper() + phrase[1:].lower())
        self._add_literal(phrase[0].upper() + phrase[1:].lower() + "s")

    def items(self) -> Iterable[Tuple[int, List[List[int]]]]:
        return self._phrase_ngrams.items()


TSType = TypeVar('TSType', bound='TreeSearch')


class TorchGeneratorModel(nn.Module, ABC):
    """
    Abstract TorchGeneratorModel.

    This interface expects you to implement model with the following reqs:

    :attribute model.encoder:
        takes input returns tuple (enc_out, enc_hidden, attn_mask)

    :attribute model.decoder:
        takes decoder params and returns decoder outputs after attn

    :attribute model.output:
        takes decoder outputs and returns distr over dictionary
    """

    def __init__(
        self,
        padding_idx=0,
        start_idx=1,
        end_idx=2,
        unknown_idx=3,
        input_dropout=0,
        longest_label=1,
    ):
        super().__init__()
        self.NULL_IDX = padding_idx
        self.END_IDX = end_idx
        self.START_IDX = start_idx
        self.register_buffer('START', torch.LongTensor([start_idx]))
        self.longest_label = longest_label

    def _get_initial_forced_decoder_input(self, bsz: int, inputs: torch.LongTensor):
        """
        Return initial input to the decoder.

        :param bsz:
            batchsize
        :param inputs:
            inputs to decode

        :return initial_input:
            initial input for the decoder.
        """
        return torch.cat([self.START.detach().expand(bsz, 1), inputs], 1)

    def decode_forced(self, encoder_states, ys):
        """
        Decode with a fixed, true sequence, computing loss.

        Useful for training, or ranking fixed candidates.

        :param ys:
            the prediction targets. Contains both the start and end tokens.

        :type ys:
            LongTensor[bsz, time]

        :param encoder_states:
            Output of the encoder. Model specific types.

        :type encoder_states:
            model specific

        :return:
            pair (logits, choices) containing the logits and MLE predictions

        :rtype:
            (FloatTensor[bsz, ys, vocab], LongTensor[bsz, ys])
        """
        bsz = ys.size(0)
        seqlen = ys.size(1)
        inputs = ys.narrow(1, 0, seqlen - 1)
        if (ys[:, 0] == self.START_IDX).any():
            raise AssertionError(
                "The Beginning of Sentence token is automatically added to the "
                "label in decode_forced, but you included it in the label. This means "
                "your model will have a double BOS token, which is probably not what "
                "you intended."
            )
        inputs = self._get_initial_forced_decoder_input(bsz, inputs)
        latent, _ = self.decoder(inputs, encoder_states)
        logits = self.output(latent)
        _, preds = logits.max(dim=2)
        return logits, preds

    @abstractmethod
    def reorder_encoder_states(self, encoder_states, indices):
        """
        Reorder encoder states according to a new set of indices.

        This is an abstract method, and *must* be implemented by the user.

        Its purpose is to provide beam search with a model-agnostic interface for
        beam search. For example, this method is used to sort hypotheses,
        expand beams, etc.

        For example, assume that encoder_states is an bsz x 1 tensor of values

        .. code-block:: python

            indices = [0, 2, 2]
            encoder_states = [[0.1]
                              [0.2]
                              [0.3]]

        then the output will be

        .. code-block:: python

            output = [[0.1]
                      [0.3]
                      [0.3]]

        :param encoder_states:
            output from encoder. type is model specific.

        :type encoder_states:
            model specific

        :param indices:
            the indices to select over. The user must support non-tensor
            inputs.

        :type indices: list[int]

        :return:
            The re-ordered encoder states. It should be of the same type as
            encoder states, and it must be a valid input to the decoder.

        :rtype:
            model specific
        """
        pass

    @abstractmethod
    def reorder_decoder_incremental_state(self, incremental_state, inds):
        """
        Reorder incremental state for the decoder.

        Used to expand selected beams in beam search. Unlike reorder_encoder_states,
        implementing this method is optional. However, without incremental decoding,
        decoding a single beam becomes O(n^2) instead of O(n), which can make
        beam search impractically slow.

        In order to fall back to non-incremental decoding, just return None from this
        method.

        :param incremental_state:
            second output of model.decoder
        :type incremental_state:
            model specific
        :param inds:
            indices to select and reorder over.
        :type inds:
            LongTensor[n]

        :return:
            The re-ordered decoder incremental states. It should be the same
            type as incremental_state, and usable as an input to the decoder.
            This method should return None if the model does not support
            incremental decoding.

        :rtype:
            model specific
        """
        pass

    def forward(self, *xs, ys=None, prev_enc=None, maxlen=None, bsz=None):
        """
        Get output predictions from the model.

        :param xs:
            input to the encoder
        :type xs:
            LongTensor[bsz, seqlen]
        :param ys:
            Expected output from the decoder. Used
            for teacher forcing to calculate loss.
        :type ys:
            LongTensor[bsz, outlen]
        :param prev_enc:
            if you know you'll pass in the same xs multiple times, you can pass
            in the encoder output from the last forward pass to skip
            recalcuating the same encoder output.
        :param maxlen:
            max number of tokens to decode. if not set, will use the length of
            the longest label this model has seen. ignored when ys is not None.
        :param bsz:
            if ys is not provided, then you must specify the bsz for greedy
            decoding.

        :return:
            (scores, candidate_scores, encoder_states) tuple

            - scores contains the model's predicted token scores.
              (FloatTensor[bsz, seqlen, num_features])
            - candidate_scores are the score the model assigned to each candidate.
              (FloatTensor[bsz, num_cands])
            - encoder_states are the output of model.encoder. Model specific types.
              Feed this back in to skip encoding on the next call.
        """
        assert ys is not None, "Greedy decoding in TGModel.forward no longer supported."
        # TODO: get rid of longest_label
        # keep track of longest label we've ever seen
        # we'll never produce longer ones than that during prediction
        self.longest_label = max(self.longest_label, ys.size(1))

        # use cached encoding if available
        encoder_states = prev_enc if prev_enc is not None else self.encoder(*xs)

        # use teacher forcing
        scores, preds = self.decode_forced(encoder_states, ys)
        return scores, preds, encoder_states


class PPLMetric(AverageMetric):
    def value(self):
        return math.exp(super().value())


class TorchGeneratorAgent(TorchAgent, ABC):
    """
    Abstract Generator agent; only meant to be extended.

    TorchGeneratorAgent aims to handle much of the bookkeeping and infrastructure work
    for any generative models, like seq2seq or transformer. It implements the train_step
    and eval_step. The only requirement is that your model *must* implemented the
    interface TorchGeneratorModel interface.
    """

    @classmethod
    def upgrade_opt(cls, opt_from_disk: Opt):
        # call the parent upgrades
        opt_from_disk = super(TorchGeneratorAgent, cls).upgrade_opt(opt_from_disk)

        # 2019-08-18: Adding support for generation other than beam search
        # Previously, selecting --beam-size > 1 enabled beam search and == 1 was
        # greedy. New behavior is --inference greedy or --inference beam.
        if 'inference' not in opt_from_disk:
            assert 'beam_size' in opt_from_disk
            if opt_from_disk['beam_size'] == 1:
                method = 'greedy'
            else:
                method = 'beam'
            opt_from_disk['inference'] = method
            warn_once(f'Old model inference method inferred as {method}')

        # 2020-06-03: Changing "blacklist" --> "blocklist"
        if 'beam_blacklist_filename' in opt_from_disk:
            if opt_from_disk['beam_blacklist_filename'] is not None:
                opt_from_disk['beam_block_list_filename'] = opt_from_disk[
                    'beam_blacklist_filename'
                ]
            del opt_from_disk['beam_blacklist_filename']

        # 2020-08-04: Introduce full context beam blocking
        # Previous, specifying --beam-context-block-ngram > 1 would block
        # from generating ngrams from model's context, which is limited
        # by truncation parameters. Now, we block on full dialogue history.
        if 'beam_block_full_context' not in opt_from_disk:
            warn_once('Loading model with `--beam-block-full-context false`')
            opt_from_disk['beam_block_full_context'] = False

        return opt_from_disk

    @classmethod
    def add_cmdline_args(
        cls, parser: ParlaiParser, partial_opt: Optional[Opt] = None
    ) -> ParlaiParser:
        """
        Add command line arguments.
        """
        agent = parser.add_argument_group('Torch Generator Agent')
        agent.add_argument(
            '--beam-size',
            type=int,
            default=1,
            help='Beam size, if 1 then greedy search',
        )
        agent.add_argument(
            '--beam-min-length',
            type=int,
            default=1,
            help='Minimum length of prediction to be generated by the beam search',
        )
        agent.add_argument(
            '--beam-context-block-ngram',
            type=int,
            default=-1,
            help=(
                'Size n-grams to block in beam search from the context. val <= 0 '
                'implies no blocking'
            ),
        )
        agent.add_argument(
            '--beam-block-ngram',
            type=int,
            default=-1,
            help='Size n-grams to block in beam search. val <= 0 implies no blocking',
        )
        agent.add_argument(
            '--beam-block-full-context',
            type='bool',
            default=True,
            help='Block n-grams from the *full* history context. Specify False to block '
            'up to m tokens in the past, where m is truncation parameter for agent',
        )
        agent.add_argument(
            '--beam-length-penalty',
            type=float,
            default=0.65,
            help='Applies a length penalty. Set to 0 for no penalty.',
        )
        agent.add_argument(
            '--skip-generation',
            type='bool',
            default=False,
            hidden=True,
            help='Skip beam search. Useful for speeding up training, '
            'if perplexity is the validation metric.',
        )
        agent.add_argument(
            '--inference',
            choices={'beam', 'greedy', 'topk', 'nucleus', 'delayedbeam', 'constrainedbeam'},
            default='greedy',
            help='Generation algorithm',
        )
        parser.add_argument(
            '--constraints',
            type=str,
            nargs='+',
            default=None,
            help='List of constraints for the constrained beam search',
        )
        agent.add_argument(
            '--topk', type=int, default=10, help='K used in Top K sampling'
        )
        agent.add_argument(
            '--topp', type=float, default=0.9, help='p used in nucleus sampling'
        )
        agent.add_argument(
            '--beam-delay', type=int, default=30, help='used in delayedbeam search'
        )
        agent.add_argument(
            '--beam-block-list-filename',
            type=str,
            default=None,
            help='Load a text file of hard blocks for beam search to never say.',
        )
        agent.add_argument(
            '--temperature',
            type=float,
            default=1.0,
            help='temperature to add during decoding',
        )
        agent.add_argument(
            '--compute-tokenized-bleu',
            type='bool',
            default=False,
            help='if true, compute tokenized bleu scores',
        )

        super().add_cmdline_args(parser, partial_opt=partial_opt)
        return agent

    def __init__(self, opt: Opt, shared=None):
        init_model, is_finetune = self._get_init_model(opt, shared)
        super().__init__(opt, shared)

        self.beam_size = opt.get('beam_size', 1)
        self.beam_min_length = opt.get('beam_min_length', 1)
        self.beam_block_ngram = opt.get('beam_block_ngram', -1)
        self.beam_context_block_ngram = opt.get('beam_context_block_ngram', -1)
        self.beam_block_full_context = opt.get('beam_block_full_context', False)
        self.temperature = opt.get('temperature', 1.0)
        assert self.temperature > 0, '--temperature must be greater than 0'
        self.output_token_losses = opt.get(
            'verbose', False
        ) or 'token_losses' in opt.get('display_add_fields', '')
        self.compute_tokenized_bleu = opt.get('compute_tokenized_bleu', False)
        self.beam_block_list: Optional[SearchBlocklist] = None
        self.constraints = opt.get('constraints', None)

        if shared:
            # set up shared properties
            states = shared.get('states', {})
            self.beam_block_list = shared.get('beam_block_list')
        else:
            # this is not a shared instance of this class, so do full init
            self.criterion = self.build_criterion()
            # ensure all distributed copies will always be in sync
            self.model = self.build_model()

            # load the block_list for beam search
            self.beam_block_list = self._load_beam_block_list()

            if self.model is None or self.criterion is None:
                raise AttributeError(
                    'build_model() and build_criterion() need to return the model or criterion'
                )
            if self.use_cuda:
                if self.model_parallel:
                    ph = PipelineHelper()
                    ph.check_compatibility(self.opt)
                    self.model = ph.make_parallel(self.model)
                else:
                    self.model.cuda()
                self.criterion.cuda()

            sync_parameters(self.model)
            train_params = trainable_parameters(self.model)
            total_params = total_parameters(self.model)
            logging.info(
                f"Total parameters: {total_params:,d} ({train_params:,d} trainable)"
            )

            if self.fp16:
                self.model = self.model.half()

            if init_model is not None:
                # load model parameters if available
                logging.info(f'Loading existing model params from {init_model}')
                states = self.load(init_model)
            else:
                states = {}

        if shared is not None:
            if 'optimizer' in shared:
                self.optimizer = shared['optimizer']
        elif self._should_initialize_optimizer():
            # do this regardless of share state, but don't
            was_reset = self.init_optim(
                [p for p in self.model.parameters() if p.requires_grad],
                optim_states=states.get('optimizer'),
                saved_optim_type=states.get('optimizer_type'),
            )
            if was_reset and not is_finetune:
                logging.warning("Optimizer was reset. Also resetting LR scheduler.")
            self.build_lr_scheduler(states, hard_reset=is_finetune or was_reset)

        if shared is None and is_distributed():
            device_ids = None if self.model_parallel else [self.opt['gpu']]
            self.model = torch.nn.parallel.DistributedDataParallel(
                self.model, device_ids=device_ids, broadcast_buffers=False
            )

        self.reset()

    def build_criterion(self):
        """
        Construct and return the loss function.

        By default torch.nn.CrossEntropyLoss.

        If overridden, this model should produce a sum that can be used for a per-token loss.
        """
        if not self.fp16:
            return torch.nn.CrossEntropyLoss(
                ignore_index=self.NULL_IDX, reduction='none'
            )
        else:
            # FP16 safe cross entropy (softmax done in FP32)
            return FP16SafeCrossEntropy(ignore_index=self.NULL_IDX, reduction='none')

    def _v2t(self, vec):
        """
        Convert token indices to string of tokens.
        """
        new_vec = []
        if hasattr(vec, 'cpu'):
            vec = vec.cpu()
        for i in vec:
            if i == self.END_IDX:
                break
            elif i != self.START_IDX:
                new_vec.append(i)
        return self.dict.vec2txt(new_vec)

    def set_interactive_mode(self, mode, shared=False):
        """
        Turn on interactive mode.
        """
        super().set_interactive_mode(mode, shared)
        if mode:
            self.skip_generation = False
        else:
            self.skip_generation = self.opt.get('skip_generation', False)

    def _dummy_batch(self, batchsize, maxlen):
        """
        Create a dummy batch.

        This is used to preinitialize the cuda buffer, or otherwise force a
        null backward pass after an OOM.

        If your model uses additional inputs beyond text_vec and label_vec,
        you will need to override it to add additional fields.
        """
        text_vec = (
            torch.arange(1, maxlen + 1)  # need it as long as specified
            .clamp(max=3)  # cap at 3 for testing with tiny dictionaries
            .unsqueeze(0)
            .expand(batchsize, maxlen)
            .cuda()
        )
        # label vec has two tokens to make it interesting, but we we can't use the
        # start token, it's reserved.
        label_vec = (
            torch.LongTensor([self.END_IDX, self.NULL_IDX])
            .unsqueeze(0)
            .expand(batchsize, 2)
            .cuda()
        )
        return Batch(
            text_vec=text_vec, label_vec=label_vec, text_lengths=[maxlen] * batchsize
        )

    def _init_cuda_buffer(self, batchsize, maxlen, force=False):
        """
        Pre-initialize CUDA buffer by doing fake forward pass.

        This is also used in distributed mode to force a worker to sync with others.
        """
        if self.use_cuda and (force or not hasattr(self, 'buffer_initialized')):
            try:
                self._control_local_metrics(disabled=True)
                loss = 0 * self.compute_loss(self._dummy_batch(batchsize, maxlen))
                self._control_local_metrics(enabled=True)
                self._temporarily_disable_local_metrics = False
                self.backward(loss)
                self.buffer_initialized = True
            except RuntimeError as e:
                if 'out of memory' in str(e):
                    m = (
                        'CUDA OOM: Lower batch size (-bs) from {} or lower '
                        ' max sequence length (-tr) from {}'
                        ''.format(batchsize, maxlen)
                    )
                    raise RuntimeError(m)
                else:
                    raise e

    def reset_metrics(self):
        """
        Reset metrics for reporting loss and perplexity.
        """
        super().reset_metrics()

    def share(self):
        """
        Share internal states between parent and child instances.
        """
        shared = super().share()
        shared['beam_block_list'] = self.beam_block_list
        if hasattr(self, 'optimizer'):
            shared['optimizer'] = self.optimizer
        return shared

    def vectorize(self, *args, **kwargs):
        """
        Override vectorize for generative models.
        """
        kwargs['add_start'] = False  # model does this in module code
        kwargs['add_end'] = True  # we do want this
        return super().vectorize(*args, **kwargs)

    def batchify(self, obs_batch, sort=True):
        batch = super().batchify(obs_batch, sort=sort)
        if (
            self.beam_block_full_context
            and obs_batch
            and any('full_text_vec' in o for o in obs_batch)
        ):
            batch['full_text_vec'], _ = self._pad_tensor(
                [obs_batch[i].get('full_text_vec', []) for i in batch.valid_indices]
            )
        if self.constraints is not None:
            batch['constraints'] = pack_constraints([[torch.LongTensor(self.history.parse(c)) for c in self.constraints]])

        return batch

    def _model_input(self, batch):
        """
        Create the input (x) value for the model.

        Must return a tuple.  This will be passed directly into the model via
        `*args`, i.e.,

        >>> model(*_model_input(batch))

        This is intentionally overridable so that richer models can pass the
        additional inputs.
        """
        return (batch.text_vec,)

    def _encoder_input(self, batch):
        """
        Create the input (x) value for the encoder.

        Must return a tuple.  This will be passed directly into the encoder via
        `*args`, i.e.,

        >>> model.encoder(*_encoder_input(batch))

        This is intentionally overridable so that richer models can pass the
        additional inputs directly to the encoder.
        """
        return self._model_input(batch)

    def compute_loss(self, batch, return_output=False):
        """
        Compute and return the loss for the given batch.

        Easily overridable for customized loss functions.

        If return_output is True, the full output from the call to self.model()
        is also returned, via a (loss, model_output) pair.
        """
        if batch.label_vec is None:
            raise ValueError('Cannot compute loss without a label.')
        model_output = self.model(*self._model_input(batch), ys=batch.label_vec)
        scores, preds, *_ = model_output
        score_view = scores.reshape(-1, scores.size(-1))
        loss = self.criterion(score_view, batch.label_vec.view(-1))
        loss = loss.view(scores.shape[:-1]).sum(dim=1)
        # save loss to metrics
        notnull = batch.label_vec.ne(self.NULL_IDX)
        target_tokens = notnull.long().sum(dim=-1)
        correct = ((batch.label_vec == preds) * notnull).sum(dim=-1)

        # cross entropy loss
        self.record_local_metric('loss', AverageMetric.many(loss, target_tokens))
        # perplexity
        self.record_local_metric('ppl', PPLMetric.many(loss, target_tokens))
        # token-wise accuracy
        self.record_local_metric(
            'token_acc', AverageMetric.many(correct, target_tokens)
        )
        # utterance-wise exact match
        self.record_local_metric(
            'token_em', AverageMetric.many(correct == target_tokens)
        )
        # actually do backwards loss
        loss = loss.sum()
        loss /= target_tokens.sum()  # average loss per token
        if return_output:
            return (loss, model_output)
        else:
            return loss

    def train_step(self, batch):
        """
        Train on a single batch of examples.
        """
        # helps with memory usage
        # note we want to use the opt's batchsize instead of the observed batch size
        # in case dynamic batching is in use
        self._init_cuda_buffer(self.opt['batchsize'], self.label_truncate or 256)
        self.model.train()
        self.zero_grad()

        try:
            loss = self.compute_loss(batch)
            self.backward(loss)
            self.update_params()
            oom_sync = False
        except RuntimeError as e:
            # catch out of memory exceptions during fwd/bck (skip batch)
            if 'out of memory' in str(e):
                oom_sync = True
                logging.error(
                    'Ran out of memory, skipping batch. '
                    'if this happens frequently, decrease batchsize or '
                    'truncate the inputs to the model.'
                )
                self.global_metrics.add('skipped_batches', SumMetric(1))
            else:
                raise e

        if oom_sync:
            # moved outside of the try-except because the raised exception in scope
            # actually prevents from the data being freed, which can sometimes cause
            # us to OOM during our OOM handling.
            # https://github.com/pytorch/pytorch/issues/18853#issuecomment-583779161

            # gradients are synced on backward, now this model is going to be
            # out of sync! catch up with the other workers
            self._init_cuda_buffer(8, 8, True)

    def _construct_token_losses(self, labels, model_output):
        # Get non-aggregated losses
        scores, _, _ = model_output
        score_view = scores.reshape(-1, scores.size(-1))
        losses = self.criterion(score_view, labels.view(-1)).view(len(labels), -1)

        # Zip decoded tokens with losses
        token_losses = []
        for i, label in enumerate(labels):
            token_losses.append(
                list(
                    zip(
                        [self.dict[token] for token in label.tolist()],
                        losses[i].tolist(),
                    )
                )
            )
        return token_losses

    def _compute_fairseq_bleu(self, batch: Batch, preds):
        """
        Compute BLEU score between text and label, using the FAIRSeq BLEU Scorer.

        :param batch:
            Batch of observations
        :param texts:
            list of string predictions
        """
        all_results = []
        label_vec = batch.label_vec
        assert label_vec is not None, "label_vec must exist for fairseq bleu"
        for i, t in enumerate(preds):
            result = FairseqBleuMetric.compute_many(
                t,
                label_vec[i].unsqueeze(0),
                pad_idx=self.NULL_IDX,
                end_idx=self.END_IDX,
                unk_idx=self.dict[self.dict.unk_token],
            )
            if result is None:
                return
            all_results.append(result)

        bleu_scores = list(zip(*all_results))
        for k in range(4):
            self.record_local_metric(f'fairseq_bleu{k + 1}', bleu_scores[k])

    def _add_generation_metrics(self, batch, preds):
        """
        Can be overridden to allow for some metrics on the generations calculated at
        eval.
        """
        pass

    def rank_eval_label_candidates(self, batch, batchsize):
        """
        Rank label_candidates during eval_step.

        Can be overridden to allow for different ways of ranking candidates. Must have
        `--rank-candidates` set to True. By default, we roughly compute PPL to rank the
        candidates.
        """
        # compute roughly ppl to rank candidates
        cand_choices = []
        cand_choices_scores = []
        encoder_states = self.model.encoder(*self._encoder_input(batch))
        for i in range(batchsize):
            num_cands = len(batch.candidate_vecs[i])
            enc = self.model.reorder_encoder_states(encoder_states, [i] * num_cands)
            cands, _ = self._pad_tensor(batch.candidate_vecs[i])
            cands = cands.to(batch.text_vec.device)
            scores, _ = self.model.decode_forced(enc, cands)
            score_view = scores.reshape(num_cands * cands.size(1), -1)
            cand_losses = F.cross_entropy(
                score_view, cands.view(-1), reduction='none'
            ).view(num_cands, cands.size(1))
            # now cand_losses is cands x seqlen size, but we still need to
            # check padding and such
            mask = (cands != self.NULL_IDX).float()
            cand_scores = (cand_losses * mask).sum(dim=1) / (mask.sum(dim=1) + 1e-9)
            sorted_scores, ordering = cand_scores.sort()
            cand_choices.append([batch.candidates[i][o] for o in ordering])
            cand_choices_scores.append(sorted_scores.tolist())

        return cand_choices, cand_choices_scores

    def eval_step(self, batch):
        """
        Evaluate a single batch of examples.
        """
        if batch.text_vec is None and batch.image is None:
            return
        if batch.text_vec is not None:
            bsz = batch.text_vec.size(0)
        else:
            bsz = len(batch.image)
        self.model.eval()
        cand_scores = None
        token_losses = None

        if batch.label_vec is not None:
            # calculate loss on targets with teacher forcing
            loss, model_output = self.compute_loss(batch, return_output=True)
            if self.output_token_losses:
                token_losses = self._construct_token_losses(
                    batch.label_vec, model_output
                )

        preds = None
        if self.skip_generation:
            warn_once("--skip-generation true produces limited metrics")
        else:
            maxlen = self.label_truncate or 256
            beam_preds_scores, beams = self._generate(batch, self.beam_size, maxlen)
            preds, scores = zip(*beam_preds_scores)
            self._add_generation_metrics(batch, preds)

            # bsz x beamsize
            beam_texts: List[List[Tuple[str, float]]] = []
            for beam in beams:
                beam_texts.append([])
                for tokens, score in beam.get_rescored_finished():
                    try:
                        beam_texts[-1].append((self._v2t(tokens), score.item()))
                    except KeyError:
                        logging.error("Decoding error: %s", tokens)
                        continue

        cand_choices = None
        cand_scores = None
        if self.rank_candidates:
            cand_choices, cand_scores = self.rank_eval_label_candidates(batch, bsz)

        text = [self._v2t(p) for p in preds] if preds is not None else None
        if text and self.compute_tokenized_bleu:
            # compute additional bleu scores
            self._compute_fairseq_bleu(batch, preds)
        retval = Output(
            text, cand_choices, token_losses=token_losses, cand_scores=cand_scores
        )
        if not self.skip_generation:
            retval.beam_texts = beam_texts
        return retval

    def _treesearch_factory(self, device):
        method = self.opt.get('inference', 'greedy')
        beam_size = self.opt.get('beam_size', 1)
        if method == 'greedy':
            return GreedySearch(
                beam_size,
                min_length=0,
                block_ngram=self.beam_block_ngram,
                context_block_ngram=self.beam_context_block_ngram,
                length_penalty=self.opt.get('beam_length_penalty', 0.65),
                padding_token=self.NULL_IDX,
                bos_token=self.START_IDX,
                eos_token=self.END_IDX,
                device=device,
            )
        elif method == 'beam':
            return BeamSearch(
                beam_size,
                min_length=self.beam_min_length,
                block_ngram=self.beam_block_ngram,
                context_block_ngram=self.beam_context_block_ngram,
                length_penalty=self.opt.get('beam_length_penalty', 0.65),
                padding_token=self.NULL_IDX,
                bos_token=self.START_IDX,
                eos_token=self.END_IDX,
                device=device,
            )
        elif method == 'delayedbeam':
            return DelayedBeamSearch(
                self.opt['topk'],
                self.opt['beam_delay'],
                beam_size,
                min_length=self.beam_min_length,
                block_ngram=self.beam_block_ngram,
                context_block_ngram=self.beam_context_block_ngram,
                length_penalty=self.opt.get('beam_length_penalty', 0.65),
                padding_token=self.NULL_IDX,
                bos_token=self.START_IDX,
                eos_token=self.END_IDX,
                device=device,
            )
        elif method == 'constrainedbeam':
            return ConstrainedBeamSearch(
                self.dict,
                "ordered",
                beam_size,
                min_length=self.beam_min_length,
                block_ngram=self.beam_block_ngram,
                context_block_ngram=self.beam_context_block_ngram,
                length_penalty=self.opt.get('beam_length_penalty', 0.65),
                padding_token=self.NULL_IDX,
                bos_token=self.START_IDX,
                eos_token=self.END_IDX,
                device=device,
            )
        elif method == 'topk':
            return TopKSampling(
                self.opt['topk'],
                beam_size,
                min_length=self.beam_min_length,
                block_ngram=self.beam_block_ngram,
                context_block_ngram=self.beam_context_block_ngram,
                length_penalty=self.opt.get('beam_length_penalty', 0.65),
                padding_token=self.NULL_IDX,
                bos_token=self.START_IDX,
                eos_token=self.END_IDX,
                device=device,
            )
        elif method == 'nucleus':
            return NucleusSampling(
                self.opt['topp'],
                beam_size,
                min_length=self.beam_min_length,
                block_ngram=self.beam_block_ngram,
                context_block_ngram=self.beam_context_block_ngram,
                length_penalty=self.opt.get('beam_length_penalty', 0.65),
                padding_token=self.NULL_IDX,
                bos_token=self.START_IDX,
                eos_token=self.END_IDX,
                device=device,
            )

        else:
            raise ValueError(f"Can't use inference method {method}")

    def _get_context(self, batch, batch_idx):
        """
        Set the beam context for n-gram context blocking.

        Intentionally overridable for more complex model histories.
        """
        ctxt = batch.text_vec[batch_idx]
        if self.beam_block_full_context:
            ctxt = batch.full_text_vec[batch_idx]
        return ctxt

    def _get_initial_decoder_input(
        self, bsz: int, beam_size: int, dev: torch.device
    ) -> torch.LongTensor:
        """
        Return initial input to the decoder.

        :param bsz:
            batchsize
        :param beam_size:
            beam size
        :param dev:
            device to send input to.

        :return initial_input:
            initial input for the decoder
        """
        return (
            torch.LongTensor([self.START_IDX])  # type: ignore
            .expand(bsz * beam_size, 1)
            .to(dev)
        )

    def _get_next_decoder_input(
        self,
        prev_input: torch.LongTensor,
        selection: torch.LongTensor,
        incr_state_inds: torch.LongTensor,
    ) -> torch.LongTensor:
        """
        Return next decoder input.

        :param prev_input:
            previous input to decoder
        :param selection:
            token selections for current timestep
        :param inds:
            incremental state indices

        :return decoder input:
            return decoder input for next timestep
        """
        prev_input = torch.index_select(prev_input, 0, incr_state_inds)
        decoder_input = torch.cat([prev_input, selection], dim=-1)
        return decoder_input

    def _generate(
        self,
        batch: Batch,
        beam_size: int,
        max_ts: int,
        prefix_tokens: Optional[torch.LongTensor] = None,
    ):
        """
        Generate an output with beam search.

        Depending on the options, this may perform greedy/topk/nucleus generation.

        :param Batch batch:
            Batch structure with input and labels
        :param int beam_size:
            Size of each beam during the search
        :param int max_ts:
            the maximum length of the decoded sequence
        :param prefix_tokens:
            if given, a tensor of tokens that must begin the decoded sequence.

        :return:
            tuple (beam_pred_scores, beams)

            - beam_preds_scores: list of (prediction, score) pairs for each sample in
              Batch
            - beams :list of Beam instances defined in Beam class, can be used for any
              following postprocessing, e.g. dot logging.
        """
        model = self.model
        if isinstance(model, torch.nn.parallel.DistributedDataParallel):
            model = self.model.module
        encoder_states = model.encoder(*self._encoder_input(batch))
        if batch.text_vec is not None:
            dev = batch.text_vec.device
        else:
            assert batch.label_vec is not None, "need label_vec for _generate"
            dev = batch.label_vec.device

        bsz = batch.batchsize
        if batch.text_vec is not None:
            batchsize = batch.batchsize
            beams = [
                self._treesearch_factory(dev)
                .set_context(self._get_context(batch, batch_idx))
                .set_block_list(self.beam_block_list)
                for batch_idx in range(batchsize)
            ]
        else:
            beams = [self._treesearch_factory(dev) for _ in range(bsz)]

        # repeat encoder outputs and decoder inputs
        decoder_input = self._get_initial_decoder_input(bsz, beam_size, dev)

        inds = torch.arange(bsz).to(dev).view(-1, 1).repeat(1, beam_size).view(-1)
        encoder_states = model.reorder_encoder_states(encoder_states, inds)
        incr_state = None
        reorder_state: Optional[Tensor] = None

        if batch.get('constraints') is not None:
            #init constraints for the constrained beam search
            [b.search.init_constraints(batch['constraints'], beam_size) if b.search is not None else 0 for b in beams]

        self.pad = self.dict[self.dict.null_token]
        self.unk = self.dict[self.dict.unk_token]
        self.eos = self.dict[self.dict.end_token]
        self.bos = self.dict[self.dict.start_token]
        self.unk_penalty = 0.0

        for _ts in range(max_ts):
            if all((b.is_done() for b in beams)):
                # exit early if possible
                break

            score, incr_state = model.decoder(decoder_input, encoder_states, incr_state)
            # only need the final hidden state to make the word prediction
            score = score[:, -1:, :]
            score = model.output(score)
            # score contains softmax scores for bsz * beam_size samples
            score = score.view(bsz, beam_size, -1)
            if self.temperature != 1.0:
                score.div_(self.temperature)
            # force to fp32 to avoid overflow issues during search calculations
            score = F.log_softmax(score, dim=-1, dtype=torch.float32)  # type: ignore
            score[score != score] = torch.tensor(-math.inf).to(score)
            score[:,:, self.pad] = -math.inf  # never select pad
            score[:,:, self.unk] -= self.unk_penalty  # apply unk penalty

            if prefix_tokens is not None and _ts < prefix_tokens.size(1):
                # generate prefix_tokens for every timestep that they exist
                # achieve by setting score of all other tokens to be -inf
                prefix_toks = prefix_tokens[:, _ts].unsqueeze(-1).repeat(1, beam_size)
                prefix_score = score.gather(-1, prefix_toks.unsqueeze(-1))
                prefix_mask = prefix_toks.ne(self.NULL_IDX)
                score[prefix_mask] = neginf(score.dtype)
                score[prefix_mask] = score[prefix_mask].scatter_(
                    -1,
                    prefix_toks[prefix_mask].unsqueeze(-1),
                    prefix_score[prefix_mask],
                )
            for i, b in enumerate(beams):
                if not b.is_done():
                    b.advance(score[i])

            #if True:
            #    if _ts >= 1:
            #        for i in range(self.beam_size):
            #            print(f'Tokens[{i}]: {self._v2t(beams[0].tokens[i, : _ts + 1])}')
            #            #print(f'Tokens[{j}]: {self._v2t(tokens[j][ : ])}')

            incr_state_inds = torch.cat(
                [
                    beam_size * i + b.get_backtrack_from_current_step()
                    for i, b in enumerate(beams)
                ]
            )
            incr_state = model.reorder_decoder_incremental_state(
                incr_state, incr_state_inds
            )
            selection = torch.cat(
                [b.get_output_from_current_step() for b in beams]
            ).unsqueeze(-1)
            decoder_input = self._get_next_decoder_input(
                decoder_input, selection, incr_state_inds
            )
            encoder_states = model.reorder_encoder_states(encoder_states, incr_state_inds)

        if isinstance(beams[0].search, LexicallyConstrainedBeamSearch):
            # sort by score descending
            finalized = beams[0].cbs_finalized
            for sent in range(len(finalized)):
                scores = torch.tensor(
                    [float(elem["score"].item()) for elem in finalized[sent]]
                )
                _, sorted_scores_indices = torch.sort(scores, descending=True)
                finalized[sent] = [finalized[sent][ssi] for ssi in sorted_scores_indices]
                finalized[sent] = torch.jit.annotate(
                    List[Dict[str, Tensor]], finalized[sent]
                )
                finalized = [(b['tokens'], b['score']) for b in finalized[sent]]
            return finalized, beams
        else:
            # get all finalized candidates for each sample (and validate them)
            n_best_beam_preds_scores = [b.get_rescored_finished() for b in beams]
            if hasattr(self, '_rerank_beams'):
                n_best_beam_preds_scores = self._rerank_beams(  # type: ignore
                    batch, n_best_beam_preds_scores
                )
            #get the top prediction for each beam (i.e. minibatch sample)
            beam_preds_scores = [n_best_list[0] for n_best_list in n_best_beam_preds_scores]
            return beam_preds_scores, beams

    def _load_beam_block_list(self) -> SearchBlocklist:
        """
        Load the beam block_list.

        :return: a dict mapping ngram length to different ngrams
        """
        block_list = SearchBlocklist(self.dict)
        if not self.opt.get('beam_block_list_filename'):
            return block_list

        block_list_fn = self.opt['beam_block_list_filename']
        try:
            with PathManager.open(block_list_fn) as f:
                for line in f:
                    block_list.add(line.strip())
        except IOError:
            logging.error(
                f"Could not load beam block_list {block_list_fn}, using empty block_list."
            )
        return block_list


class _HypothesisTail(object):
    """
    Hold some bookkeeping about a hypothesis.
    """

    # use slots because we don't want dynamic attributes here
    __slots__ = ['timestep', 'hypid', 'score', 'tokenid']

    def __init__(self, timestep, hypid, score, tokenid):
        self.timestep = timestep
        self.hypid = hypid
        self.score = score
        self.tokenid = tokenid


class TreeSearch(object):
    """
    Abstract Tree Search class.

    It keeps information about beam_size concurrent, developing hypotheses. Concrete
    implementations make choices about which token to explore next at each point in the
    tree. Different choices result in different generation algorithms.
    """

    def __init__(
        self,
        beam_size,
        block_ngram=-1,
        context_block_ngram=-1,
        padding_token=0,
        bos_token=1,
        eos_token=2,
        min_length=3,
        device='cpu',
        length_penalty=0.65,
    ):
        """
        Instantiate Beam object.

        :param beam_size:
            number of hypothesis in the beam
        :param block_ngram:
            size of ngrams to block.
        :param context_block_ngram:
            size of context ngrams to block
        :param padding_token:
            padding token ID
        :param bos_token:
            beginning of sentence token ID
        :param eos_token:
            end of sentence token ID
        :param min_length:
            minimum length of the predicted sequence
        :param device:
            What device to use for computations
        """
        self.beam_size = beam_size
        self.length_penalty = length_penalty
        self.block_ngram = block_ngram
        self.min_length = min_length
        self.eos = eos_token
        self.bos = bos_token
        self.pad = padding_token
        self.context = None
        self.context_block_ngram = context_block_ngram
        self.block_list: Optional[SearchBlocklist] = None
        self.device = device
        # recent score for each hypo in the beam
        self.scores = None
        # self.scores values per each time step
        self.all_scores = [torch.Tensor([0.0] * beam_size).to(self.device)]
        # backtracking id to hypothesis at previous time step
        self.bookkeep = []
        # output tokens at each time step
        self.outputs = [
            torch.Tensor(self.beam_size).long().fill_(self.bos).to(self.device)
        ]
        # keeps tuples (score, time_step, hyp_id)
        self.finished = []
        self.eos_top = False
        self.eos_top_ts = None
        self.n_best_counter = 0
        self.partial_hyps = [[self.bos] for i in range(beam_size)]

    def set_context(self: TSType, context: torch.LongTensor) -> TSType:
        """
        Set the internal context representation and return self.

        :param context:
            a LongTensor representing the input context; used for context
            ngram blocking, if supplied
        """
        self.context = context.tolist()
        return self

    def set_block_list(self: TSType, block_list: Optional[SearchBlocklist]) -> TSType:
        self.block_list = block_list
        return self

    def get_output_from_current_step(self):
        """
        Get the outputput at the current step.
        """
        return self.outputs[-1]

    def get_backtrack_from_current_step(self):
        """
        Get the backtrack at the current step.
        """
        return self.bookkeep[-1]

    @abstractmethod
    def select_paths(self, logprobs, prior_scores, current_length):
        """
        Select the next vocabulary item in these beams.

        :param logprobs:
            a (beamsize x vocab) tensor of log probabilities. If this is the first
            turn in the dialogue, it will be a (1 x vocab) tensor.
        :param prior_scores:
            a (beamsize) tensor of weights with the cumulative running
            log-probability of each beam. If the first turn, it will be a (1) tensor.
        :param current_length:
            the current length in tokens
        :return:
            a (hypothesis_ids, token_id, scores) tuple, where:

            - hypothesis_ids is a LongTensor of hypotheses we're extending. May have
              repeats, but should always be (beamsize) long.
            - token_ids is a (beamsize) LongTensor of next-token choices for
              each of the hypotheses.
            - scores is a (beamsize) Tensor with the updated cumulative log-probs
              of each beam.
        """
        pass

    def _block_ngrams(
        self, ngram_size: int, logprobs: torch.Tensor, source: torch.LongTensor = None
    ):
        """
        Hard block ngrams from the logprobs, based on the source.

        :param ngram_size:
            The length of ngrams to block. Must be > 0.
        :param logprobs:
            Float or HalfTensor, representing the log-probabilities. This is
            modified in place.
        :param source:
            Source text to grab ngrams from. If None, it uses the current
            hypothesis (i.e. self-blocking).
        """
        for beam_id, hyp in enumerate(self.partial_hyps):
            if len(hyp) < ngram_size - 1:
                continue
            source_ = hyp if source is None else source
            ngrams = self._find_ngrams(source_, ngram_size)
            prefix = hyp[-(ngram_size - 1) :]
            for ngram in ngrams:
                if ngram_size == 1 or prefix == list(ngram[:-1]):
                    logprobs[beam_id][ngram[-1]] = neginf(logprobs.dtype)
        return logprobs

    def _block_block_list(self, logprobs: torch.Tensor) -> torch.Tensor:
        if self.block_list is None:
            return logprobs

        for beam_id, hyp in enumerate(self.partial_hyps):
            for ngram_size, bad_ngrams in self.block_list.items():
                prefix = hyp[-(ngram_size - 1) :]
                for ngram in bad_ngrams:
                    if (ngram_size == 1) or prefix == list(ngram[:-1]):
                        logprobs[beam_id][ngram[-1]] = neginf(logprobs.dtype)
        return logprobs

    def advance(self, logprobs):
        """
        Advance the beam one step.
        """
        current_length = len(self.all_scores) - 1
        if current_length < self.min_length:
            # penalize all eos probs to make it decode longer
            for hyp_id in range(logprobs.size(0)):
                logprobs[hyp_id][self.eos] = neginf(logprobs.dtype)

        if self.scores is None:
            self.scores = torch.zeros(1).type_as(logprobs).to(logprobs.device)

        # penalize hypotheses ending in EOS on the prior scores (self.scores) level
        # this is related to search which uses prior scores (self.scores) (e.g. beam)
        for hyp_id, token in enumerate(self.outputs[-1]):
            if token == self.eos:
                self.scores[hyp_id] = neginf(self.scores.dtype)

        # beam blocking
        if self.block_ngram > 0:
            logprobs = self._block_ngrams(self.block_ngram, logprobs, None)

        logprobs = self._block_block_list(logprobs)

        if self.context_block_ngram > 0:
            if self.context is None:
                raise ValueError(
                    "Must use TreeSearch.set_context to use context blocking."
                )
            logprobs = self._block_ngrams(
                self.context_block_ngram, logprobs, self.context
            )

        hyp_ids, tok_ids, self.scores = self.select_paths(
            logprobs, self.scores, current_length
        )
        # use clone() here to ensure that self.all_scores will not be changed
        # later due to any penalties to self.scores
        self.all_scores.append(self.scores.clone())

        self.outputs.append(tok_ids)
        self.bookkeep.append(hyp_ids)
        self.partial_hyps = [
            self.partial_hyps[hyp_ids[i]] + [tok_ids[i].item()]
            for i in range(self.beam_size)
        ]

        #  check new hypos for eos label, if we have some, add to finished
        for hypid in range(self.beam_size):
            if self.outputs[-1][hypid] == self.eos:
                if self.scores[hypid] <= neginf(self.scores.dtype):
                    continue
                #  this is finished hypo, adding to finished
                eostail = _HypothesisTail(
                    timestep=len(self.outputs) - 1,
                    hypid=hypid,
                    score=self.all_scores[-1][hypid],
                    tokenid=self.eos,
                )
                self.finished.append(eostail)
                self.n_best_counter += 1

        if self.outputs[-1][0] == self.eos:
            self.eos_top = True
            if self.eos_top_ts is None:
                self.eos_top_ts = len(self.outputs) - 1

    def is_done(self):
        """
        Return whether beam search is complete.
        """
        return self.eos_top and self.n_best_counter >= self.beam_size

    def _find_ngrams(self, input_list, n):
        """
        Find ngrams of size n in input list.
        """
        return list(zip(*[input_list[i:] for i in range(n)]))

    def _get_hyp_from_finished(self, hypothesis_tail):
        """
        Extract hypothesis ending with EOS at timestep with hyp_id.

        :param timestep:
            timestep with range up to len(self.outputs) - 1

        :param hyp_id:
            id with range up to beam_size - 1

        :return:
            hypothesis sequence
        """
        hyp_idx = []
        endback = hypothesis_tail.hypid
        for i in range(hypothesis_tail.timestep, -1, -1):
            hyp_idx.append(
                _HypothesisTail(
                    timestep=i,
                    hypid=endback,
                    score=self.all_scores[i][endback],
                    tokenid=self.outputs[i][endback],
                )
            )
            endback = self.bookkeep[i - 1][endback]

        return hyp_idx

    def _get_pretty_hypothesis(self, list_of_hypotails):
        """
        Return hypothesis as a tensor of token ids.
        """
        return torch.stack([ht.tokenid for ht in reversed(list_of_hypotails)])

    def get_rescored_finished(self, n_best=None):
        """
        Return finished hypotheses according to adjusted scores.

        Score adjustment is done according to the Google NMT paper, which
        penalizes long utterances.

        :param n_best:
            number of finalized hypotheses to return

        :return:
            list of (tokens, score) pairs, in sorted order, where:
              - tokens is a tensor of token ids
              - score is the adjusted log probability of the entire utterance
        """
        # if we never actually finished, force one
        if not self.finished:
            self.outputs[-1][0] = self.eos
            self.finished.append(
                _HypothesisTail(
                    timestep=len(self.outputs) - 1,
                    hypid=0,
                    score=self.all_scores[-1][0],
                    tokenid=self.outputs[-1][0],
                )
            )

        rescored_finished = []
        for finished_item in self.finished:
            current_length = finished_item.timestep + 1
            # these weights are from Google NMT paper
            length_penalty = math.pow((1 + current_length) / 6, self.length_penalty)
            rescored_finished.append(
                _HypothesisTail(
                    timestep=finished_item.timestep,
                    hypid=finished_item.hypid,
                    score=finished_item.score / length_penalty,
                    tokenid=finished_item.tokenid,
                )
            )

        # Note: beam size is almost always pretty small, so sorting is cheap enough
        srted = sorted(rescored_finished, key=attrgetter('score'), reverse=True)

        if n_best is not None:
            srted = srted[:n_best]

        n_best_list = [
            (self._get_pretty_hypothesis(self._get_hyp_from_finished(hyp)), hyp.score)
            for hyp in srted
        ]

        # check that there is at least one finished candidate
        # and assert that each of them contains only one EOS
        assert (
            len(n_best_list) >= 1
        ), f'TreeSearch returned {len(n_best_list)} candidates, must be >= 1'
        for (pred, score) in n_best_list:
            assert (pred == self.eos).sum() == 1, (
                f'TreeSearch returned a finalized hypo with multiple end tokens '
                f'with score {score.item():.2f}'
            )

        return n_best_list


class GreedySearch(TreeSearch):
    """
    Greedy search.

    Picks the highest probability utterance at each step.  Only works with
    --beam-size 1.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.beam_size != 1:
            raise ValueError('Greedy search can only be run with beam size 1.')

    def select_paths(self, logprobs, prior_scores, current_length):
        tok_scores, tok_ids = logprobs.max(1)
        best_scores = tok_scores + prior_scores
        hyp_ids = torch.arange(logprobs.size(0)).to(logprobs.device)
        return (hyp_ids, tok_ids, best_scores)


class BeamSearch(TreeSearch):
    """
    Beam search.
    """

    def select_paths(self, logprobs, prior_scores, current_length):
        """
        Select the next vocabulary item in these beams.
        """
        # if numel is 1, then this is the first time step, only one hyp is expanded
        if prior_scores.numel() == 1:
            logprobs = logprobs[0:1]

        # beam search actually looks over all hypotheses together so we flatten
        beam_scores = logprobs + prior_scores.unsqueeze(1).expand_as(logprobs)
        flat_beam_scores = beam_scores.view(-1)
        best_scores, best_idxs = torch.topk(flat_beam_scores, self.beam_size, dim=-1)
        voc_size = logprobs.size(-1)

        # get the backtracking hypothesis id as a multiple of full voc_sizes
        hyp_ids = best_idxs // voc_size
        # get the actual word id from residual of the same division
        tok_ids = best_idxs % voc_size

        return (hyp_ids, tok_ids, best_scores)


class DelayedBeamSearch(TreeSearch):
    """
    DelayedBeam: Top-K sampling followed by beam search (Massarelli et al., 2019).

    Samples from a truncated distribution where only the most probable K words
    are considered at each time for the first N tokens, then switches to beam
    after N steps.

    See https://arxiv.org/abs/1911.03587 for details.
    """

    def __init__(self, k, delay, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.k = k
        self.delay = delay

    def select_paths(self, logprobs, prior_scores, current_length):
        if current_length < self.delay:
            return TopKSampling.select_paths(
                self, logprobs, prior_scores, current_length
            )
        else:
            return BeamSearch.select_paths(self, logprobs, prior_scores, current_length)


class TopKSampling(TreeSearch):
    """
    Top-K sampling (Fan et al., 2018).

    Samples from a truncated distribution where only the most probable K words
    are considered at each time.

    Typical values of k are 2, 10, 50.

    See https://arxiv.org/abs/1805.04833 for details.
    """

    def __init__(self, k, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.k = k

    def select_paths(self, logprobs, prior_scores, current_length):
        values, indices = logprobs.topk(self.k, dim=-1)
        probs = torch.softmax(values, dim=-1)
        choices = torch.multinomial(probs, 1)[:, 0]
        hyp_ids = torch.arange(logprobs.size(0)).to(logprobs.device)
        tok_ids = indices[hyp_ids, choices]
        scores = values[hyp_ids, choices]
        best_scores = prior_scores.expand_as(scores) + scores
        return (hyp_ids, tok_ids, best_scores)


class NucleusSampling(TreeSearch):
    """
    Nucelus, aka top-p sampling (Holtzman et al., 2019).

    Samples from a truncated distribution which covers a fixed CDF proportion
    of the original distribution.

    Typical values of p are 0.3 and 0.9.

    See https://arxiv.org/abs/1904.09751 for details.
    """

    def __init__(self, p, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.p = p

    def select_paths(self, logprobs, prior_scores, current_length):
        # Unlike the other treesearch methods, we have to switch to linspace
        # for the probabilities in order to compute the CDF.
        probs = torch.softmax(logprobs, dim=-1)
        sprobs, sinds = probs.sort(dim=-1, descending=True)
        # The subtraction here is to get the exclusive prefix sum,
        # to guarantee the first element is not masked
        mask = (sprobs.cumsum(dim=-1) - sprobs) >= self.p
        sprobs[mask] = 0
        sprobs.div_(sprobs.sum(dim=-1).unsqueeze(1))
        choices = torch.multinomial(sprobs, 1)[:, 0]
        hyp_ids = torch.arange(logprobs.size(0)).to(logprobs.device)
        tok_ids = sinds[hyp_ids, choices]
        # Convert back to logspace.
        scores = sprobs[hyp_ids, choices].log()
        best_scores = prior_scores.expand_as(scores) + scores
        return (hyp_ids, tok_ids, best_scores)

class ConstrainedBeamSearch(TreeSearch):
    """
    Constrained Beam search.
    """
    def __init__(self, dictionary, constraints, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_dictionary = dictionary
        self.constraints = constraints
        self.search = LexicallyConstrainedBeamSearch(
            self.target_dictionary, constraints
        )
        self.max_len=127
        self.normalize_scores = True
        self.len_penalty = 1.0
        self.match_source_len = False
        #self.pad = dictionary[dictionary.null_token]
        #self.unk = dictionary[dictionary.unk_token]
        #self.eos = dictionary[dictionary.end_token]
        #self.bos = dictionary[dictionary.start_token]

        bsz=1 #batch size - assumed to be 1 for inferrence
        # initialize buffers
        self.cbs_scores = (
            torch.zeros(bsz * self.beam_size, self.max_len + 1).to(self.device).float()
        )  # +1 for eos; pad is never chosen for scoring
        self.tokens = (
            torch.zeros(bsz * self.beam_size, self.max_len + 2)
                .to(self.device)
                .long()
                .fill_(self.pad)
        )  # +2 for eos and pad
        self.tokens[:, 0] = self.eos if self.bos is None else self.bos

        # A list that indicates candidates that should be ignored.
        # For example, suppose we're sampling and have already finalized 2/5
        # samples. Then cands_to_ignore would mark 2 positions as being ignored,
        # so that we only finalize the remaining 3 samples.
        self.cands_to_ignore = (
            torch.zeros(bsz, self.beam_size).to(self.device).eq(-1)
        )  # forward and backward-compatible False mask

        # number of candidate hypos per step
        self.cand_size = 2 * self.beam_size  # 2 x beam size in case half are EOS

        # offset arrays for converting between different indexing schemes
        self.bbsz_offsets = (torch.arange(0, bsz) * self.beam_size).unsqueeze(1).type_as(self.tokens)
        self.cand_offsets = torch.arange(0, self.cand_size).type_as(self.tokens)

        self.reorder_state: Optional[Tensor] = None
        self.batch_idxs: Optional[Tensor] = None

        self.original_batch_idxs: Optional[Tensor] = None
        self.original_batch_idxs = torch.arange(0, bsz).type_as(self.tokens)
        self.eos_bbsz_idx = torch.empty(0).to(
            self.tokens
        )  # indices of hypothesis ending with eos (finished sentences)
        self.eos_scores = torch.empty(0).to(
            self.cbs_scores
        ) # scores of hypothesis ending with eos (finished sentences)
        # list of completed sentences
        self.cbs_finalized = torch.jit.annotate(
            List[List[Dict[str, Tensor]]],
            [torch.jit.annotate(List[Dict[str, Tensor]], []) for i in range(bsz)],
        )  # contains lists of dictionaries of infomation about the hypothesis being finalized at each step
        self.cbs_finished = [
            False for i in range(bsz)
        ]  # a boolean array indicating if the sentence at the index is finished or not
        self.num_remaining_sent = bsz
        self.finalized_sents: List[int] = []

    def is_done(self):
        """
        Return whether beam search is complete.
        """
        return self.num_remaining_sent==0 #or (self.eos_top and self.n_best_counter >= self.beam_size)

        #if self.eos_bbsz_idx.numel() > 0:
        #    return True
        #return False

    def select_paths(self, logprobs, prior_scores, current_length):
        """
        Select the next vocabulary item in these beams.
        """

        beam_size, vocab_size = logprobs.size()
        bsz = 1
        step=current_length
        lprobs = logprobs.view(bsz, -1, vocab_size)

        self.cbs_scores = self.cbs_scores.type_as(lprobs)

        # Shape: (batch, cand_size)
        cand_scores, cand_indices, cand_beams = self.search.step(
                step=step,
                lprobs=lprobs.view(bsz, -1, vocab_size),
                scores=self.cbs_scores.view(bsz, beam_size, -1)[:, :, :step],
                #prev_output_tokens=self.tokens[:, : step + 1],
                #original_batch_idxs=self.original_batch_idxs,
        )
        #scores, indices, beams = self.cbs.step(step=current_length, lprobs=lprobs,scores=prior_scores.view(1, beam_size, -1)[:, :, :current_length])

        # cand_bbsz_idx contains beam indices for the top candidate
        # hypotheses, with a range of values: [0, bsz*beam_size),
        # and dimensions: [bsz, cand_size]
        cand_bbsz_idx = cand_beams.add(self.bbsz_offsets)

        # finalize hypotheses that end in eos
        # Shape of eos_mask: (batch size, beam size)
        self.eos_mask = cand_indices.eq(self.eos) & cand_scores.ne(-math.inf)
        self.eos_mask[:, :beam_size][self.cands_to_ignore] = torch.tensor(0).to(self.eos_mask)

        # only consider eos when it's among the top beam_size indices
        # Now we know what beam item(s) to finish
        # Shape: 1d list of absolute-numbered
        self.eos_bbsz_idx = torch.masked_select(
            cand_bbsz_idx[:, :beam_size], mask=self.eos_mask[:, :beam_size]
        )

        #__________ finished cents omitted
        finalized_sents: List[int] = []
        if self.eos_bbsz_idx.numel() > 0:
            self.eos_scores = torch.masked_select(
                cand_scores[:, :beam_size], mask=self.eos_mask[:, :beam_size]
            )
            attn = None
            finalized_sents = self.finalize_hypos(
                step,
                self.eos_bbsz_idx,
                self.eos_scores,
                self.tokens,
                self.cbs_scores,
                self.cbs_finalized,
                self.cbs_finished,
                beam_size,
                attn,
                0,
                self.max_len,
            )
            self.num_remaining_sent -= len(finalized_sents)

        #assert self.num_remaining_sent >= 0
        #if self.num_remaining_sent == 0:
        #if self.search.stop_on_max_len and step >= self.max_len:

        assert self.num_remaining_sent >= 0
        #if num_remaining_sent == 0:

        assert step < self.max_len

        # Set active_mask so that values > cand_size indicate eos hypos
        # and values < cand_size indicate candidate active hypos.
        # After, the min values per row are the top candidate active hypos

        # Rewrite the operator since the element wise or is not supported in torchscript.

        self.eos_mask[:, :beam_size] = ~((~self.cands_to_ignore) & (~self.eos_mask[:, :beam_size]))
        active_mask = torch.add(
            self.eos_mask.type_as(self.cand_offsets) * self.cand_size,
            self.cand_offsets[: self.eos_mask.size(1)],
        )

        # get the top beam_size active hypotheses, which are just
        # the hypos with the smallest values in active_mask.
        # {active_hypos} indicates which {beam_size} hypotheses
        # from the list of {2 * beam_size} candidates were
        # selected. Shapes: (batch size, beam size)
        new_cands_to_ignore, active_hypos = torch.topk(
            active_mask, k=beam_size, dim=1, largest=False
        )

        # update cands_to_ignore to ignore any finalized hypos.
        self.cands_to_ignore = new_cands_to_ignore.ge(self.cand_size)[:, :beam_size]
        # Make sure there is at least one active item for each sentence in the batch.
        assert (~self.cands_to_ignore).any(dim=1).all()

        # update cands_to_ignore to ignore any finalized hypos

        # {active_bbsz_idx} denotes which beam number is continued for each new hypothesis (a beam
        # can be selected more than once).
        active_bbsz_idx = torch.gather(cand_bbsz_idx, dim=1, index=active_hypos)
        active_scores = torch.gather(cand_scores, dim=1, index=active_hypos)

        active_bbsz_idx = active_bbsz_idx.view(-1)
        active_scores = active_scores.view(-1)

        # copy tokens and scores for active hypotheses

        # Set the tokens for each beam (can select the same row more than once)
        self.tokens[:, : step + 1] = torch.index_select(
            self.tokens[:, : step + 1], dim=0, index=active_bbsz_idx
        )
        # Select the next token for each of them
        self.tokens.view(bsz, beam_size, -1)[:, :, step + 1] = torch.gather(
            cand_indices, dim=1, index=active_hypos
        )
        if step > 0:
            self.cbs_scores[:, :step] = torch.index_select(
                self.cbs_scores[:, :step], dim=0, index=active_bbsz_idx
            )
        self.cbs_scores.view(bsz, beam_size, -1)[:, :, step] = torch.gather(
            cand_scores, dim=1, index=active_hypos
        )

        self.active_hypos_global = active_bbsz_idx
        # Update constraints based on which candidates were selected for the next beam
        self.search.update_constraints(active_hypos)

        hyp_ids = active_bbsz_idx
        tok_ids = self.tokens[:,step+1]
        best_scores = self.cbs_scores[:,step]

        return (hyp_ids, tok_ids, best_scores)

    def finalize_hypos(
        self,
        step: int,
        bbsz_idx,
        eos_scores,
        tokens,
        scores,
        finalized: List[List[Dict[str, Tensor]]],
        finished: List[bool],
        beam_size: int,
        attn: Optional[Tensor],
        src_lengths,
        max_len: int,
    ):
        """Finalize hypothesis, store finalized information in `finalized`, and change `finished` accordingly.
        A sentence is finalized when {beam_size} finished items have been collected for it.

        Returns number of sentences (not beam items) being finalized.
        These will be removed from the batch and not processed further.
        Args:
            bbsz_idx (Tensor):
        """
        assert bbsz_idx.numel() == eos_scores.numel()

        # clone relevant token and attention tensors.
        # tokens is (batch * beam, max_len). So the index_select
        # gets the newly EOS rows, then selects cols 1..{step + 2}
        tokens_clone = tokens.index_select(0, bbsz_idx)[
            :, 1 : step + 2
        ]  # skip the first index, which is EOS

        tokens_clone[:, step] = self.eos
        attn_clone = (
            attn.index_select(0, bbsz_idx)[:, :, 1 : step + 2]
            if attn is not None
            else None
        )

        # compute scores per token position
        pos_scores = scores.index_select(0, bbsz_idx)[:, : step + 1]
        pos_scores[:, step] = eos_scores
        # convert from cumulative to per-position scores
        pos_scores[:, 1:] = pos_scores[:, 1:] - pos_scores[:, :-1]

        # normalize sentence-level scores
        if self.normalize_scores:
            eos_scores /= (step + 1) ** self.len_penalty

        # cum_unfin records which sentences in the batch are finished.
        # It helps match indexing between (a) the original sentences
        # in the batch and (b) the current, possibly-reduced set of
        # sentences.
        cum_unfin: List[int] = []
        prev = 0
        for f in finished:
            if f:
                prev += 1
            else:
                cum_unfin.append(prev)

        # set() is not supported in script export

        # The keys here are of the form "{sent}_{unfin_idx}", where
        # "unfin_idx" is the index in the current (possibly reduced)
        # list of sentences, and "sent" is the index in the original,
        # unreduced batch
        sents_seen: Dict[str, Optional[Tensor]] = {}

        # For every finished beam item
        for i in range(bbsz_idx.size()[0]):
            idx = bbsz_idx[i]
            score = eos_scores[i]
            # sentence index in the current (possibly reduced) batch
            unfin_idx = idx // beam_size
            # sentence index in the original (unreduced) batch
            sent = unfin_idx + cum_unfin[unfin_idx]
            # print(f"{step} FINISHED {idx} {score} {sent}={unfin_idx} {cum_unfin}")
            # Cannot create dict for key type '(int, int)' in torchscript.
            # The workaround is to cast int to string
            seen = str(sent.item()) + "_" + str(unfin_idx.item())
            if seen not in sents_seen:
                sents_seen[seen] = None

            if self.match_source_len and step > src_lengths[unfin_idx]:
                score = torch.tensor(-math.inf).to(score)

            # An input sentence (among those in a batch) is finished when
            # beam_size hypotheses have been collected for it
            if len(finalized[sent]) < beam_size:
                if attn_clone is not None:
                    # remove padding tokens from attn scores
                    hypo_attn = attn_clone[i]
                else:
                    hypo_attn = torch.empty(0)

                finalized[sent].append(
                    {
                        "tokens": tokens_clone[i],
                        "score": score,
                        "attention": hypo_attn,  # src_len x tgt_len
                        "alignment": torch.empty(0),
                        "positional_scores": pos_scores[i],
                    }
                )

        newly_finished: List[int] = []

        for seen in sents_seen.keys():
            # check termination conditions for this sentence
            sent: int = int(float(seen.split("_")[0]))
            unfin_idx: int = int(float(seen.split("_")[1]))

            if not finished[sent] and self.is_finished(
                step, unfin_idx, max_len, len(finalized[sent]), beam_size
            ):
                finished[sent] = True
                newly_finished.append(unfin_idx)

        return newly_finished

    def is_finished(
        self,
        step: int,
        unfin_idx: int,
        max_len: int,
        finalized_sent_len: int,
        beam_size: int,
    ):
        """
        Check whether decoding for a sentence is finished, which
        occurs when the list of finalized sentences has reached the
        beam size, or when we reach the maximum length.
        """
        assert finalized_sent_len <= beam_size
        if finalized_sent_len == beam_size or step == max_len:
            return True
        return False