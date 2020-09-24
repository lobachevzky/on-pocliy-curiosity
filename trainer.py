import itertools
import inspect
import os
import sys
from abc import ABC
from collections import namedtuple, defaultdict
from pathlib import Path
from pprint import pprint
from typing import Dict, Optional

import gym
import numpy as np
import ray
import torch
from ray import tune
from ray.tune.suggest.hyperopt import HyperOptSearch
from tensorboardX import SummaryWriter

from aggregator import SumAcrossEpisode, InfosAggregator, EvalWrapper
from common.vec_env.dummy_vec_env import DummyVecEnv
from common.vec_env.subproc_vec_env import SubprocVecEnv
from common.vec_env.util import set_seeds
from networks import Agent, AgentOutputs, MLPBase
from ppo import PPO
from rollouts import RolloutStorage
from utils import k_scalar_pairs, get_device
from wrappers import VecPyTorch

EpochOutputs = namedtuple("EpochOutputs", "obs reward done infos act masks")


class Trainer:
    @classmethod
    def structure_config(cls, **config):
        agent_args = {}
        rollouts_args = {}
        ppo_args = {}
        gen_args = {}
        for k, v in config.items():
            if k in ["num_processes"]:
                gen_args[k] = v
            else:
                if k in inspect.signature(cls.build_agent).parameters:
                    agent_args[k] = v
                if k in inspect.signature(Agent.__init__).parameters:
                    agent_args[k] = v
                if k in inspect.signature(MLPBase.__init__).parameters:
                    agent_args[k] = v
                if k in inspect.signature(RolloutStorage.__init__).parameters:
                    rollouts_args[k] = v
                if k in inspect.signature(PPO.__init__).parameters:
                    ppo_args[k] = v
                if k in inspect.signature(cls.run).parameters or k not in (
                    list(agent_args.keys())
                    + list(rollouts_args.keys())
                    + list(ppo_args.keys())
                ):
                    gen_args[k] = v
        config = dict(
            agent_args=agent_args,
            rollouts_args=rollouts_args,
            ppo_args=ppo_args,
            **gen_args,
        )
        return config

    def save_checkpoint(self, tmp_checkpoint_dir):
        modules = dict(
            optimizer=self.ppo.optimizer, agent=self.agent
        )  # type: Dict[str, torch.nn.Module]
        # if isinstance(self.envs.venv, VecNormalize):
        #     modules.update(vec_normalize=self.envs.venv)
        state_dict = {name: module.state_dict() for name, module in modules.items()}
        save_path = Path(tmp_checkpoint_dir, f"checkpoint.pt")
        torch.save(dict(step=self.i, **state_dict), save_path)
        print(f"Saved parameters to {save_path}")

    def load_checkpoint(self, checkpoint_path):
        state_dict = torch.load(checkpoint_path, map_location=self.device)
        self.agent.load_state_dict(state_dict["agent"])
        self.ppo.optimizer.load_state_dict(state_dict["optimizer"])
        # if isinstance(self.envs.venv, VecNormalize):
        #     self.envs.venv.load_state_dict(state_dict["vec_normalize"])
        print(f"Loaded parameters from {checkpoint_path}.")
        return state_dict.get("step", -1) + 1

    def run(
        self,
        agent_args: dict,
        cuda: bool,
        cuda_deterministic: bool,
        env_args: dict,
        env_id: str,
        log_dir: Path,
        log_interval: int,
        normalize: float,
        num_batch: int,
        num_iterations: int,
        num_processes: int,
        ppo_args: dict,
        render_eval: bool,
        rollouts_args: dict,
        seed: int,
        synchronous: bool,
        train_steps: int,
        eval_interval: int = None,
        eval_steps: int = None,
        no_eval: bool = False,
        load_path: Path = None,
        render: bool = False,
    ):
        writer = SummaryWriter(logdir=str(log_dir))

        # Properly restrict pytorch to not consume extra resources.
        #  - https://github.com/pytorch/pytorch/issues/975
        #  - https://github.com/ray-project/ray/issues/3609
        torch.set_num_threads(1)
        os.environ["OMP_NUM_THREADS"] = "1"

        class EpochCounter:
            def __init__(self):
                self.episode_rewards = []
                self.episode_time_steps = []
                self.rewards = np.zeros(num_processes)
                self.time_steps = np.zeros(num_processes)

            def update(self, reward, done):
                self.rewards += reward.numpy()
                self.time_steps += np.ones_like(done)
                self.episode_rewards += list(self.rewards[done])
                self.episode_time_steps += list(self.time_steps[done])
                self.rewards[done] = 0
                self.time_steps[done] = 0

            def reset(self):
                self.episode_rewards = []
                self.episode_time_steps = []

            def items(self, prefix=""):
                if self.episode_rewards:
                    yield prefix + "rewards", np.mean(self.episode_rewards)
                if self.episode_time_steps:
                    yield prefix + "time_steps", np.mean(self.episode_time_steps)

        def make_vec_envs(evaluation):
            def env_thunk(rank):
                return lambda: self.make_env(
                    rank=rank,
                    evaluation=evaluation,
                    **env_args,
                )

            env_fns = [env_thunk(i) for i in range(num_processes)]
            use_dummy = len(env_fns) == 1 or sys.platform == "darwin" or synchronous
            return VecPyTorch(
                DummyVecEnv(env_fns, render=render)
                if use_dummy
                else SubprocVecEnv(env_fns)
            )

        def run_epoch(obs, rnn_hxs, masks, envs, num_steps):
            for _ in range(num_steps):
                with torch.no_grad():
                    act = agent(
                        inputs=obs, rnn_hxs=rnn_hxs, masks=masks
                    )  # type: AgentOutputs

                action = envs.preprocess(act.action)
                # Observe reward and next obs
                obs, reward, done, infos = envs.step(action)

                # If done then clean the history of observations.
                masks = torch.tensor(
                    1 - done, dtype=torch.float32, device=obs.device
                ).unsqueeze(1)
                yield EpochOutputs(
                    obs=obs, reward=reward, done=done, infos=infos, act=act, masks=masks
                )

                rnn_hxs = act.rnn_hxs

        if render_eval and not render:
            eval_interval = 1
        if render or render_eval:
            ppo_args.update(ppo_epoch=0)
            num_processes = 1
            cuda = False
        cuda &= torch.cuda.is_available()

        # reproducibility
        set_seeds(cuda, cuda_deterministic, seed)

        self.device = get_device(self.name) if cuda else "cpu"
        print("Using device", self.device)

        train_envs = make_vec_envs(evaluation=False)
        try:
            train_envs.to(self.device)
            agent = self.build_agent(envs=train_envs, **agent_args)
            start = 0
            if load_path:
                start = self.load_checkpoint(load_path)
            rollouts = RolloutStorage(
                num_steps=train_steps,
                num_processes=num_processes,
                obs_space=train_envs.observation_space,
                action_space=train_envs.action_space,
                recurrent_hidden_state_size=agent.recurrent_hidden_state_size,
                **rollouts_args,
            )

            # copy to device
            if cuda:
                agent.to(self.device)
                rollouts.to(self.device)

            train_report = SumAcrossEpisode()
            train_infos = InfosAggregator()
            ppo = PPO(agent=agent, **ppo_args)
            train_counter = EpochCounter()

            for i in range(start, num_iterations + 1):
                eval_report = EvalWrapper(SumAcrossEpisode())
                eval_infos = EvalWrapper(InfosAggregator())
                if eval_interval and not no_eval and i % eval_interval == 0:
                    # vec_norm = get_vec_normalize(eval_envs)
                    # if vec_norm is not None:
                    #     vec_norm.eval()
                    #     vec_norm.ob_rms = get_vec_normalize(envs).ob_rms

                    # self.envs.evaluate()
                    eval_masks = torch.zeros(num_processes, 1, device=self.device)
                    eval_envs = make_vec_envs(evaluation=True)
                    eval_envs.to(self.device)
                    with agent.recurrent_module.evaluating(eval_envs.observation_space):
                        eval_recurrent_hidden_states = torch.zeros(
                            num_processes,
                            agent.recurrent_hidden_state_size,
                            device=self.device,
                        )

                        for output in run_epoch(
                            obs=eval_envs.reset(),
                            rnn_hxs=eval_recurrent_hidden_states,
                            masks=eval_masks,
                            envs=eval_envs,
                            num_steps=eval_steps,
                        ):
                            eval_report.update(
                                reward=output.reward.cpu().numpy(),
                                dones=output.done,
                            )
                            eval_infos.update(*output.infos, dones=output.done)
                    eval_envs.close()

                rollouts.obs[0].copy_(train_envs.reset())

                for output in run_epoch(
                    obs=rollouts.obs[0],
                    rnn_hxs=rollouts.recurrent_hidden_states[0],
                    masks=rollouts.masks[0],
                    envs=train_envs,
                    num_steps=train_steps,
                ):
                    train_report.update(
                        reward=output.reward.cpu().numpy(),
                        dones=output.done,
                    )
                    train_infos.update(*output.infos, dones=output.done)
                    rollouts.insert(
                        obs=output.obs,
                        recurrent_hidden_states=output.act.rnn_hxs,
                        actions=output.act.action,
                        action_log_probs=output.act.action_log_probs,
                        values=output.act.value,
                        rewards=output.reward,
                        masks=output.masks,
                    )

                with torch.no_grad():
                    next_value = agent.get_value(
                        rollouts.obs[-1],
                        rollouts.recurrent_hidden_states[-1],
                        rollouts.masks[-1],
                    )

                rollouts.compute_returns(next_value.detach())
                train_results = ppo.update(rollouts)
                rollouts.after_update()

                if i % log_interval == 0:
                    result = dict(
                        **train_results,
                        **dict(train_report.items()),
                        **dict(train_infos.items()),
                        **dict(eval_report.items()),
                        **dict(eval_infos.items()),
                        step=i,
                    )
                    pprint(result)
                    if writer is not None:
                        for k, v in k_scalar_pairs(**result):
                            writer.add_scalar(k, v, i)
                    #     if (
                    #         None not in (log_dir, save_interval)
                    #         and (i + 1) % save_interval == 0
                    #     ):
                    #         print("steps until save:", save_interval - i)
                    #         trainer.save_checkpoint(Path(log_dir, "checkpoint.pt"))
                    train_report = SumAcrossEpisode()
                    train_infos = InfosAggregator()
        finally:
            train_envs.close()

    @staticmethod
    def process_infos(episode_counter, done, infos, **act_log):
        for d in infos:
            for k, v in d.items():
                episode_counter[k] += v if type(v) is list else [float(v)]
        for k, v in act_log.items():
            episode_counter[k] += v if type(v) is list else [float(v)]

    @staticmethod
    def build_agent(envs, **agent_args):
        return Agent(envs.observation_space.shape, envs.action_space, **agent_args)

    @staticmethod
    def make_env(env_id, seed, rank, evaluation, **kwargs):
        env = gym.make(env_id, **kwargs)
        env.seed(seed + rank)
        return env

    @classmethod
    def main(
        cls,
        gpus_per_trial,
        cpus_per_trial,
        log_dir,
        num_iterations,
        num_samples,
        name,
        config,
        save_interval=None,
        **kwargs,
    ):
        cls.name = name
        if config is None:
            config = dict()
        for k, v in kwargs.items():
            if k not in config or v is not None:
                config[k] = v

        config.update(num_iterations=num_iterations, log_dir=log_dir)
        if log_dir:
            print("Not using tune, because log_dir was specified")
            c = cls().structure_config(**config)
            cls().run(**c)
        else:
            local_mode = num_samples is None
            ray.init(dashboard_host="127.0.0.1", local_mode=local_mode)

            resources_per_trial = dict(gpu=gpus_per_trial, cpu=cpus_per_trial)

            if local_mode:
                print("Using local mode because num_samples is None")
                kwargs = dict()
            else:
                kwargs = dict(
                    search_alg=HyperOptSearch(config, metric="eval_reward"),
                    num_samples=num_samples,
                )
            if num_iterations:
                kwargs.update(stop=dict(training_iteration=num_iterations))

            tune.run(
                cls,
                name=name,
                config=config,
                resources_per_trial=resources_per_trial,
                **kwargs,
            )
