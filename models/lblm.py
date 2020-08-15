import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl
import argparse
import math

from transformer.Models import Encoder
from utils import get_known_length_canvas, sample_permutation, seq_cross_entropy, to_tensor, collect, batch_randint


class LBLM(pl.LightningModule):
    """Length-aware Blank Language Model"""

    def __init__(self, vocab, hparams):
        super().__init__()
        self.vocab = vocab
        self.hparams = hparams

        self.G = Encoder(
            n_src_vocab=vocab.size, len_max_seq=hparams.max_len,
            d_word_vec=hparams.d_model, d_model=hparams.d_model, d_inner=hparams.d_inner_hid,
            n_layers=hparams.n_layers, n_head=hparams.n_head, d_k=hparams.d_k, d_v=hparams.d_v,
            dropout=hparams.dropout)

        self.word = nn.Linear(hparams.d_model, vocab.size, bias=False)
        nn.init.xavier_normal_(self.word.weight)
        self.x_logit_scale = 1.
        if hparams.share_emb_prj_weight:
            self.word.weight = self.G.src_word_emb.weight
            self.x_logit_scale = (hparams.d_model ** -0.5)

        self.loc = nn.Linear(hparams.d_model, 1)
        self.lrb = nn.Sequential(
            nn.Linear(hparams.d_model * 2, hparams.d_model * 2),
            nn.ReLU(),
            nn.Linear(hparams.d_model * 2, hparams.max_len)
        )


    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = argparse.ArgumentParser(parents=[parent_parser], add_help=False)
        # Model args
        parser.add_argument('--d_model', type=int, default=512, metavar='N',
                            help='transformer dimension d_model')
        parser.add_argument('--d_inner_hid', type=int, default=2048, metavar='N',
                            help='transformer dimension d_inner_hid')
        parser.add_argument('--d_k', type=int, default=64, metavar='N',
                            help='transformer dimension d_k')
        parser.add_argument('--d_v', type=int, default=64, metavar='N',
                            help='transformer dimension d_v')
        parser.add_argument('--n_head', type=int, default=8, metavar='N',
                            help='number of attention heads')
        parser.add_argument('--n_layers', type=int, default=6, metavar='N',
                            help='number of layers')

        parser.add_argument('--share_emb_prj_weight', action='store_true',
                            help='share word embedding and projection weights')
        # LBLM
        parser.add_argument('--lrb_only', action='store_true',
                        help='LRB only')
        parser.add_argument('--max_blank_length', default=2000,
                        help='Max lengths of blanks')

        # Optim
        parser.add_argument('--adam_betas', default='(0.9, 0.999)', metavar='(R, R)',
                            help='adam betas')
        parser.add_argument('--adam_eps', type=float, default=1e-8, metavar='R',
                            help='adam eps')
        parser.add_argument('--weight_decay', type=float, default=1e-5, metavar='R',
                            help='weight decay')
        parser.add_argument('--dropout', type=float, default=0.3, metavar='P',
                            help='dropout probability (0 = no dropout)')


        # Eval
        parser.add_argument('--n_mc', type=int, default=1, metavar='N',
                        help='num of samples for monte carlo estimate of ppl')


        # Data
        parser.add_argument('--max_tok', type=int, default=10000, metavar='N',
                            help='max number of tokens per batch')
        parser.add_argument('--eval_max_tok', type=int, default=40000, metavar='N',
                            help='max number of tokens per batch for evaluation')
        parser.add_argument('--vocab_size', type=int, default=10000, metavar='N',
                            help='keep N most frequent words in vocabulary')
        parser.add_argument('--max_len', type=int, default=512, metavar='N',
                            help='max sequence length')
        parser.add_argument('--cat_sent', action='store_true',
                            help='concat sentences and then chunk into size of max_len')
        parser.add_argument('--add_eos', action='store_true',
                            help='add <eos> at the end of each sentence')

        # LR schedule
        parser.add_argument('--lr_schedule', default='fixed', metavar='S',
                            choices=['fixed', 'triangular'],
                            help='learning rate schedule')
        parser.add_argument('--lr', type=float, default=0.0001, metavar='R',
                            help='learning rate')
        parser.add_argument('--warmup_steps', type=int, default=4000, metavar='N',
                            help='number of warmup steps (inverse_sqrt)')
        parser.add_argument('--train_steps', type=int, default=300000, metavar='N',
                            help='number of training steps')

        #  parser.add_argument('--lr_decay', type=float, default=4, metavar='R',
                            #  help='learning rate decay factor (reduce_on_plateau)')

        return parser

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(
            self.parameters(),
            betas=eval(self.hparams.adam_betas),
            eps=self.hparams.adam_eps,
            weight_decay=self.hparams.weight_decay,
            lr=self.hparams.lr
        )

        if self.hparams.lr_schedule == "fixed":
            return optimizer

        if self.hparams.lr_schedule == "triangular":
            scheduler = torch.optim.lr_scheduler.CyclicLR(
                optimizer,
                base_lr=0,
                max_lr=self.hparams.lr,
                step_size_up=self.hparams.warmup_steps,
                step_size_down=(self.hparams.train_steps - self.hparams.warmup_steps),
                cycle_momentum=False,
            )

            return [optimizer], [{'scheduler': scheduler, 'interval': 'step'}]

    def training_step(self, batch, batch_idx):
        seq, n, n_real = map(lambda x: x.squeeze(0), batch)
        losses = self("losses", seq, n, n_real)
        losses['log'] = {**losses}
        return losses

    def validation_step(self, batch, batch_idx):
        seq, n, n_real = map(lambda x: x.squeeze(0), batch)
        if seq.size(1) == 0:
            raise ValueError
        # This is not actually compute n_mc, but uses the args.n_mc argument to compute
        # an average of the loss across multiple k / sigma
        nlls = []
        print("n_mc: {}:".format(self.hparams.n_mc))
        for _ in range(max(1, self.hparams.n_mc)):
            losses = self("losses", seq, n, n_real)
            nll = (losses['loss'] * n_real.sum())
            nlls.append(nll)
        nll = torch.tensor(nlls).mean()
        n_words = n_real.sum()
        # Todo use AverageMeters

        return {**losses, 'n_words': n_words, 'nll': nll}

    def validation_epoch_end(self, outputs):
        logs = {}
        for key in outputs[0].keys():
            if key not in ["n_words", "nll"]:
                logs['val_' + key] = torch.stack([x[key] for x in outputs]).mean()
        total_nll = torch.stack([x['nll'] for x in outputs]).sum()
        n_words = torch.stack([x['n_words'] for x in outputs]).sum()
        ppl = torch.exp(total_nll / n_words)
        logs['total_nll'] = total_nll
        logs['n_words'] = n_words
        logs['ppl'] = ppl
        return {'val_loss': logs['val_loss'], 'log': logs}

    def test_step(self, batch, batch_idx):
        return self.validation_step(batch, batch_idx)

    def test_epoch_end(self, outputs):
        logs = {}
        for key in outputs[0].keys():
            if key not in ["n_words", "nll"]:
                logs['test_' + key] = torch.stack([x[key] for x in outputs]).mean()
        total_nll = torch.stack([x['nll'] for x in outputs]).sum()
        n_words = torch.stack([x['n_words'] for x in outputs]).sum()
        ppl = torch.exp(total_nll / n_words)
        logs['total_nll'] = total_nll
        logs['n_words'] = n_words
        logs['ppl'] = ppl
        self.logger.log_metrics(logs)
        return {'test_loss': logs['test_loss'], 'log': logs}

    def forward_encoder(self, canvas):
        pos = (1 + torch.arange(canvas.size(1))).repeat(len(canvas), 1)
        pos[canvas == self.vocab.pad] = 0
        output, *_ = self.G(canvas, pos.to(canvas.device))
        return output

    def forward(self, action, *args):
        if action == "nll_mc":
            return self.nll_mc(*args)
        elif action == "losses":
            return self.losses(*args)
        raise NotImplementedError


    def get_loss(self, seq, canvas, blanks, rest, loc, lb):
        count = (rest != -1).sum(1)
        output = self.forward_encoder(canvas)
        output_blank = collect(output, blanks)

        logits_loc = self.loc(output_blank).squeeze(-1)
        logits_loc[blanks == -1] = float('-inf')
        nll_loc = -F.log_softmax(logits_loc, 1)
        loss_loc = collect(nll_loc, loc)
        loss_loc = loss_loc.sum(1) / count.float()
        output_loc = collect(output_blank, loc)

        logits_word = self.word(output_loc) * self.x_logit_scale
        target = collect(seq, rest, self.vocab.pad)
        loss_word = seq_cross_entropy(logits_word, target, self.vocab.pad)
        loss_word = loss_word.sum(1) / count.float()
        output_word = torch.cat((output_loc, self.G.src_word_emb(target)), -1)


        logits_lrb = self.lrb(output_word)

        # Mask out illegal blank options
        blank_0 = self.vocab.word2idx['<blank_0>']
        length = collect(canvas, blanks) - blank_0
        length_loc = collect(length, loc, -1)
        bs, seq_len = length_loc.shape
        ta = length_loc.unsqueeze(-1).repeat(1, 1, self.hparams.max_len)
        ra = torch.arange(self.hparams.max_len).unsqueeze(0).unsqueeze(0).repeat(bs, seq_len, 1).to(ta.device)
        mask = (ra >= ta)
        logits_lrb.masked_fill_(mask, float('-inf'))

        loss_lrb = seq_cross_entropy(logits_lrb, lb, -1)
        loss_lrb = loss_lrb.sum(1) / count.float()

        return loss_loc, loss_word, loss_lrb

    def losses(self, seq, n, n_real):
        m = (seq == self.vocab.missing).sum(1)
        # m = torch.max(m, n - 10)
        # k = m + (torch.rand_like(n.float()) * (n - m).float()).long() # sample k from m to n-1
        k = batch_randint(m, n - 1)
        rank = sample_permutation(seq, self.vocab)
        keep = (rank < k.unsqueeze(1))
        canvas, blanks, rest, loc, lb = get_known_length_canvas(seq, keep, n, self.vocab)
        loss_loc, loss_word, loss_lrb = self.get_loss(seq, canvas, blanks, rest, loc, lb)
        nll_lb = (loss_loc + loss_word + loss_lrb) * (n - m).float() - (n - m + 1).float().lgamma()
        return {'loss' : nll_lb.sum() / n_real.sum(),
                'loc'  : loss_loc.mean(),
                'word' : loss_word.mean(),
                'lrb'  : loss_lrb.mean()
               }

    def nll_mc(self, seq, n, n_mc):
        """
        Compute negative log-likelihood by monte carlo estimate
        Args:
            n_mc: number of samples to take

        Note: sentences in the batch must have the same length
        """
        raise NotImplementedError
