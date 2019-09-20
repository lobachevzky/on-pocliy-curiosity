from collections import Counter
import functools
import itertools
import re
import sys
import time

import gym
import numpy as np
import torch
from gym.wrappers import TimeLimit
from tensorboardX import SummaryWriter
from tqdm import tqdm

from common.atari_wrappers import wrap_deepmind
from common.vec_env.dummy_vec_env import DummyVecEnv
from common.vec_env.subproc_vec_env import SubprocVecEnv
from ppo.agent import Agent, AgentValues
from ppo.storage import RolloutStorage
from ppo.update import PPO
from ppo.utils import k_scalar_pairs, get_n_gpu, get_random_gpu
from ppo.wrappers import AddTimestep, TransposeImage, VecPyTorch, VecPyTorchFrameStack


# noinspection PyAttributeOutsideInit
class TrainBase(abc.ABC):
    def setup(
        self,
        num_steps,
        num_processes,
        seed,
        cuda_deterministic,
        cuda,
        time_limit,
        gamma,
        normalize,
        log_interval,
        eval_interval,
        use_gae,
        tau,
        ppo_args,
        agent_args,
        render,
        render_eval,
        load_path,
        synchronous,
        num_batch,
        env_args,
        success_reward,
        use_tqdm,
    ):
        if render_eval and not render:
            eval_interval = 1
        if render or render_eval:
            ppo_args.update(ppo_epoch=0)
            num_processes = 1
            cuda = False

        # reproducibility
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        np.random.seed(seed)
        cuda &= torch.cuda.is_available()
        if cuda and cuda_deterministic:
            torch.backends.cudnn.benchmark = False
            torch.backends.cudnn.deterministic = True
        torch.set_num_threads(1)

        envs = self.make_vec_envs(
            env_id=env_id,
            seed=seed,
            num_processes=num_processes,
            gamma=(gamma if normalize else None),
            add_timestep=add_timestep,
            render=render,
            synchronous=True if render else synchronous,
            evaluation=False)

        self.agent = self.build_agent(envs, **agent_args)
        rollouts = RolloutStorage(
            num_steps=num_steps,
            num_processes=num_processes,
            obs_space=self.envs.observation_space,
            action_space=self.envs.action_space,
            recurrent_hidden_state_size=self.agent.recurrent_hidden_state_size,
            use_gae=use_gae,
            gamma=gamma,
            tau=tau,
        )

        # copy to device
        if cuda:
            tick = time.time()
            self.agent.to(self.device)
            self.rollouts.to(self.device)
            print("Values copied to GPU in", time.time() - tick, "seconds")

        self.ppo = PPO(agent=self.agent, num_batch=num_batch, **ppo_args)
        self.counter = Counter()

        self.i = 0
        if load_path:
            self._restore(load_path)

        self.make_train_iterator = lambda: self.train_generator(
            num_steps=num_steps,
            num_processes=num_processes,
            time_limit=time_limit,
            log_interval=log_interval,
            eval_interval=eval_interval,
            use_tqdm=use_tqdm,
            success_reward=success_reward,
        )
        self.train_iterator = self.make_train_iterator()

    def _train(self):
        try:
            return next(self.train_iterator)
        except StopIteration:
            self.train_iterator = self.make_train_iterator()
            return self._train()

    def train_generator(
        self,
        num_steps,
        num_processes,
        time_limit,
        log_interval,
        eval_interval,
        success_reward,
        use_tqdm,
    ):
        if eval_interval:
            # vec_norm = get_vec_normalize(eval_envs)
            # if vec_norm is not None:
            #     vec_norm.eval()
            #     vec_norm.ob_rms = get_vec_normalize(envs).ob_rms
            self.envs.evaluate()
            eval_recurrent_hidden_states = torch.zeros(
                num_processes,
                self.agent.recurrent_hidden_state_size,
                device=self.device,
            )
            eval_masks = torch.zeros(num_processes, 1, device=self.device)
            eval_counter = Counter()
            eval_result = self.run_epoch(
                obs=self.envs.reset(),
                rnn_hxs=eval_recurrent_hidden_states,
                masks=eval_masks,
                num_steps=time_limit,
                # max(num_steps, time_limit) if time_limit else num_steps,
                counter=eval_counter,
                success_reward=success_reward,
                use_tqdm=use_tqdm,
            )
            eval_result = {f"eval_{k}": v for k, v in eval_result.items()}
        else:
            eval_result = {}
        self.envs.train()
        obs = self.envs.reset()
        self.rollouts.obs[0].copy_(obs)
        tick = time.time()
        log_progress = None

        if eval_interval:
            eval_iterator = range(self.i % eval_interval, eval_interval)
            if use_tqdm:
                eval_iterator = tqdm(eval_iterator, desc="next eval")
        else:
            eval_iterator = itertools.count(self.i)

        for _ in eval_iterator:
            if self.i % log_interval == 0 and use_tqdm:
                log_progress = tqdm(total=log_interval, desc="next log")
            self.i += 1
            epoch_counter = self.run_epoch(
                obs=self.rollouts.obs[0],
                rnn_hxs=self.rollouts.recurrent_hidden_states[0],
                masks=self.rollouts.masks[0],
                num_steps=num_steps,
                counter=self.counter,
                success_reward=success_reward,
                use_tqdm=False,
            )

            with torch.no_grad():
                next_value = self.agent.get_value(rollouts.obs[-1],
                                                  rollouts.recurrent_hidden_states[-1],
                                                  rollouts.masks[-1]).detach()

            rollouts.compute_returns(next_value=next_value, use_gae=use_gae, gamma=gamma, tau=tau)
            train_results = ppo.update(rollouts)
            rollouts.after_update()

            if save_dir and save_interval and \
                    time.time() - last_save >= save_interval:
                last_save = time.time()
                modules = dict(
                    optimizer=ppo.optimizer, agent=self.agent)  # type: Dict[str, torch.nn.Module]

                if isinstance(envs.venv, VecNormalize):
                    modules.update(vec_normalize=envs.venv)

                state_dict = {name: module.state_dict() for name, module in modules.items()}
                save_path = Path(save_dir, 'checkpoint.pt')
                torch.save(dict(step=j, **state_dict), save_path)

                print(f'Saved parameters to {save_path}')

            total_num_steps = (j + 1) * num_processes * num_steps

            mean_success_rate = np.mean(epoch_counter['successes'])
            if target_success_rate and mean_success_rate > target_success_rate:
                print('Finished training with success rate of', mean_success_rate)
                return

            if j % log_interval == 0 and writer is not None:
                end = time.time()
                fps = total_num_steps / (end - start)
                log_values = dict(fps=fps, **epoch_counter, **train_results)
                if writer:
                    for k, v in log_values.items():
                        mean = np.mean(v)
                        if not np.isnan(mean):
                            writer.add_scalar(k, np.mean(v), total_num_steps)

            log_progress.update()

            if eval_interval is not None and j % eval_interval == eval_interval - 1:
                eval_envs = self.make_vec_envs(
                    env_id=env_id,
                    seed=seed + num_processes,
                    num_processes=num_processes,
                    gamma=gamma if normalize else None,
                    add_timestep=add_timestep,
                    evaluation=True,
                    synchronous=True if render_eval else synchronous,
                    render=render_eval)
                eval_envs.to(device)

                # vec_norm = get_vec_normalize(eval_envs)
                # if vec_norm is not None:
                #     vec_norm.eval()
                #     vec_norm.ob_rms = get_vec_normalize(envs).ob_rms

                obs = eval_envs.reset()
                eval_recurrent_hidden_states = torch.zeros(
                    num_processes, self.agent.recurrent_hidden_state_size, device=device)
                eval_masks = torch.zeros(num_processes, 1, device=device)
                eval_counter = Counter()

                eval_values = self.run_epoch(
                    envs=eval_envs,
                    obs=obs,
                    rnn_hxs=eval_recurrent_hidden_states,
                    masks=eval_masks,
                    num_steps=max(num_steps, max_episode_steps)
                    if max_episode_steps else num_steps,
                    rollouts=None,
                    counter=eval_counter)

                eval_envs.close()

                print('Evaluation outcome:')
                if writer is not None:
                    for k, v in eval_values.items():
                        print(f'eval_{k}', np.mean(v))
                        writer.add_scalar(f'eval_{k}', np.mean(v), total_num_steps)

            if eval_interval:
                eval_progress.update()

    def run_epoch(
        self, obs, rnn_hxs, masks, num_steps, counter, success_reward, use_tqdm
    ):
        # noinspection PyTypeChecker
        episode_counter = defaultdict(list)
        iterator = range(num_steps)
        if use_tqdm:
            iterator = tqdm(iterator, desc="evaluating")
        for step in iterator:
            with torch.no_grad():
                act = self.agent(inputs=obs, rnn_hxs=rnn_hxs, masks=masks)  # type: AgentValues

            # Observe reward and next obs
            obs, reward, done, infos = self.envs.step(act.action)

            for d in infos:
                for k, v in d.items():
                    episode_counter[k] += [float(v)]

            # track rewards
            counter['reward'] += reward.numpy()
            counter['time_step'] += np.ones_like(done)
            episode_rewards = counter['reward'][done]
            episode_counter['rewards'] += list(episode_rewards)
            if self.success_reward is not None:
                episode_counter['success'] += list(episode_rewards >= self.success_reward)
            episode_counter['time_steps'] += list(counter['time_step'][done])
            counter['reward'][done] = 0
            counter['time_step'][done] = 0

            # If done then clean the history of observations.
            masks = torch.tensor(1 - done, dtype=torch.float32, device=obs.device).unsqueeze(1)
            rnn_hxs = act.rnn_hxs
            if rollouts is not None:
                rollouts.insert(
                    obs=obs,
                    recurrent_hidden_states=act.rnn_hxs,
                    actions=act.action,
                    action_log_probs=act.action_log_probs,
                    values=act.value,
                    rewards=reward,
                    masks=masks)

        return dict(episode_counter)

    @staticmethod
    def build_agent(envs, **agent_args):
        return Agent(envs.observation_space.shape, envs.action_space, **agent_args)

    @staticmethod
    def make_env(env_id, seed, rank, add_timestep):
        if env_id.startswith("dm"):
            _, domain, task = env_id.split('.')
            env = dm_control2gym.make(domain_name=domain, task_name=task)
        else:
            env = gym.make(env_id)

        is_atari = hasattr(gym.envs, 'atari') and isinstance(env.unwrapped,
                                                             gym.envs.atari.atari_env.AtariEnv)
        if isinstance(env.unwrapped, SubtasksGridWorld):
            env = Wrapper(env)

        env.seed(seed + rank)
        obs_shape = env.observation_space.shape

        if add_timestep and len(obs_shape) == 1 and str(env).find('TimeLimit') > -1:
            env = AddTimestep(env)
        if is_atari and len(env.observation_space.shape) == 3:
            env = wrap_deepmind(env)

        # elif len(env.observation_space.shape) == 3:
        #     raise NotImplementedError(
        #         "CNN models work only for atari,\n"
        #         "please use a custom wrapper for a custom pixel input env.\n"
        #         "See wrap_deepmind for an example.")

        # If the input has shape (W,H,3), wrap for PyTorch convolutions
        obs_shape = env.observation_space.shape
        if len(obs_shape) == 3 and obs_shape[2] in [1, 3]:
            env = TransposeImage(env)

        if time_limit is not None:
            env = TimeLimit(env, max_episode_steps=time_limit)

        return env

        envs = [functools.partial(self.make_env, rank=i, **kwargs) for i in range(num_processes)]

        if len(envs) == 1 or sys.platform == "darwin" or synchronous:
            envs = DummyVecEnv(envs, render=render)
        else:
            envs = SubprocVecEnv(envs)

        # if (
        # envs.observation_space.shape
        # and len(envs.observation_space.shape) == 1
        # ):
        # if gamma is None:
        # envs = VecNormalize(envs, ret=False)
        # else:
        # envs = VecNormalize(envs, gamma=gamma)

        envs = VecPyTorch(envs)

        if num_frame_stack is not None:
            envs = VecPyTorchFrameStack(envs, num_frame_stack)
        # elif len(envs.observation_space.shape) == 3:
        #     envs = VecPyTorchFrameStack(envs, 4, device)

        return envs

    def _save(self, checkpoint_dir):
        modules = dict(
            optimizer=self.ppo.optimizer, agent=self.agent
        )  # type: Dict[str, torch.nn.Module]
        # if isinstance(self.envs.venv, VecNormalize):
        #     modules.update(vec_normalize=self.envs.venv)
        state_dict = {name: module.state_dict() for name, module in modules.items()}
        save_path = Path(checkpoint_dir, "checkpoint.pt")
        torch.save(dict(step=self.i, **state_dict), save_path)
        print(f"Saved parameters to {save_path}")
        return str(save_path)

    def _restore(self, checkpoint):
        load_path = checkpoint
        state_dict = torch.load(load_path, map_location=self.device)
        self.agent.load_state_dict(state_dict["agent"])
        self.ppo.optimizer.load_state_dict(state_dict["optimizer"])
        self.i = state_dict.get("step", -1) + 1
        # if isinstance(self.envs.venv, VecNormalize):
        #     self.envs.venv.load_state_dict(state_dict["vec_normalize"])
        print(f"Loaded parameters from {load_path}.")

    @abc.abstractmethod
    def get_device(self):
        raise NotImplementedError


class Train(TrainBase):
    def __init__(
        self,
        run_id,
        log_dir: Path,
        save_interval: int,
        num_processes: int,
        num_steps: int,
        **kwargs,
    ):
        self.num_steps = num_steps
        self.num_processes = num_processes
        self.run_id = run_id
        self.save_interval = save_interval
        self.log_dir = log_dir
        if log_dir:
            self.writer = SummaryWriter(logdir=str(log_dir))
        else:
            self.writer = None
        self.setup(**kwargs, num_processes=num_processes, num_steps=num_steps)
        self.last_save = time.time()  # dummy save

    def run(self):
        for _ in itertools.count():
            for result in self.make_train_iterator():
                if self.writer is not None:
                    total_num_steps = (self.i + 1) * self.num_processes * self.num_steps
                    for k, v in k_scalar_pairs(**result):
                        self.writer.add_scalar(k, v, total_num_steps)

                if (
                    self.log_dir
                    and self.save_interval
                    and (time.time() - self.last_save >= self.save_interval)
                ):
                    self._save(str(self.log_dir))
                    self.last_save = time.time()

    def get_device(self):
        match = re.search("\d+$", self.run_id)
        if match:
            device_num = int(match.group()) % get_n_gpu()
        else:
            device_num = get_random_gpu()

        return torch.device("cuda", device_num)
