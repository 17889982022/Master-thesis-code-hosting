''' This module will handle the text generation with beam search. '''

import torch
from source.utils.Beam import Beam


class TopKGenerator(object):
    ''' Load with trained model and handle the beam search '''

    def __init__(self, model, src_field, tgt_field, max_length, beam_size, n_best, use_gpu, ):
        self.device = torch.device('cuda' if use_gpu else 'cpu')
        self.beam_size = beam_size
        self.max_token_seq_len = max_length
        self.n_best = n_best
        self.src_field = src_field
        self.tgt_field = tgt_field
        self.n_best = 1
        self.PAD = tgt_field.stoi[tgt_field.pad_token]
        self.UNK = tgt_field.stoi[tgt_field.unk_token]
        self.BOS = tgt_field.stoi[tgt_field.bos_token]
        self.EOS = tgt_field.stoi[tgt_field.eos_token]
        self.model = model.cuda() if use_gpu else model
        self.model.eval()

    def translate_batch(self, inputs):
        ''' Translation work in one batch '''

        def get_inst_idx_to_tensor_position_map(inst_idx_list):
            ''' Indicate the position of an instance in a tensor. '''
            return {inst_idx: tensor_position for tensor_position, inst_idx in enumerate(inst_idx_list)}

        def collect_active_part(beamed_tensor, curr_active_inst_idx, n_prev_active_inst, n_bm):
            ''' Collect tensor parts associated to active instances. '''

            _, *d_hs = beamed_tensor.size()
            n_curr_active_inst = len(curr_active_inst_idx)
            new_shape = (n_curr_active_inst * n_bm, *d_hs)

            beamed_tensor = beamed_tensor.view(n_prev_active_inst, -1)
            beamed_tensor = beamed_tensor.index_select(0, curr_active_inst_idx)
            beamed_tensor = beamed_tensor.view(*new_shape)

            return beamed_tensor

        def collate_active_info(dec_state, inst_idx_to_position_map, active_inst_idx_list):
            # Sentences which are still active are collected,
            # so the decoder will not run on completed sentences.
            n_prev_active_inst = len(inst_idx_to_position_map)
            active_inst_idx = [inst_idx_to_position_map[k] for k in active_inst_idx_list]
            active_inst_idx = torch.LongTensor(active_inst_idx).to(self.device)
            active_dec_state = dec_state.collect_active_part(active_inst_idx, n_prev_active_inst, n_bm)
            active_inst_idx_to_position_map = get_inst_idx_to_tensor_position_map(active_inst_idx_list)

            return active_dec_state, active_inst_idx_to_position_map

        def beam_decode_step(
                inst_dec_beams, len_dec_seq, dec_state, inst_idx_to_position_map, n_bm):
            ''' Decode and update beam status, and then return active beam idx '''

            def prepare_beam_dec_seq(inst_dec_beams, len_dec_seq):
                dec_partial_seq = [b.get_current_state() for b in inst_dec_beams if not b.done]
                dec_partial_seq = torch.stack(dec_partial_seq).to(self.device)
                dec_partial_seq = dec_partial_seq.view(-1, len_dec_seq)
                return dec_partial_seq

            def prepare_beam_dec_pos(len_dec_seq, n_active_inst, n_bm):
                dec_partial_pos = torch.arange(1, len_dec_seq + 1, dtype=torch.long, device=self.device)
                dec_partial_pos = dec_partial_pos.unsqueeze(0).repeat(n_active_inst * n_bm, 1)
                return dec_partial_pos

            def predict_word(dec_seq, dec_pos, dec_state, n_active_inst, n_bm):
                word_prob, *_ = self.model.decode((dec_seq, dec_pos), dec_state)
                word_prob = word_prob[:, -1, :]  # Pick the last step: (bh * bm) * d_h
                word_prob = word_prob.view(n_active_inst, n_bm, -1)

                return word_prob

            def collect_active_inst_idx_list(inst_beams, word_prob, inst_idx_to_position_map):
                active_inst_idx_list = []
                for inst_idx, inst_position in inst_idx_to_position_map.items():
                    is_inst_complete = inst_beams[inst_idx].advance(word_prob[inst_position])
                    if not is_inst_complete:
                        active_inst_idx_list += [inst_idx]

                return active_inst_idx_list

            n_active_inst = len(inst_idx_to_position_map)

            dec_seq = prepare_beam_dec_seq(inst_dec_beams, len_dec_seq)
            dec_pos = prepare_beam_dec_pos(len_dec_seq, n_active_inst, n_bm)
            word_prob = predict_word(dec_seq, dec_pos, dec_state, n_active_inst, n_bm)

            # Update the beam with predicted word prob information and collect incomplete instances
            active_inst_idx_list = collect_active_inst_idx_list(
                inst_dec_beams, word_prob, inst_idx_to_position_map)

            return active_inst_idx_list

        def collect_hypothesis_and_scores(inst_dec_beams, n_best):
            all_hyp, all_scores = [], []
            for inst_idx in range(len(inst_dec_beams)):
                scores, tail_idxs = inst_dec_beams[inst_idx].sort_scores()
                all_scores += [scores[:n_best]]

                hyps = [inst_dec_beams[inst_idx].get_hypothesis(i) for i in tail_idxs[:n_best]]
                all_hyp += [hyps]
            return all_hyp, all_scores

        with torch.no_grad():
            # -- Encode
            outputs, dec_init_state = self.model.encode(inputs)

            # -- Repeat data for beam search
            n_bm = self.beam_size
            n_inst, len_s, d_h = dec_init_state.context_memory.size()
            dec_state = dec_init_state.inflate(n_bm)
            # src_enc = src_enc.repeat(1, n_bm, 1).view(n_inst * n_bm, len_s, d_h)

            # -- Prepare beams
            inst_dec_beams = [Beam(PAD=self.PAD, BOS=self.BOS, EOS=self.EOS, size=n_bm, device=self.device) for _ in
                              range(n_inst)]

            # -- Bookkeeping for active or not
            active_inst_idx_list = list(range(n_inst))
            inst_idx_to_position_map = get_inst_idx_to_tensor_position_map(active_inst_idx_list)

            # -- Decode
            for len_dec_seq in range(1, self.max_token_seq_len + 1):

                active_inst_idx_list = beam_decode_step(
                    inst_dec_beams, len_dec_seq, dec_state, inst_idx_to_position_map, n_bm)

                if not active_inst_idx_list:
                    break  # all instances have finished their path to <EOS>

                dec_state, inst_idx_to_position_map = collate_active_info(
                    dec_state, inst_idx_to_position_map, active_inst_idx_list)

        batch_hyp, batch_scores = collect_hypothesis_and_scores(inst_dec_beams, self.n_best)

        return outputs, batch_hyp, batch_scores

    def generate(self, batch_iter, num_batches=None):
        results = []
        batch_cnt = 0

        for batch in batch_iter:
            enc_outputs, preds, scores = self.translate_batch(inputs=batch)
            # denumericalization
            src = batch.src[0]
            tgt = batch.tgt[0]
            src = self.src_field.denumericalize(src)
            tgt = self.tgt_field.denumericalize(tgt)
            preds = self.tgt_field.denumericalize(preds)
            # scores = scores.tolist()

            if 'cue' in batch:
                cue = self.tgt_field.denumericalize(batch.cue[0].data)
                enc_outputs.add(cue=cue)

            enc_outputs.add(src=src, tgt=tgt, preds=preds, scores=scores)
            result_batch = enc_outputs.flatten()
            results += result_batch
            batch_cnt += 1
            if batch_cnt == num_batches:
                break
        return results

