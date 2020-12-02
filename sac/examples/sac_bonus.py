from gym.envs.mujoco import HalfCheetahEnv

import rlkit.torch.pytorch_util as ptu
from rlkit.data_management.env_replay_buffer import EnvReplayBuffer
from rlkit.envs.wrappers import NormalizedBoxEnv
from rlkit.launchers.launcher_util import setup_logger
from rlkit.samplers.data_collector import MdpPathCollector, CustomMDPPathCollector
from rlkit.torch.sac.policies import TanhGaussianPolicy, MakeDeterministic
from rlkit.torch.sac.sac import SACTrainer
from rlkit.torch.sac.sac_rnd import SAC_RNDTrainer
from rlkit.torch.sac.sac_rnd_kl import SAC_RNDTrainerKL
from rlkit.torch.sac.sac_cls import SAC_BonusTrainer

from rlkit.torch.networks import FlattenMlp
from rlkit.torch.networks import Mlp

from rlkit.torch.torch_rl_algorithm import TorchBatchRLAlgorithm

from torch.nn import functional as F

import h5py
import argparse
import os
import gym
import d4rl
import numpy as np
import torch
import time
import _pickle as cPickle


def load_hdf5(dataset, replay_buffer, max_size):
    all_obs = dataset['observations']
    all_act = dataset['actions']
    N = min(all_obs.shape[0], max_size)

    _obs = all_obs[:N - 1]
    _actions = all_act[:N - 1]
    _next_obs = all_obs[1:]
    _rew = np.squeeze(dataset['rewards'][:N - 1])
    _rew = np.expand_dims(np.squeeze(_rew), axis=-1)
    _done = np.squeeze(dataset['terminals'][:N - 1])
    _done = (np.expand_dims(np.squeeze(_done), axis=-1)).astype(np.int32)

    max_length = 1000
    ctr = 0
    # Only for MuJoCo environments
    # Handle the condition when terminal is not True and trajectory ends due to a timeout
    for idx in range(_obs.shape[0]):
        if ctr >= max_length - 1:
            ctr = 0
        else:
            replay_buffer.add_sample_only(
                _obs[idx], _actions[idx], _rew[idx], _next_obs[idx], _done[idx])
            ctr += 1
            if _done[idx][0]:
                ctr = 0
    ###

    print (replay_buffer._size, replay_buffer._terminals.shape)


def experiment(variant):
    eval_env = gym.make(variant['env_name'])
    expl_env = eval_env
    obs_dim = expl_env.observation_space.low.size
    action_dim = eval_env.action_space.low.size

    M = variant['layer_size']
    # q and policy netwroks
    qf1 = FlattenMlp(
        input_size=obs_dim + action_dim,
        output_size=1,
        hidden_sizes=[M, M],
    ).to(ptu.device)
    qf2 = FlattenMlp(
        input_size=obs_dim + action_dim,
        output_size=1,
        hidden_sizes=[M, M],
    ).to(ptu.device)
    target_qf1 = FlattenMlp(
        input_size=obs_dim + action_dim,
        output_size=1,
        hidden_sizes=[M, M],
    ).to(ptu.device)
    target_qf2 = FlattenMlp(
        input_size=obs_dim + action_dim,
        output_size=1,
        hidden_sizes=[M, M],
    ).to(ptu.device)
    policy = TanhGaussianPolicy(
        obs_dim=obs_dim,
        action_dim=action_dim,
        hidden_sizes=[M, M],
    ).to(ptu.device)

    # if bonus: define bonus networks
    if variant['bonus']:
        bonus_network = Mlp(
            input_size=obs_dim + action_dim,
            output_size=1,
            hidden_sizes=[64, 64],
            output_activation=F.sigmoid,
        ).to(ptu.device)

        checkpoint = torch.load(variant['bonus_path'], map_location=map_location)
        bonus_network.load_state_dict(checkpoint['network_state_dict'])
        print('Loading bonus model: {}'.format(variant['bonus_path']))

    eval_policy = MakeDeterministic(policy)
    eval_path_collector = CustomMDPPathCollector(
        eval_env,
    )
    expl_path_collector = MdpPathCollector(
        expl_env,
        policy,
    )
    buffer_filename = None
    if variant['buffer_filename'] is not None:
        buffer_filename = variant['buffer_filename']

    replay_buffer = EnvReplayBuffer(
        variant['replay_buffer_size'],
        expl_env,
    )

    dataset = eval_env.unwrapped.get_dataset()

    load_hdf5(dataset, replay_buffer, max_size=variant['replay_buffer_size'])

    if variant['normalize'] or variant['bonus']:
        obs_mu, obs_std = dataset['observations'].mean(axis=0), dataset['observations'].std(axis=0)
        # actions_mu, actions_std = dataset['actions'].mean(axis=0), dataset['actions'].std(axis=0)
        bonus_norm_param = [obs_mu, obs_std]
    else:
        bonus_norm_param = [None] * 2

    # shift the reward
    if variant['reward_shift'] is not None:
        rewards_shift_param = min(dataset['rewards']) - variant['reward_shift']
        print('.... reward is shifted : {} '.format(rewards_shift_param))

    if variant['bonus']:
        trainer = SAC_BonusTrainer(
            env=eval_env,
            policy=policy,
            qf1=qf1,
            qf2=qf2,
            target_qf1=target_qf1,
            target_qf2=target_qf2,
            bonus_network=bonus_network,
            beta=variant['bonus_beta'],
            use_bonus_critic=variant['use_bonus_critic'],
            use_bonus_policy=variant['use_bonus_policy'],
            bonus_norm_param=bonus_norm_param,
            rewards_shift_param=rewards_shift_param,
            device=ptu.device,
            **variant['trainer_kwargs']
        )

    else:
        trainer = SACTrainer(
            env=eval_env,
            policy=policy,
            qf1=qf1,
            qf2=qf2,
            target_qf1=target_qf1,
            target_qf2=target_qf2,
            rewards_shift_param=rewards_shift_param,
            **variant['trainer_kwargs']
        )
    algorithm = TorchBatchRLAlgorithm(
        trainer=trainer,
        exploration_env=expl_env,
        evaluation_env=eval_env,
        exploration_data_collector=expl_path_collector,
        evaluation_data_collector=eval_path_collector,
        replay_buffer=replay_buffer,
        batch_rl=True,
        q_learning_alg=True,
        **variant['algorithm_kwargs']
    )
    algorithm.to(ptu.device)
    algorithm.train()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='sac_bonus')
    parser.add_argument("--env", type=str, default='halfcheetah-medium-v0')
    # sac
    parser.add_argument('--alpha_lr', default=1e-6, type=float)
    parser.add_argument('--qf_lr', default=3e-4, type=float)
    parser.add_argument('--policy_lr', default=1e-4, type=float)
    parser.add_argument('--num_samples', default=100, type=int)
    parser.add_argument('--no_automatic_entropy_tuning', action='store_true', default=False, help='no automatic entropy tuning')


    # bonus
    parser.add_argument('--bonus', action='store_true', default=False, help='use bonus in sac')
    parser.add_argument('--beta', default=0.25, type=float, help='beta for the bonus')
    parser.add_argument("--bonus_path", type=str, default='/usr/local/google/home/shideh/', help='path to the bonus model')
    parser.add_argument("--bonus_model", type=str, default='Nov-30-2020_1147_walker2d-medium-v0.pt', help='name of the bonus model')
    parser.add_argument('--bonus_type', type=str, default='actor-critic', help='use bonus in actor, critic or both')
    parser.add_argument('--kl', action='store_true', default=False, help='use bonus in KL regularized way')
    parser.add_argument('--normalize', action='store_true', default=False, help='use normalization in bonus')
    parser.add_argument('--reward_shift', default=None, type=int, help='minimum reward')

    # d4rl
    parser.add_argument('--dataset_path', type=str, default=None, help='d4rl dataset path')

    parser.add_argument('--no-cuda', action='store_true', default=False, help='disables cuda (default: False')
    parser.add_argument('--seed', default=10, type=int)
    parser.add_argument('--device-id', type=int, default=0, help='GPU device id (default: 0')
    args = parser.parse_args()

    # noinspection
    bonus_path = '{}RL/continuous_rnd/sac/examples/models/{}'.format(
        args.bonus_path, args.bonus_model)
    variant = dict(
        algorithm="SAC",
        # bonus
        bonus=args.bonus,
        bonus_path=bonus_path,
        bonus_beta=args.beta,
        version="normal",
        layer_size=256,
        replay_buffer_size=int(1E6),
        buffer_filename=args.env,  # halfcheetah_101000.pkl',
        load_buffer=True,
        env_name=args.env,
        seed=args.seed,
        # bonus_type
        use_bonus_policy=False,
        use_bonus_critic=False,
        KL=False,
        # use normalization for bonus
        normalize=args.normalize,

        # make reward positive
        reward_shift=args.reward_shift,

        algorithm_kwargs=dict(
            num_epochs=3000,
            num_eval_steps_per_epoch=5000,
            num_trains_per_train_loop=1000,
            num_expl_steps_per_train_loop=1000,
            min_num_steps_before_training=1000,
            max_path_length=1000,
            batch_size=256,
            num_actions_sample=args.num_samples,

        ),
        trainer_kwargs=dict(
            discount=0.99,
            soft_target_tau=5e-3,
            target_update_period=1,
            policy_lr=args.policy_lr,
            qf_lr=args.qf_lr,
            alpha_lr=args.alpha_lr,
            reward_scale=1,
            use_automatic_entropy_tuning=not args.no_automatic_entropy_tuning,),
    )

    # datatset

    # timestapms
    t = time.localtime()
    timestamp = time.strftime('%b-%d-%Y_%H%M', t)
    # bonus and the type
    if args.bonus:
        exp_dir = '{}/bonus_{}/{}_{}'.format(args.env,
                                             timestamp, args.bonus_type, args.seed)
        # use bonus in actor, critic or both
        if args.bonus_type == 'actor-critic':

            variant["use_bonus_policy"] = True
            variant["use_bonus_critic"] = True

        elif args.bonus_type == 'critic':

            variant["use_bonus_critic"] = True

        else:
            variant["use_bonus_policy"] = True

        if args.kl:
            # use bonus as KL: -\beta * b - \tau * lse (- \tau * b / \beta)
            exp_dir = '{}_kl'.format(exp_dir)
            variant["KL"] = True

        else:
            # use bonus as KL: -\beta * b
            exp_dir = '{0}_{1:.2g}'.format(exp_dir, args.beta)

    else:
        exp_dir = '{}/offline/{}_{}'.format(args.env, timestamp, args.seed)

    # if args.use_norm and args.bonus:
    #     exp_dir = exp_dir + '_bonus_norm'

    # setup the logger
    print('experiment dir:logs/{}'.format(exp_dir))
    setup_logger(variant=variant, log_dir='logs/{}'.format(exp_dir))

    # cuda setup
    use_cuda = not args.no_cuda and torch.cuda.is_available()

    if use_cuda:
        # optionally set the GPU (default=False)
        ptu.set_gpu_mode(True, gpu_id=args.device_id)
        print('using gpu:{}'.format(args.device_id))
        def map_location(storage, loc): return storage.cuda()

    else:
        map_location = 'cpu'
        ptu.set_gpu_mode(False)  # optionally set the GPU (default=False)

    experiment(variant)