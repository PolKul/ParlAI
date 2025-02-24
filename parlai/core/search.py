# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math
from typing import List, Optional

import torch
import torch.nn as nn
from parlai.core.dict import DictionaryAgent
from parlai.core.token_generation_constraints import (
    ConstraintState,
    OrderedConstraintState,
    UnorderedConstraintState,
)
from torch import Tensor


class Search(nn.Module):
    def __init__(self, tgt_dict: DictionaryAgent):
        super().__init__()
        self.pad = tgt_dict[tgt_dict.null_token]
        self.unk = tgt_dict[tgt_dict.unk_token]
        self.eos = tgt_dict[tgt_dict.end_token]
        self.vocab_size = len(tgt_dict)
        self.src_lengths = torch.tensor(-1)
        self.supports_constraints = False
        self.stop_on_max_len = False

    def step(
        self, step, lprobs, scores, prev_output_tokens=None, original_batch_idxs=None
    ):
        """Take a single search step.

        Args:
            step: the current search step, starting at 0
            lprobs: (bsz x input_beam_size x vocab_size)
                the model's log-probabilities over the vocabulary at the current step
            scores: (bsz x input_beam_size x step)
                the historical model scores of each hypothesis up to this point
            prev_output_tokens: (bsz x step)
                the previously generated oputput tokens
            original_batch_idxs: (bsz)
                the tensor with the batch indices, in the range [0, bsz)
                this is useful in case there has been applied a re-ordering
                and we need to know the orignal indices

        Return: A tuple of (scores, indices, beams) where:
            scores: (bsz x output_beam_size)
                the scores of the chosen elements; output_beam_size can be
                larger than input_beam_size, e.g., we may return
                2*input_beam_size to account for EOS
            indices: (bsz x output_beam_size)
                the indices of the chosen elements
            beams: (bsz x output_beam_size)
                the hypothesis ids of the chosen elements, in the range [0, input_beam_size)
        """
        raise NotImplementedError

    @torch.jit.export
    def set_src_lengths(self, src_lengths):
        self.src_lengths = src_lengths

    @torch.jit.export
    def init_constraints(self, batch_constraints: Optional[Tensor], beam_size: int):
        """Initialize constraint states for constrained decoding (if supported).

        Args:
            batch_constraints: (torch.Tensor, optional)
                the list of constraints, in packed form
            beam_size: (int)
                the beam size
        Returns:
            *encoder_out* rearranged according to *new_order*
        """
        pass

    def prune_sentences(self, batch_idxs: Tensor):
        """
        Removes constraint states for completed sentences (if supported).
        This is called from sequence_generator._generate() when sentences are
        deleted from the batch.

        Args:
            batch_idxs: Indices of *sentences* whose constraint state should be *kept*.
        """
        pass

    def update_constraints(self, active_hypos: Tensor):
        """
        Updates the constraint states by selecting the beam items that are retained.
        This is called at each time step of sequence_generator._generate() when
        the set of 2 * {beam_size} candidate hypotheses are reduced to the beam size.

        Args:
            active_hypos: (batch size, beam size)
              list of integers denoting, for each sentence, which beam candidate items
              should be kept.
        """
        pass

class LexicallyConstrainedBeamSearch(Search):
    """Implements lexically constrained beam search as described in

        Fast Lexically Constrained Decoding with Dynamic Beam
        Allocation for Neural Machine Translation.  Post & Vilar,
        NAACL 2018.  https://www.aclweb.org/anthology/N18-1119/

    and

        Improved Lexically Constrained Decoding for Translation and
        Monolingual Rewriting. Hu et al, NAACL
        2019. https://www.aclweb.org/anthology/N19-1090/

    This is accomplished by maintaining, for each beam hypothesis, a
    ConstraintState object (see constraints.py) that tracks which
    constraints have been generated and using this information to
    shape the beam for each input sentence.
    """

    def __init__(self, tgt_dict, representation):
        super().__init__(tgt_dict)
        self.representation = representation
        self.vocab_size = len(tgt_dict)
        self.num_cands = 0
        self.supports_constraints = True
        self.constraints = []

    @torch.jit.export
    def init_constraints(self, batch_constraints: Optional[Tensor], beam_size: int):
        self.constraint_states = []
        self.constraints = batch_constraints
        for constraint_tensor in batch_constraints:
            if self.representation == "ordered":
                constraint_state = OrderedConstraintState.create(constraint_tensor)
            elif self.representation == "unordered":
                constraint_state = UnorderedConstraintState.create(constraint_tensor)

            self.constraint_states.append([constraint_state for i in range(beam_size)])

    @torch.jit.export
    def prune_sentences(self, batch_idxs: Tensor):
        self.constraint_states = [
            self.constraint_states[i] for i in batch_idxs.tolist()
        ]

    @torch.jit.export
    def update_constraints(self, active_hypos: Tensor):
        if self.constraint_states:
            batch_size = active_hypos.size(0)
            for sentid in range(batch_size):
                self.constraint_states[sentid] = [
                    self.constraint_states[sentid][i] for i in active_hypos[sentid]
                ]

    @torch.jit.export
    def step(
        self,
        step: int,
        lprobs: Tensor,
        scores: Optional[Tensor],
        prev_output_tokens: Optional[Tensor] = None,
        original_batch_idxs: Optional[Tensor] = None,
    ):
        """
        A constrained step builds a large candidates list from the following:
        - the top 2 * {beam_size} items over the whole beam
        - for each item in the beam
          - the top {each_k} (default 1)
          - all next constraints
        We then compute the constrained state of each beam item, and assign
        stripe codes: 0 to the best in each bank, 1 to the 2nd-best, and so
        on. We then sort by (stripe, score), and truncate the list at
        2 * beam size.

        Args:
            step: the decoder step
            lprobs: (batch size, beam size, target vocab)
                the target-vocab distributions for each item in the beam.
        Retrun: A tuple of (scores, indices, beams, constraints) where:
            scores: (batch, output beam size)
                the scores of the chosen elements
            indices: (batch, output beam size)
                the target vocab indices of the chosen elements
            beams: (batch, output beam size)
                the 0-indexed hypothesis ids of the chosen elements
            constraints: (batch, output beam size)
                the new constraint states
        """
        each_k = 1
        device = lprobs.device

        batch_size, beam_size, vocab_size = lprobs.size()

        self.num_cands = min(
            # Just take the k-best. We'll get another k from the 1-best from each
            # row, plus more from the constraints
            beam_size * 2,
            lprobs.view(batch_size, -1).size(1) - 1,  # -1 so we never select pad
        )

        # STEP 0: Preliminary. Prevent EOS for unfinished hyps across all batch items
        constraint_states = self.constraint_states
        if constraint_states and step > 0:
            not_finished_indices = []
            for sentno, sent_constraints in enumerate(constraint_states):
                for beamno, state in enumerate(sent_constraints):
                    index = sentno * beam_size + beamno
                    if not state.finished:
                        not_finished_indices.append(index)
            not_finished_indices = torch.tensor(not_finished_indices)
            if not_finished_indices.numel() > 0:
                lprobs.view(batch_size * beam_size, -1)[
                    not_finished_indices, self.eos
                ] = -math.inf

        if step == 0:
            # at the first step all hypotheses are equally likely, so use
            # only the first beam entry for each batch item
            lprobs = lprobs[:, ::beam_size, :].contiguous()
        else:
            # make probs contain cumulative scores for each hypothesis
            assert scores is not None
            lprobs = lprobs + scores[:, :, step - 1].unsqueeze(-1)

        top_prediction = torch.topk(
            lprobs.view(batch_size, -1),
            self.num_cands,
        )
        scores_buf, indices_buf = top_prediction
        # Project back into relative indices and beams
        beams_buf = indices_buf // vocab_size
        indices_buf = indices_buf.fmod(vocab_size)

        # Short circuit if there are no constraints in this batch
        if not constraint_states:
            return scores_buf, indices_buf, beams_buf

        # STEP 1: get top-1 from each hypothesis across all sentences in the batch
        if step > 0:
            top_scores, top_indices = torch.topk(
                lprobs.view(batch_size * beam_size, -1),
                k=each_k,
                dim=1,
            )
            top_scores = top_scores.view(batch_size, -1)
            top_indices = top_indices.view(batch_size, -1)
            scores_buf = torch.cat((scores_buf, top_scores), dim=1)
            indices_buf = torch.cat((indices_buf, top_indices), dim=1)
            new_beams = torch.arange(0, beam_size, device=device).repeat(batch_size, 1)
            beams_buf = torch.cat((beams_buf, new_beams), dim=1)

        # Now, process sentences in the batch one by one.
        new_scores_buf = torch.zeros((batch_size, 2 * beam_size), device=device)
        new_indices_buf = torch.zeros((batch_size, 2 * beam_size), device=device).long()
        new_beams_buf = torch.zeros((batch_size, 2 * beam_size), device=device).long()
        for sentno, states in enumerate(constraint_states):
            scores, indices, beams, new_states = self.step_sentence(
                step,
                sentno,
                lprobs[sentno],
                constraint_states[sentno],
                beams_buf[sentno].clone(),
                indices_buf[sentno].clone(),
                scores_buf[sentno].clone(),
            )
            new_scores_buf[sentno] = scores
            new_indices_buf[sentno] = indices
            new_beams_buf[sentno] = beams
            self.constraint_states[sentno] = new_states

        return new_scores_buf, new_indices_buf, new_beams_buf

    @torch.jit.export
    def step_sentence(
        self,
        step: int,
        sentno: int,
        lprobs: Tensor,
        constraint_states: List[List[ConstraintState]],
        beams_buf: Tensor,
        indices_buf: Tensor,
        scores_buf: Tensor,
    ):
        """Does per-sentence processing. Adds all constraints for each
        hypothesis to the list of candidates; then removes duplicates,
        sorts, and dynamically stripes across the banks. All tensor inputs
        are collapsed to those pertaining to a single input sentence.
        """
        device = lprobs.device

        # STEP 2: Add all constraints for each beam item
        for beamno, state in enumerate(constraint_states):
            next_tokens = torch.tensor(list(state.next_tokens()), device=device).long()
            if next_tokens.numel() != 0:
                indices_buf = torch.cat((indices_buf, next_tokens))
                next_beams = (
                    torch.tensor(beamno, device=device)
                    .repeat(next_tokens.size(0))
                    .long()
                )
                beams_buf = torch.cat((beams_buf, next_beams))
                next_values = lprobs[beamno].take(next_tokens.view(-1))
                scores_buf = torch.cat((scores_buf, next_values))

            # At the 0th time step, there is just one beam item
            if step == 0:
                break

        # STEP 3: Compute the "bank" for each candidate. This is the
        # number of constraints it's generated. We need this so that
        # we can do round-robin allocation of the beam across these
        # banks. If C is the number of constraints, we select the best
        # item in bank C, then the best in bank C-1, etc, followed by
        # the 2nd-best in bank C, the 2nd-best in bank C-1, etc, and so
        # on, until the maximum beam size. We accomplish this by
        # creating a sort key and striping across the banks.

        # Compute the new states for all candidates
        cands_size = indices_buf.size(0)
        constraint_states = [
            constraint_states[beams_buf[i]].advance(indices_buf[i])
            for i in range(cands_size)
        ]

        banks = torch.tensor([state.bank for state in constraint_states], device=device)

        # STEP 4: Sort
        num_constraint_tokens = len(state.tokens)

        # Sort by keys (bank, score) (i.e., sort banks together, and scores
        # within banks). AFAIK pytorch doesn't support either stable sort or
        # multi-key sorting, so we have to hack this.
        MAX_SCORE = -100
        sort_key = (num_constraint_tokens - banks) * MAX_SCORE + scores_buf
        sort_values, sort_indices = sort_key.sort(dim=0, descending=True)
        scores_buf = scores_buf[sort_indices]
        indices_buf = indices_buf[sort_indices]
        beams_buf = beams_buf[sort_indices]
        banks = banks[sort_indices]

        # Sort the constraints to follow suit
        constraint_states = [constraint_states[i] for i in sort_indices]

        # STEP 5: Remove duplicates. The topk calls (overall and
        # per-row) plus the per-row generation of constraints will
        # produce duplicates. Here we remove them.

        def roll(t):
            """Rolls a 1d tensor left by 1.

            [0, 1, 2, 3, 4] becomes [4, 0, 1, 2, 3]
            """
            return torch.cat((t[-1].unsqueeze(0), t[0:-1]), dim=0)

        # We map candidates (beam, token_id) to a single dimension.
        # This is then shifted by 1. We can then easily identify
        # duplicates and create a mask that identifies unique
        # extensions.
        uniques_mask = beams_buf * (self.vocab_size + 1) + indices_buf
        uniques_mask = roll(uniques_mask) != uniques_mask

        # Use the mask to pare down the data structures
        scores_buf = torch.masked_select(scores_buf, uniques_mask)
        indices_buf = torch.masked_select(indices_buf, uniques_mask)
        beams_buf = torch.masked_select(beams_buf, uniques_mask)
        banks = torch.masked_select(banks, uniques_mask)
        i = 1
        for mask in uniques_mask[1:]:
            if not mask:
                constraint_states.pop(i)
            i += mask

        # STEP 6: Assign IDs round-robin across banks, sort, and
        # truncate. Now that the candidates are sorted by (bank,
        # score) and uniqed, we dynamically allocate the {beam_size}
        # beam by striping across the candidates. These stripes will
        # be used as sort keys to do round-robin selection. This is
        # accomplished in a single pass with offsets. Sorting by
        # highest-banks (furthest-along hypotheses) first ensures
        # progress through the constraints.
        #
        # e.g., BANKS: 3 3 3 2 2 2 2 1 1 1 0 0
        # OLD STRIPES: 0 1 2 0 1 2 3 0 1 2 0 1
        # NEW STRIPES: 0 1+4 2+8 0+1 1+5 2+9 3+11 0+2 1+6 2+10 0+3 1+7
        #            = 0 5 10 1 6 11 13 2 7 12 3 8
        #
        # Sorting by this then gives the following banks:
        #
        #             3 2 1 0 3 2 1 0 3 2 1 2
        #
        # We'll take the top {beam_size} of these.
        stripe_offsets = [offset * (len(banks) + 1) for offset in range(len(banks) + 1)]
        stripes = torch.zeros_like(banks)
        cur_bank_count = -1
        cur_bank = banks[0]
        for i, bank in enumerate(banks):
            if bank != cur_bank:
                cur_bank_count = 0
                cur_bank = bank
            else:
                cur_bank_count += 1
            stripes[i] = num_constraint_tokens - bank + stripe_offsets[cur_bank_count]

        # STEP 7: Sort by the stripes values
        sort_values, sort_indices = stripes.sort(dim=0)
        scores_buf = scores_buf[sort_indices]
        indices_buf = indices_buf[sort_indices]
        beams_buf = beams_buf[sort_indices]
        constraint_states = [constraint_states[i] for i in sort_indices]

        # STEP 8: Truncate to the candidates size!
        scores_buf = scores_buf[: self.num_cands]
        indices_buf = indices_buf[: self.num_cands]
        beams_buf = beams_buf[: self.num_cands]

        return scores_buf, indices_buf, beams_buf, constraint_states
