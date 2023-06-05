import os
import torch
import argparse
import warnings
import pytorch_lightning as pl
from pytorch_lightning import Trainer, strategies
import pytorch_lightning.callbacks as plc
from pytorch_lightning.loggers import CSVLogger
from model.smiles_captioning import SmilesCaptionLM
from data_provider.smiles_caption_dm import SmilesCaptionDM


os.environ['OPENBLAS_NUM_THREADS'] = '1'
## for pyg bug
warnings.filterwarnings('ignore', category=UserWarning, message='TypedStorage is deprecated')
## for A5000 gpus
torch.set_float32_matmul_precision('medium') # can be medium (bfloat16), high (tensorfloat32), highest (float32)


def main(args):
    pl.seed_everything(args.seed)
    # model
    if args.init_checkpoint:
        model = SmilesCaptionLM.load_from_checkpoint(args.init_checkpoint, strict=False, args=args)
        print(f"loaded init checkpoint from {args.init_checkpoint}")
    else:
        model = SmilesCaptionLM(args)

    print('total params:', sum(p.numel() for p in model.parameters()))
    tokenizer = model.tokenizer
    # data
    dm = SmilesCaptionDM(args.mode, args.num_workers, args.batch_size, args.root, args.text_max_len, tokenizer, args)
    
    callbacks = []
    ## fixme save only used parameters
    callbacks.append(plc.ModelCheckpoint(dirpath="all_checkpoints/"+args.filename+"/", 
                                         filename='{epoch:02d}', 
                                         every_n_epochs=10, 
                                         save_last=True, 
                                         save_top_k=-1))
    if len(args.devices.split(',')) > 1:
        strategy = strategies.DDPSpawnStrategy(find_unused_parameters=False)
    else:
        strategy = None
        args.devices = eval(args.devices)
    logger = CSVLogger(save_dir=f'./all_checkpoints/{args.filename}/')
    trainer = Trainer.from_argparse_args(args,
                                         callbacks=callbacks,
                                         strategy=strategy,
                                         logger=logger,
                                        #  limit_train_batches=20,
                                        #  limit_test_batches=100,
                                         )
    
    if args.mode in {'pretrain', 'ft'}:
        trainer.fit(model, datamodule=dm)
    elif args.mode == 'eval':
        trainer.fit_loop.epoch_progress.current.completed = args.caption_eval_epoch - 1
        trainer.validate(model, datamodule=dm)
    else:
        raise NotImplementedError()


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--filename', type=str, default="stage2_test")
    parser.add_argument('--seed', type=int, default=42, help='random seed')
    # MM settings
    parser.add_argument('--use_bn', action='store_true', default=False)
    parser.add_argument('--mode', type=str, default='pretrain')
    parser = Trainer.add_argparse_args(parser)
    parser = SmilesCaptionLM.add_model_specific_args(parser)  # add model args
    parser = SmilesCaptionDM.add_model_specific_args(parser)
    parser.set_defaults(accelerator='gpu',
                        devices='0,1,2,3',
                        precision=16,
                        max_epochs=10,
                        accumulate_grad_batches=1,
                        check_val_every_n_epoch=1)
    args = parser.parse_args()

    print("=========================================")
    for k, v in sorted(vars(args).items()):
        print(k, '=', v)
    print("=========================================")
    return args

if __name__ == '__main__':
    main(get_args())
