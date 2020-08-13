import abc
import sys
from collections import defaultdict, namedtuple
from pathlib import Path
from pprint import pprint
from typing import Dict

import gym
import numpy as np
import ray
import torch
from ray import tune
from ray.tune.suggest import HyperOptSearch
from tensorboardX import SummaryWriter

from agent import Agent, AgentOutputs
from common.vec_env.dummy_vec_env import DummyVecEnv
from common.vec_env.subproc_vec_env import SubprocVecEnv
from common.vec_env.util import set_seeds
from ppo import PPO
from rollouts import RolloutStorage
from utils import k_scalar_pairs
from wrappers import VecPyTorch

EpochOutputs = namedtuple("EpochOutputs", "obs reward done infos act masks")


class Trainer(tune.Trainable):
    def __init__(self, config):
        self.iterator = None
        self.agent = None
        self.ppo = None
        self.i = None
        self.device = None
        super().__init__(config)

    def _setup(self, config):
        self.iterator = self.gen(**config)

    def _train(self):
        return next(self.iterator)

    def _save(self, tmp_checkpoint_dir):
        modules = dict(
            optimizer=self.ppo.optimizer, agent=self.agent
        )  # type: Dict[str, torch.nn.Module]
        # if isinstance(self.envs.venv, VecNormalize):
        #     modules.update(vec_normalize=self.envs.venv)
        state_dict = {name: module.state_dict() for name, module in modules.items()}
        save_path = Path(tmp_checkpoint_dir, f"checkpoint.pt")
        torch.save(dict(step=self.i, **state_dict), save_path)
        print(f"Saved parameters to {save_path}")

    def _restore(self, checkpoint):
        state_dict = torch.load(checkpoint, map_location=self.device)
        self.agent.load_state_dict(state_dict["agent"])
        self.ppo.optimizer.load_state_dict(state_dict["optimizer"])
        start = state_dict.get("step", -1) + 1
        # if isinstance(self.envs.venv, VecNormalize):
        #     self.envs.venv.load_state_dict(state_dict["vec_normalize"])
        print(f"Loaded parameters from {checkpoint}.")

    def loop(self):
        yield from self.iterator

    def gen(
        self,
        agent_args: dict,
        cuda: bool,
        cuda_deterministic: bool,
        env_id: str,
        log_interval: int,
        normalize: float,
        num_batch: int,
        num_epochs: int,
        num_processes: int,
        ppo_args: dict,
        render: bool,
        render_eval: bool,
        rollouts_args: dict,
        seed: int,
        synchronous: bool,
        train_steps: int,
        eval_interval: int = None,
        eval_steps: int = None,
        no_eval=False,
    ):
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
                return self.make_env(
                    seed=seed, rank=rank, evaluation=evaluation, env_id=env_id
                )

            env_fns = [lambda: env_thunk(i) for i in range(num_processes)]
            use_dummy = len(env_fns) == 1 or sys.platform == "darwin" or synchronous
            return VecPyTorch(
                DummyVecEnv(env_fns, render=render)
                if use_dummy
                else SubprocVecEnv(env_fns)
            )

        def run_epoch(obs, rnn_hxs, masks, envs, num_steps):
            episode_counter = defaultdict(list)
            for _ in range(num_steps):
                with torch.no_grad():
                    act = self.agent(
                        inputs=obs, rnn_hxs=rnn_hxs, masks=masks
                    )  # type: AgentOutputs

                # Observe reward and next obs
                obs, reward, done, infos = envs.step(act.action)
                self.process_infos(episode_counter, done, infos, **act.log)

                # If done then clean the history of observations.
                masks = torch.tensor(
                    1 - done, dtype=torch.float32, device=obs.device
                ).unsqueeze(1)
                yield EpochOutputs(
                    obs=obs, reward=reward, done=done, infos=infos, act=act, masks=masks
                )

                rnn_hxs = act.rnn_hxs

        if not torch.cuda.is_available():
            cuda = False
        set_seeds(cuda=cuda, cuda_deterministic=cuda_deterministic, seed=seed)

        if render_eval and not render:
            eval_interval = 1
        if render or render_eval:
            ppo_args.update(ppo_epoch=0)
            num_processes = 1
            cuda = False

        self.device = device = torch.device("cuda" if cuda else "cpu")

        train_envs = make_vec_envs(evaluation=False)

        train_envs.to(device)
        self.agent = agent = self.build_agent(envs=train_envs, **agent_args)
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
            agent.to(device)
            rollouts.to(device)

        self.ppo = ppo = PPO(agent=agent, num_batch=num_batch, **ppo_args)

        start = 0
        train_counter = EpochCounter()
        rollouts.obs[0].copy_(train_envs.reset())

        for i in range(start, num_epochs):
            self.i = i
            for epoch_output in run_epoch(
                obs=rollouts.obs[0],
                rnn_hxs=rollouts.recurrent_hidden_states[0],
                masks=rollouts.masks[0],
                envs=train_envs,
                num_steps=train_steps,
            ):
                train_counter.update(reward=epoch_output.reward, done=epoch_output.done)
                rollouts.insert(
                    obs=epoch_output.obs,
                    recurrent_hidden_states=epoch_output.act.rnn_hxs,
                    actions=epoch_output.act.action,
                    action_log_probs=epoch_output.act.action_log_probs,
                    values=epoch_output.act.value,
                    rewards=epoch_output.reward,
                    masks=epoch_output.masks,
                )

            with torch.no_grad():
                next_value = agent.get_value(
                    rollouts.obs[-1],
                    rollouts.recurrent_hidden_states[-1],
                    rollouts.masks[-1],
                ).detach()

            rollouts.compute_returns(next_value=next_value)
            train_results = ppo.update(rollouts)
            rollouts.after_update()

            eval_counter = EpochCounter()
            if eval_interval and eval_steps and not no_eval and i % eval_interval == 0:
                # vec_norm = get_vec_normalize(eval_envs)
                # if vec_norm is not None:
                #     vec_norm.eval()
                #     vec_norm.ob_rms = get_vec_normalize(envs).ob_rms

                # self.envs.evaluate()
                eval_masks = torch.zeros(num_processes, 1, device=device)
                eval_envs = make_vec_envs(evaluation=True)
                eval_envs.to(device)
                with agent.network.evaluating(eval_envs.observation_space):
                    eval_recurrent_hidden_states = torch.zeros(
                        num_processes, agent.recurrent_hidden_state_size, device=device
                    )
                    for epoch_output in run_epoch(
                        obs=eval_envs.reset(),
                        rnn_hxs=eval_recurrent_hidden_states,
                        masks=eval_masks,
                        envs=eval_envs,
                        num_steps=eval_steps,
                    ):
                        eval_counter.update(
                            reward=epoch_output.reward, done=epoch_output.done
                        )

                eval_envs.close()
            if i % log_interval == 0:
                yield dict(
                    **train_results,
                    **dict(train_counter.items()),
                    **dict(eval_counter.items(prefix="eval_")),
                )
                train_counter.reset()

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
    def make_env(env_id, seed, rank, evaluation):
        env = gym.make(env_id)
        env.seed(seed + rank)
        return env

    @classmethod
    def main(
        cls,
        log_dir=None,
        num_samples=None,
        name=None,
        config=None,
        gpus_per_trial=None,
        cpus_per_trial=None,
        **kwargs,
    ):
        if config is None:
            config = dict()
        for k, v in kwargs.items():
            if v is not None:
                config[k] = v

        if log_dir:
            print("Not using tune, because log_dir was specified")
            writer = SummaryWriter(logdir=str(log_dir))
            for i, report in enumerate(cls(config).loop()):
                pprint(report)
                for k, v in report.items():
                    writer.add_scalar(k, v, i)
        else:
            local_mode = num_samples is None
            ray.init(dashboard_host="127.0.0.1", local_mode=local_mode)
            metric = "final_reward"

            resources_per_trial = {"gpu": gpus_per_trial, "cpu": cpus_per_trial}
            kwargs = dict()

            if local_mode:
                print("Using local mode because num_samples is None")
            else:
                kwargs = dict(
                    search_alg=HyperOptSearch(config, metric=metric),
                    num_samples=num_samples,
                )
            tune.run(
                cls,
                name=name,
                config=config,
                resources_per_trial=resources_per_trial,
                **kwargs,
            )