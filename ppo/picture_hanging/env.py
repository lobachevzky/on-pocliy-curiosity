import itertools
import shutil
import numpy as np
import gym
from gym.utils import seeding
from collections import namedtuple

Obs = namedtuple("Obs", "sizes pos index")


class Env(gym.Env):
    def __init__(
        self,
        width,
        n_train: int,
        n_eval: int,
        speed: float,
        seed: int,
        time_limit: int,
        one_hot_sizes: bool,
        one_hot_pos: bool,
        one_hot_index: bool,
    ):
        self.time_limit = time_limit
        self.speed = speed
        self.n_eval = n_eval
        self.n_train = n_train
        self.sizes = None
        self.centers = None
        self.width = width
        self.random, self.seed = seeding.np_random(seed)
        self.max_pictures = max(n_eval, n_train)
        self.one_hot_index = one_hot_index
        self.one_hot_pos = one_hot_pos
        self.one_hot_sizes = one_hot_sizes
        sizes = (
            gym.spaces.MultiDiscrete(np.ones((self.max_pictures, self.width)))
            if one_hot_sizes
            else gym.spaces.MultiDiscrete(np.ones(self.max_pictures) * self.width)
        )
        pos = (
            gym.spaces.MultiBinary(self.width)
            if one_hot_pos
            else gym.spaces.Discrete(self.width)
        )
        index = (
            gym.spaces.MultiBinary(self.max_pictures)
            if one_hot_index
            else gym.spaces.Discrete(self.max_pictures)
        )
        self.observation_space = gym.spaces.Dict(
            Obs(sizes=sizes, pos=pos, index=index)._asdict()
        )
        self.action_space = gym.spaces.Discrete(self.width + 1)
        # self.action_space = gym.spaces.Dict(
        #     goal=gym.spaces.Discrete(self.width), next=gym.spaces.Discrete(2)
        # )
        self.evaluating = False
        self.t = None
        if one_hot_sizes or one_hot_pos:
            self.eye = np.vstack([np.eye(self.width), np.zeros((1, self.width))])
        if one_hot_index:
            self.pic_eye = np.eye(self.max_pictures)

    def step(self, action):
        next_picture = action >= self.width
        self.t += 1
        if self.t > self.time_limit:
            return self.get_observation(), -2 * self.width, True, {}
        if next_picture:
            if len(self.centers) < len(self.sizes):
                self.centers.append(self.new_position())
            else:

                def compute_white_space():
                    left = 0
                    for center, picture in zip(self.centers, self.sizes):
                        right = center - picture / 2
                        yield right - left
                        left = center + picture / 2
                    yield self.width - left

                white_space = list(compute_white_space())
                # max reward is 0
                return (
                    self.get_observation(),
                    (min(white_space) - max(white_space)),
                    True,
                    {},
                )
        else:
            pos = self.centers[-1]
            desired_delta = action - pos
            delta = min(abs(desired_delta), self.speed) * (
                1 if desired_delta > 0 else -1
            )
            self.centers[-1] = max(0, min(self.width, pos + delta))
        return self.get_observation(), 0, False, {}

    def reset(self):
        self.t = 0
        self.centers = [self.new_position()]
        randoms = self.random.random(
            self.n_eval
            if self.evaluating
            else self.random.random_integers(1, self.n_train)
        )
        normalized = randoms * self.width / randoms.sum()
        cumsum = np.round(np.cumsum(normalized)).astype(int)
        z = np.roll(np.append(cumsum, 0), 1)
        self.sizes = z[1:] - z[:-1]
        self.random.shuffle(self.sizes)
        return self.get_observation()

    def new_position(self):
        return int(self.random.random() * self.width)

    def get_observation(self):
        sizes = self.pad(self.sizes)
        if self.one_hot_sizes:
            sizes = self.eye[sizes]
        pos = self.centers[-1]
        if self.one_hot_pos:
            pos = self.eye[pos]
        index = len(self.centers) - 1
        if self.one_hot_index:
            index = self.pic_eye[index]
        obs = Obs(sizes=sizes, pos=pos, index=index)._asdict()
        self.observation_space.contains(obs)
        return obs

    def pad(self, obs):
        if len(obs) == self.max_pictures:
            return obs
        return np.pad(obs, (0, self.max_pictures - len(obs)), constant_values=-1)

    def render(self, mode="human", pause=True):
        terminal_width = shutil.get_terminal_size((80, 20)).columns
        ratio = terminal_width / self.width
        right = 0
        for i, picture in enumerate(self.sizes):
            print(str(i) * int(round(picture * ratio)))
        print("placements")
        for i, (center, picture) in enumerate(zip(self.centers, self.sizes)):
            left = center - picture / 2
            print("-" * int(round(left * ratio)), end="")
            print(str(i) * int(round(picture * ratio)))
            right = center + picture / 2
        print("-" * int(round(self.width * ratio)))
        if pause:
            input("pause")

    def increment_curriculum(self):
        raise NotImplementedError

    def train(self):
        self.evaluating = False

    def evaluate(self):
        self.evaluating = True


if __name__ == "__main__":
    import argparse
    from rl_utils import hierarchical_parse_args, namedtuple
    from ppo import keyboard_control

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument("--width", default=100, type=int)
    parser.add_argument("--n-train", default=4, type=int)
    parser.add_argument("--n-eval", default=6, type=int)
    parser.add_argument("--speed", default=100, type=int)
    parser.add_argument("--time-limit", default=100, type=int)
    args = hierarchical_parse_args(parser)

    def action_fn(string):
        try:
            a, b = string.split()
            return float(a), int(b)
        except ValueError:
            return

    keyboard_control.run(Env(**args), action_fn=action_fn)
