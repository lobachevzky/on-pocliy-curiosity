from collections import OrderedDict, namedtuple

import numpy as np
from gym import spaces

import upper_env
import keyboard_control
from upper_env import Action
from objects import Interaction, Resource

Obs = namedtuple("Obs", "inventory line obs")


class Env(upper_env.Env):
    def __init__(self, *args, **kwargs):
        kwargs.update(
            bridge_failure_prob=0,
            map_discovery_prob=0,
            bandit_prob=0,
            windfall_prob=0,
        )
        super().__init__(*args, **kwargs)
        self.observation_space.spaces["line"] = spaces.MultiDiscrete(
            np.array(self.line_space)
        )
        self.observation_space = spaces.Dict(
            {k: v for k, v in self.observation_space.spaces.items() if k in Obs._fields}
        )

        self.action_space = spaces.Discrete(len(self.lower_level_actions))

    def step(self, action: int):
        action = Action(
            upper=0, lower=self.lower_level_actions[action], delta=0, dg=0, ptr=0
        )
        return self.iterator.send(action)

    def obs_generator(self, lines):
        iterator = super().obs_generator(lines)
        state = yield next(iterator)

        def line(ptr, **_):
            ptr = min(ptr, len(lines) - 1)
            return self.preprocess_line(lines[ptr])

        while True:
            obs, render = iterator.send(state)
            obs = OrderedDict(
                Obs(
                    inventory=obs["inventory"], line=line(**state), obs=obs["obs"]
                )._asdict()
            )
            state = yield obs, render

    def done_generator(self, lines):
        state = yield
        time_remaining = self.time_per_subtask()

        while True:
            done = state["subtask_complete"]
            if not self.evaluating:
                time_remaining -= 1
                done |= time_remaining == 0
            state = yield done, lambda: print("Time remaining:", time_remaining)

    def info_generator(self, lines, rooms):
        iterator = super().info_generator(lines, rooms)
        state = yield next(iterator)
        while True:
            info, render = iterator.send(state)
            if state["done"]:
                info.update(success=state["subtask_complete"])
            state = yield info, render

    def main(self):
        actions = [
            tuple(x) if isinstance(x, np.ndarray) else x
            for x in self.lower_level_actions
        ]
        mapping = dict(
            w=(-1, 0),
            s=(1, 0),
            a=(0, -1),
            d=(0, 1),
            c=Interaction.COLLECT,
            t=Resource.STONE,
            i=Resource.IRON,
            o=Resource.WOOD,
        )

        def action_fn(string):
            action = mapping.get(string, None)
            if action is None:
                return None
            return actions.index(action)

        keyboard_control.run(self, action_fn=action_fn)


if __name__ == "__main__":
    import argparse

    PARSER = argparse.ArgumentParser()
    PARSER.add_argument("--min-eval-lines", type=int)
    PARSER.add_argument("--max-eval-lines", type=int)
    Env.add_arguments(PARSER)
    PARSER.add_argument("--seed", default=0, type=int)
    Env(rank=0, **vars(PARSER.parse_args())).main()
