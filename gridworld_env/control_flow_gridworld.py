from collections import Counter, namedtuple

from gym import spaces
import numpy as np

from gridworld_env import SubtasksGridWorld

Obs = namedtuple("Obs", "base subtask subtasks conditions control next_subtask pred")


class ControlFlowGridWorld(SubtasksGridWorld):
    def __init__(self, *args, n_subtasks, force_branching=False, **kwargs):
        super().__init__(*args, n_subtasks=n_subtasks, **kwargs)
        self.passing_objects = None
        self.failing_objects = None
        self.pred = None
        self.force_branching = force_branching
        if force_branching:
            assert n_subtasks % 2 == 0

        self.conditions = None
        self.control = None
        self.required_objects = None
        obs_space, subtasks_space = self.observation_space.spaces
        self.observation_space = spaces.Tuple(
            Obs(
                base=obs_space,
                subtasks=spaces.MultiDiscrete(
                    np.tile(subtasks_space.nvec[:1], [self.n_subtasks + 1, 1])),
                conditions=spaces.MultiDiscrete(
                    np.array([len(self.object_types)]).repeat(self.n_subtasks + 1)),
                control=spaces.MultiDiscrete(
                    np.tile(
                        np.array([[self.n_subtasks + 1]]),
                        [
                            self.n_subtasks + 1,
                            2  # binary conditions
                        ]))))

    def set_pred(self):
        try:
            self.pred = self.evaluate_condition()
        except IndexError:
            pass

    def render_current_subtask(self):
        if self.subtask_idx == 0:
            print('none')
        else:
            super().render_current_subtask()

    def render_task(self):
        def helper(i, indent):
            neg, pos = self.control[i]
            condition = self.conditions[i]

            def develop_branch(j, add_indent):
                new_indent = indent + add_indent
                if j == 0:
                    subtask = f''
                else:
                    try:
                        subtask = f'{j}:{self.subtasks[j]}'
                    except IndexError:
                        return f'{new_indent}terminate'
                return f"{new_indent}{subtask}\n{helper(j, new_indent)}"

            if pos == neg:
                return f"{develop_branch(pos, '')}"
            else:
                return f"""\
{indent}if {self.object_types[condition]}:
{develop_branch(pos, '    ')}
{indent}else:
{develop_branch(neg, '    ')}
"""

        print(helper(i=0, indent=""))

    def get_observation(self):
        obs = super().get_observation()
        obs.update(
            control=self.control,
            conditions=self.conditions,
            pred=self.evaluate_condition(),
        )
        return Obs(**obs)._asdict()

    def subtasks_generator(self):
        choices = self.np_random.choice(len(self.possible_subtasks), size=self.n_subtasks + 1)
        subtasks = [self.Subtask(*self.possible_subtasks[i]) for i in choices]
        i = 0
        encountered_passing = []
        encountered_failing = []
        encountered_subtasks = []
        encountered_idxs = []
        while True:
            passing = self.conditions[i] in self.required_objects
            branching = self.control[i, 0] != self.control[i, 1]
            encountered.update(passing=[condition if branching and passing else None])
            encountered.update(
                failing=[condition if branching and not passing else None]
            )
            encountered.update(subtasks=[i])
            i = self.control[i, int(passing)]

        failing = encountered["failing"]
        object_types = np.arange(len(self.object_types))
        non_failing = list(set(object_types) - set(failing))
        self.required_objects = list(
            set(o for o in encountered["passing"] if o is not None)
        )
        available = [x for x in self.required_objects]

        print(self.object_types)
        print('encountered_failing', encountered_failing)
        print('encountered_idxs', encountered_idxs)
        for i, subtask in enumerate(subtasks):
            if i in encountered_idxs:
                obj = subtask.object
                to_be_removed = subtask.interaction in {1, 2}
                print('available', available)
                if obj not in available:
                    if not to_be_removed and obj in failing:
                        obj = self.np_random.choice(non_failing)
                        subtasks[i] = subtask._replace(object=obj)
                    past_failing = failing[-i + 1 :]
                    if to_be_removed and obj in past_failing:
                        obj = self.np_random.choice(
                            list(set(object_types) - set(past_failing))
                        )
                        subtasks[i] = subtask._replace(object=obj)

                    # add object to map
                    self.required_objects += [obj]
                    available += [obj]
                    print('required', self.required_objects)
                    print('available', available)

                if to_be_removed:
                    available.remove(obj)
                    print('available', available)

        yield from subtasks

    def reset(self):
        n = self.n_subtasks + 1

        def get_control():
            for i in range(n):
                j = 2 * i
                if self.force_branching or self.np_random.rand() < .7:
                    yield j, j + 1
                else:
                    yield j, j

        self.control = 1 + np.minimum(np.array(list(get_control())), self.n_subtasks)
        n_object_types = self.np_random.randint(1, len(self.object_types))
        object_types = np.arange(len(self.object_types))
        existing = self.np_random.choice(object_types, size=n_object_types)
        non_existing = np.array(list(set(object_types) - set(existing)))
        n_passing = self.np_random.choice(self.n_subtasks)
        n_failing = self.n_subtasks - n_passing
        passing = self.np_random.choice(existing, size=n_passing)
        failing = self.np_random.choice(non_existing, size=n_failing)
        self.conditions = np.concatenate([passing, failing])
        self.np_random.shuffle(self.conditions)
        self.required_objects = passing
        super().reset()
        self.subtask_idx = 0
        self.subtask_idx = self.get_next_subtask()
        self.count = self.subtask.count
        return self.get_observation()

    def get_next_subtask(self):
        if self.subtask_idx > self.n_subtasks:
            return None
        resolution = self.evaluate_condition()
        return self.control[self.subtask_idx, int(resolution)]

    def evaluate_condition(self):
        object_type = self.conditions[self.subtask_idx]
        return object_type in self.objects.values()

    def get_required_objects(self, _):
        yield from self.required_objects


def main(seed, n_subtasks):
    kwargs = gridworld_env.get_args('4x4SubtasksGridWorld-v0')
    del kwargs['class_']
    del kwargs['max_episode_steps']
    kwargs.update(n_subtasks=n_subtasks, max_task_count=1)
    env = ControlFlowGridWorld(**kwargs, evaluation=False, eval_subtasks=[])
    actions = "wsadeq"
    gridworld_env.keyboard_control.run(env, actions=actions, seed=seed)


if __name__ == "__main__":
    import argparse
    import gridworld_env.keyboard_control

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int)
    parser.add_argument("--n-subtasks", type=int)
    main(**vars(parser.parse_args()))
