import argparse
import os
import numpy as np
import torch
import torch.nn.functional as F

from vocab import Vocab
from model import LM
from utils import *
from batchify import get_batches
from train import evaluate

parser = argparse.ArgumentParser()
parser.add_argument('--checkpoint', metavar='DIR', required=True,
                    help='checkpoint directory')
parser.add_argument('--model', default='model_best.pt', metavar='FILE',
                    help='model file (in checkpoint directory)')
parser.add_argument('--vocab', default='vocab.txt', metavar='FILE',
                    help='vocab file (in checkpoint directory)')
parser.add_argument('--output', default='output.txt', metavar='FILE',
                    help='output file (in checkpoint directory)')

parser.add_argument('--eval', default='', metavar='FILE',
                    help='data file to evaluate')
parser.add_argument('--sample', type=int, default=0, metavar='N',
                    help='num of sentences to generate')
parser.add_argument('--fill', default='', metavar='FILE',
                    help='input file to fill')

parser.add_argument('--decode', default='greedy', metavar='M',
                    choices=['greedy', 'sample'],
                    help='greedy decoding or sampling')
parser.add_argument('--write_mid', action='store_true',
                    help='write intermediate partial sentences')

parser.add_argument('--batch_size', type=int, default=64, metavar='N',
                    help='batch size')
parser.add_argument('--seed', type=int, default=1111, metavar='N',
                    help='random seed')
parser.add_argument('--no_cuda', action='store_true',
                    help='disable CUDA')
args = parser.parse_args()

def select(logits):
    if args.decode == 'sample':
        return torch.multinomial(logits.exp(), num_samples=1)[0]
    else:
        return logits.argmax()

def generate(seq):
    seq = torch.LongTensor(seq).to(device)
    blanks = [i for i, w in enumerate(seq) if w == vocab.blank]
    sent_mid = [[vocab.idx2word[id] for id in seq]]
    while len(blanks) > 0 and len(seq) <= model.args.max_len:
        output = model(seq.unsqueeze(0), blanks)[0]
        loc = select(model.loc(output).squeeze(-1))
        output_loc = output[loc]

        logits_word = model.word(output_loc) * model.x_logit_scale
        lprob_word = F.log_softmax(logits_word, -1)
        output_loc_word = torch.cat((output_loc.unsqueeze(0).expand(vocab.size, -1),
            model.G.src_word_emb.weight), -1)
        logits_lrb = model.lrb(output_loc_word)
        lprob_lrb = F.log_softmax(logits_lrb, -1)
        lprob_word_lrb = lprob_word.unsqueeze(1) + lprob_lrb
        word_lrb = select(lprob_word_lrb.view(-1))
        word, lrb = word_lrb / 4, word_lrb % 4

        #word = select(model.word(output_loc) * model.x_logit_scale)
        #output_loc_word = torch.cat((output_loc, model.G.src_word_emb(word)), dim=-1)
        #lrb = select(model.lrb(output_loc_word))
        lb, rb = lrb / 2, lrb % 2

        ins = ([vocab.blank] if lb else []) + [word] + ([vocab.blank] if rb else [])
        ins = torch.LongTensor(ins).to(device)
        pos = blanks[loc]
        seq = torch.cat((seq[:pos], ins, seq[pos+1:]))
        blanks = [i for i, w in enumerate(seq) if w == vocab.blank]
        sent_mid.append([vocab.idx2word[id] for id in seq])
    return sent_mid

if __name__ == '__main__':
    set_seed(args.seed)
    vocab = Vocab(os.path.join(args.checkpoint, args.vocab))
    cuda = not args.no_cuda and torch.cuda.is_available()
    device = torch.device("cuda" if cuda else "cpu")
    model = get_model(os.path.join(args.checkpoint, args.model), vocab, device)
    out_path = os.path.join(args.checkpoint, args.output)

    if args.eval:
        sents = load_sent(args.eval, model.args.max_len, model.args.multisent)
        batches, _ = get_batches(sents, vocab, args.batch_size)
        meters = evaluate(model, device, batches)
        print(' '.join(['{} {:.2f},'.format(k, meter.avg)
            for k, meter in meters.items()]))

    if args.sample:
        sents = [generate([vocab.blank]) for _ in range(args.sample)]
        write_mid_or_last(sents, args.write_mid, out_path)

    if args.fill:
        sents = load_sent(args.fill)
        sents = [[vocab.word2idx[w] if w in vocab.word2idx else vocab.unk
            for w in s] for s in sents]
        sents = [generate(s) for s in sents]
        write_mid_or_last(sents, args.write_mid, out_path)
