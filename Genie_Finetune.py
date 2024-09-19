import argparse
import os
from transformers import set_seed
from diffusion_util.resample import create_named_schedule_sampler
from transformers import AutoTokenizer
import json
from util import logger
from train_util import dist_util
import torch
import torch.distributed as dist
from util.util import (
    create_model_and_diffusion,
    args_to_dict,
)
import collections
from data_util.s2s_data_util import load_s2s_data
from train_util.train_util import TrainLoop
from torch.serialization import default_restore_location

import os
import sys
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ['CUDA_VISIBLE_DEVICES'] ="5,6"
CheckpointState = collections.namedtuple("CheckpointState",
                                                     ['model_dict', 'optimizer_dict', 'scheduler_dict', 'offset'])


def get_arguments():
    parser = argparse.ArgumentParser()

    parser.add_argument('--checkpoint_path', type=str, default='/data1/hsy/Debatepedia/genie/datasets/duc/log', help='output path')

    
    parser.add_argument('--pretrain_model_path', type=str, default="/data1/hsy/Debatepedia/genie/pretrain_ckpt/GENIE_ckpt-XSum", help='using pretraining diffusion')

    # load model
    parser.add_argument('--model_arch', type=str, default='transformer', help='Core architecture of diffusion model')
    parser.add_argument('--model_channels', type=int, default=768, help='Try to set it to the same size as the model hidden')
    parser.add_argument('--in_channel', type=int, default=768, help='The input chanel size here must be the same as the word embedding size')
    parser.add_argument('--out_channel', type=int, default=768, help='The dimension size of the output is recommended to be the same as that of word embedding for easy reasoning')
    parser.add_argument('--dropout', type=float, default=0.1, help='')
    parser.add_argument("--learn_sigma", default=False, action="store_true", help="Whether to learning variance")
    parser.add_argument('--logits_mode', type=int, default=1, help='final logits mode of Diffusion model')
    parser.add_argument('--vocab_size', type=int, default=30522, help='vocab size')
    parser.add_argument('--config_name', type=str, default='bert-base-uncased', help='')
    parser.add_argument('--token_emb_type', type=str, default='random', help='token embedding type')
    parser.add_argument("--init_pretrained", default=False, action="store_true", help="Whether to using pretrain BERT encoder")
    parser.add_argument("--fix_encoder", default=False, action="store_true",
                        help="Whether to training encoder")


    # load diffusion
    parser.add_argument('--diffusion_steps', type=int, default=2000, help='Diffusion model maximum T')
    parser.add_argument('--use_kl', default=False, action="store_true", help="Whether to using kl loss in Diffsion loss")
    parser.add_argument('--training_mode', type=str, default='e2e', help='using e2e simple loss or e2e loss or s2s loss')
    parser.add_argument('--noise_schedule', type=str, default='sqrt', help='How to plan the noise change of Gaussian distribution')
    parser.add_argument('--predict_xstart', default=False, action="store_true", help="Model prediction target, if True, predict xstart, if False, predict EPSILON")
    parser.add_argument("--sigma_small", default=False, action="store_true", help="about learning variance")
    parser.add_argument("--rescale_learned_sigmas", default=True, action="store_false", help="about learning variance")
    parser.add_argument("--rescale_timesteps", default=True, action="store_false", help="about time rescale")

    # sample t
    parser.add_argument('--schedule_sampler', type=str, default='uniform', help='how to sample t per batch, uniform is Uniform sampling, loss-second-moment is Sampling according to loss')

    # data args
    parser.add_argument('--data_path', type=str, default='/data1/hsy/Debatepedia/genie/datasets/duc',help='data path')
    parser.add_argument('--data_name', type=str, default='xsum_data', help='data name')
    # for seq2seq
    parser.add_argument('--src_max_len', type=int, default=144, help='src max len')
    parser.add_argument('--tgt_max_len', type=int, default=32, help='tgt max len')
    parser.add_argument('--answer_max_len', type=int, default=10, help='tgt max len')
    # for doc2query
    parser.add_argument('--text_max_len', type=int, default=None, help='text max len')
    parser.add_argument('--pas_max_len', type=int, default=None, help='pas max len')

    # training args
    parser.add_argument('--train_type', type=str, default='LM_Diffusion', help='LM_Diffusion or S2S_Diffusion')
    parser.add_argument('--lr_anneal_steps', type=int, default=200000, help='total step')
    parser.add_argument('--batch_size', type=int, default=64, help='')
    parser.add_argument('--lr', type=float, default=1e-04, help='')
    parser.add_argument('--warmup_steps', type=int, default=20000, help='')
    parser.add_argument('--ema_rate', type=str, default='0.9999', help='ema training to stable model')
    parser.add_argument('--resume_checkpoint', type=str, default=None, help='')
    parser.add_argument('--eval_interval', type=int, default=2000, help='')
    parser.add_argument('--log_interval', type=int, default=100, help='')
    parser.add_argument('--save_interval', type=int, default=50000, help='')
    
    parser.add_argument('--weight_decay', type=str, default=0.0, help='')
    parser.add_argument('--gradient_clipping', type=float, default=-1., help='')
    parser.add_argument("--use_fp16", default=False, action="store_true", help="about learning variance")
    parser.add_argument('--fp16_scale_growth', type=float, default=1e-3, help='')

    # seed
    parser.add_argument('--seed', type=int, default=101, help='')

    # muti-gpu
    parser.add_argument("--local_rank", type=int, default=-1, help="For distributed training: local_rank")
    parser.add_argument("--server_ip", type=str, default="", help="For distant debugging.")
    parser.add_argument("--server_port", type=str, default="", help="For distant debugging.")

    args = parser.parse_args()
    return args

def setup_env(args):
    if args.local_rank == -1:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend="nccl")
        args.n_gpu = 1
    args.device = device

    # store args
    if args.local_rank != -1:
        args.world_size = torch.distributed.get_world_size()
        args.rank = dist.get_rank()

def load_states_from_checkpoint(model_file: str) -> CheckpointState:
    logger.info('Reading saved model from %s', model_file)
    state_dict = torch.load(model_file, map_location=lambda s, l: default_restore_location(s, 'cpu'))
    logger.info('model_state_dict keys %s', state_dict.keys())
    return CheckpointState(**state_dict)
def save(model):
    
    torch.save(model.module, "/data1/hsy/Debatepedia/bart_test-main/diffuseq_model/diffuseq2.pth")
    

def main():
    
    args = get_arguments()
    

    logger.log(f'saving the hyperparameters to {args.checkpoint_path}/training_args.json')
    with open(f'{args.checkpoint_path}/training_args.json', 'w') as f:
        json.dump(args.__dict__, f, indent=2)

    
    set_seed(args.seed)
    
    setup_env(args)
    
    log_path = os.path.join(args.checkpoint_path, 'log.txt')
    logger.configure(dir=log_path)

    model, diffusion = create_model_and_diffusion(
        args
    )
    
    if args.pretrain_model_path is not None:
        print("load model ckpt at :", args.pretrain_model_path)
        saved_state = load_states_from_checkpoint(args.pretrain_model_path)
        model.load_state_dict(saved_state.model_dict, strict=False)
    
    model.to(args.device)

    pytorch_total_params = sum(p.numel() for p in model.parameters())
    logger.log(f'the parameter count is {pytorch_total_params}')

    '''
    time step schedule sampler
    '''
    schedule_sampler = create_named_schedule_sampler(args.schedule_sampler, diffusion)

    '''
    tokenize
    '''
    logger.log("loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained('bert-base-uncased')

    '''
    for s2s
    '''
    # load data (train)
    train_data = load_s2s_data(
        args,
        split='train',
        padding_mode='max_len',
        tokenizer=tokenizer,
    )
    # load data (dev)
    dev_data = load_s2s_data(
        args,
        split='dev',
        padding_mode='max_len',
        tokenizer=tokenizer,
    )

    '''
    training
    '''
    logger.log("training Diffusion LM model...")
    TrainLoop(
        # training type
        train_type=args.train_type,
        # Training Core
        model=model,
        diffusion=diffusion,
        data=train_data,
        eval_data=dev_data,
        schedule_sampler=schedule_sampler,
        checkpoint_path=args.checkpoint_path,
        # Training Parameters
        batch_size=args.batch_size,
        lr=args.lr,
        ema_rate=args.ema_rate,
        weight_decay=args.weight_decay,
        lr_anneal_steps=args.lr_anneal_steps,
        gradient_clipping=args.gradient_clipping,
        # fp16
        use_fp16=args.use_fp16,
        fp16_scale_growth=args.fp16_scale_growth,
        # Training Log
        resume_checkpoint=args.resume_checkpoint,
        eval_interval=args.eval_interval,
        log_interval=args.log_interval,
        save_interval=args.save_interval,
        # device
        device=args.device,
        # finetune data name
        data_name=args.data_name
    ).run_loop()




if __name__ == "__main__":
    main()
