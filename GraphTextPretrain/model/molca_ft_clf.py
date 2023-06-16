import os
from typing import Any, Dict
import torch
# from model.blip2_opt import Blip2OPT
from model.molca_opt_clf import MolCAOPTClf
import pytorch_lightning as pl
from torch import optim
from lavis.common.optims import LinearWarmupCosineLRScheduler, LinearWarmupStepLRScheduler
import numpy as np
from sklearn.metrics import roc_auc_score
from peft import LoraConfig, TaskType


def eval_multi_label(y_true, y_scores):
    y_true = y_true.numpy()
    y_scores = y_scores.numpy()
    roc_list = []
    for i in range(y_true.shape[1]):
        # AUC is only defined when there is at least one positive data.
        if np.sum(y_true[:, i] == 1) > 0 and np.sum(y_true[:, i] == -1) > 0:
            is_valid = y_true[:, i]**2 > 0
            roc_list.append(roc_auc_score(
                (y_true[is_valid, i] + 1)/2, y_scores[is_valid, i]))

    if len(roc_list) < y_true.shape[1]:
        print("Some target is missing!")
        print("Missing ratio: %f" % (1 - float(len(roc_list))/y_true.shape[1]))
    mean_roc = sum(roc_list) / len(roc_list)
    return mean_roc


class AttrDict(dict):
    def __init__(self, *args, **kwargs):
        super(AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self

def load_ignore_unexpected(model, state_dict):
    keys = set(model.state_dict().keys())
    state_dict = {k: v for k, v in state_dict.items() if k in keys}
    
    ## try to print keys that are not included
    model.load_state_dict(state_dict, strict=True)


def get_module_state_dict(state_dict, module_name):
    module_state_dict = {}
    for key, value in state_dict.items():
        if key.startswith(module_name):
            key = key[len(module_name) + 1:]
            if key == '':
                return value
            module_state_dict[key] = value
    return module_state_dict

class MolCAClf(pl.LightningModule):
    def on_save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        if self.llm_tune != 'full':
            to_be_removed = []
            for key in checkpoint['state_dict']:
                if key.startswith('blip2opt.opt_model') or key.startswith('blip2opt.llm_model'):
                    to_be_removed.append(key)
            for key in to_be_removed:
                checkpoint['state_dict'].pop(key)
        if self.llm_tune == 'lora' and (self.current_epoch + 1) % 10 == 0:
            if self.local_rank == 0: # manually fix a bug in peft module
                if self.args.peft_config:
                    peft_config = LoraConfig(**LoraConfig.from_json_file(self.args.peft_config))
                else:
                    peft_config = LoraConfig(task_type=TaskType.CAUSAL_LM, inference_mode=False, r=self.args.lora_r, lora_alpha=self.args.lora_alpha, lora_dropout=self.args.lora_dropout)
                if hasattr(self.blip2opt, 'opt_model'):
                    self.blip2opt.opt_model.peft_config['default'] = peft_config
                    self.blip2opt.opt_model.save_pretrained(os.path.join(self.logger.save_dir, f'lora_epoch_{self.current_epoch}'))
                elif hasattr(self.blip2opt, 'llm_model'):
                    self.blip2opt.llm_model.peft_config['default'] = peft_config
                    self.blip2opt.llm_model.save_pretrained(os.path.join(self.logger.save_dir, f'lora_epoch_{self.current_epoch}'))
        return super().on_save_checkpoint(checkpoint)
    
    def __init__(self, args):
        super().__init__()
        if isinstance(args, dict):
            args = AttrDict(**args)

        self.args = args
        if not hasattr(args, 'do_sample'):
            args.do_sample = False
        self.caption_eval_epoch = args.caption_eval_epoch
        self.do_sample = args.do_sample
        self.num_beams = args.num_beams
        self.max_len = args.max_len
        self.min_len = args.min_len
        self.reaction_weight = args.reaction_weight
        self.llm_tune = args.llm_tune
        
        self.blip2opt = MolCAOPTClf(args.bert_name, args.gin_num_layers, args.gin_hidden_dim, args.drop_ratio, args.tune_gnn, args.num_query_token, args.cross_attention_freq, args.use_bn, args.llm_tune, args.peft_dir, args.opt_model, args.prompt, args)
        self.tokenizer = self.blip2opt.init_tokenizer()
        self.save_hyperparameters(args)

    
    def configure_optimizers(self):
        optimizer = optim.AdamW(self.parameters(), lr=self.args.init_lr, weight_decay=self.args.weight_decay)
        # warmup_steps = min(self.args.warmup_steps, len(self.train_dataloader))
        if self.args.scheduler == 'linear_warmup_cosine_lr':
            self.scheduler = LinearWarmupCosineLRScheduler(optimizer, self.args.max_epochs, self.args.min_lr, self.args.init_lr, self.args.warmup_steps, self.args.warmup_lr)
        elif self.args.scheduler == 'linear_warmup_step_lr':
            self.scheduler = LinearWarmupStepLRScheduler(optimizer, self.args.max_epochs, self.args.min_lr, self.args.init_lr, self.args.lr_decay_rate, self.args.warmup_lr, self.args.warmup_steps)
        elif self.args.scheduler == 'None':
            self.scheduler = None
        else:
            raise NotImplementedError()
        return optimizer
    
    @torch.no_grad()
    def validation_step(self, batch, batch_idx, dataloader_idx):
        if self.args.root.find('MoleculeNet') >= 0 and self.current_epoch == 0:
            return
        if dataloader_idx == 0:
            logits = self.blip2opt(batch, mode='inference')
            return (logits.cpu(), batch[0].y.cpu())
        elif dataloader_idx == 1:
            logits = self.blip2opt(batch, mode='inference')
            return (logits.cpu(), batch[0].y.cpu())
        else:
            raise NotImplementedError

    def validation_epoch_end(self, outputs):
        if self.args.root.find('MoleculeNet') >= 0 and self.current_epoch == 0:
            return
        assert self.trainer.world_size == 1
        val_output = outputs[0]
        test_output = outputs[1]
        val_logits, val_labels = zip(*val_output)
        test_logits, test_labels = zip(*test_output)
        val_logits = torch.cat(val_logits, dim=0)
        val_labels = torch.cat(val_labels, dim=0)
        test_logits = torch.cat(test_logits, dim=0)
        test_labels = torch.cat(test_labels, dim=0)

        val_roc = eval_multi_label(val_labels, val_logits)
        test_roc = eval_multi_label(test_labels, test_logits)
        self.log("val roc", float(val_roc))
        self.log("test roc", float(test_roc))

    def training_step(self, batch, batch_idx):
        if self.scheduler:
            self.scheduler.step(self.trainer.current_epoch, self.trainer.global_step)
        
        batch_size = batch[0].y.shape[0]
        ###============== Overall Loss ===================###
        loss = self.blip2opt(batch)
        self.log("train loss", float(loss['loss']), batch_size=batch_size, sync_dist=True)
        self.log("lr", self.trainer.optimizers[0].param_groups[0]['lr'], batch_size=batch_size, sync_dist=True)
        return loss['loss']

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = parent_parser.add_argument_group("GINSimclr")
        # train mode
        # GIN
        parser.add_argument('--gin_hidden_dim', type=int, default=300)
        parser.add_argument('--gin_num_layers', type=int, default=5)
        parser.add_argument('--drop_ratio', type=float, default=0.0)
        parser.add_argument('--tune_gnn', action='store_true', default=False)
        # Bert
        parser.add_argument('--bert_hidden_dim', type=int, default=768, help='')
        parser.add_argument('--bert_name', type=str, default='scibert')
        parser.add_argument('--cross_attention_freq', type=int, default=2)
        parser.add_argument('--num_query_token', type=int, default=8)
        # OPT
        parser.add_argument('--opt_model', type=str, default="facebook/galactica-1.3b")
        # parser.add_argument('--prompt', type=str, default='a molecule of ')
        parser.add_argument('--num_beams', type=int, default=5)
        parser.add_argument('--do_sample', action='store_true', default=False)
        parser.add_argument('--max_len', type=int, default=256)
        parser.add_argument('--min_len', type=int, default=8)
        parser.add_argument('--llm_tune', type=str, default='freeze')
        parser.add_argument('--peft_config', type=str, default=None)
        parser.add_argument('--peft_dir', type=str, default='')

        ## quantization
        parser.add_argument('--load_in_8bit', action='store_true', default=False)

        ## lora config
        parser.add_argument('--lora_r', type=int, default=8)
        parser.add_argument('--lora_alpha', type=int, default=32)
        parser.add_argument('--lora_dropout', type=int, default=0.1)

        # optimization
        parser.add_argument('--reaction_weight', type=float, default=1.0)
        parser.add_argument('--weight_decay', type=float, default=0.05, help='optimizer weight decay')
        parser.add_argument('--init_lr', type=float, default=1e-4, help='optimizer init learning rate')
        parser.add_argument('--min_lr', type=float, default=1e-5, help='optimizer min learning rate')
        parser.add_argument('--warmup_lr', type=float, default=1e-6, help='optimizer warmup learning rate')
        parser.add_argument('--warmup_steps', type=int, default=1000, help='optimizer warmup steps')
        parser.add_argument('--lr_decay_rate', type=float, default=0.9, help='optimizer lr decay rate')
        parser.add_argument('--scheduler', type=str, default='linear_warmup_cosine_lr', help='type of scheduler') # or linear_warmup_step_lr
        parser.add_argument('--stage1_path', type=str, default='')
        parser.add_argument('--stage2_path', type=str, default='')
        parser.add_argument('--init_checkpoint', type=str, default='')
        parser.add_argument('--caption_eval_epoch', type=int, default=10)
        return parent_parser

