import json
from collections import namedtuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from gym import spaces

import ppo.control_flow.multi_step.abstract_recurrence as abstract_recurrence
import ppo.control_flow.recurrence as recurrence
from ppo.agent import Agent
from ppo.control_flow.env import Action
from ppo.control_flow.multi_step.env import Obs
from distributions import FixedCategorical, Categorical
from utils import init_

RecurrentState = namedtuple(
    "RecurrentState", "a l d h dg p va vd vdg lh l_probs a_probs d_probs dg_probs P"
)

ParsedInput = namedtuple("ParsedInput", "obs actions")


def gate(g, new, old):
    old = torch.zeros_like(new).scatter(1, old.unsqueeze(1), 1)
    return FixedCategorical(probs=g * new + (1 - g) * old)


def optimal_padding(h, kernel, stride):
    n = np.ceil((h - kernel) / stride + 1)
    return int(np.ceil((stride * (n - 1) + kernel - h) / 2))


def conv_output_dimension(h, padding, kernel, stride, dilation=1):
    return int(1 + (h + 2 * padding - dilation * (kernel - 1) - 1) / stride)


class Recurrence(abstract_recurrence.Recurrence, recurrence.Recurrence):
    def __init__(
        self,
        hidden_size,
        conv_hidden_size,
        fuzz,
        inventory_hidden_size,
        gate_coef,
        observation_space,
        lower_level_load_path,
        lower_embed_size,
        kernel_size,
        stride,
        action_space,
        sum_pool,
        lower_level_config,
        task_embed_size,
        num_edges,
        activation,
        num_layers,
        **kwargs,
    ):
        self.sum_pool = sum_pool
        self.fuzz = fuzz
        self.gate_coef = gate_coef
        self.conv_hidden_size = conv_hidden_size
        self.kernel_size = kernel_size
        self.stride = stride
        observation_space = Obs(**observation_space.spaces)
        recurrence.Recurrence.__init__(
            self,
            hidden_size=hidden_size,
            task_embed_size=task_embed_size,
            observation_space=observation_space,
            action_space=action_space,
            num_edges=num_edges,
            **kwargs,
        )
        self.conv_hidden_size = conv_hidden_size
        abstract_recurrence.Recurrence.__init__(self)
        d, h, w = observation_space.obs.shape
        self.obs_dim = d
        self.kernel_size = min(d, kernel_size)
        self.padding = padding = optimal_padding(h, kernel_size, stride) + 1
        self.embed_lower = nn.Embedding(
            self.action_space_nvec.lower + 1, lower_embed_size
        )
        inventory_size = self.obs_spaces.inventory.n
        self.embed_inventory = nn.Sequential(
            init_(nn.Linear(inventory_size, inventory_hidden_size)), nn.ReLU()
        )
        m_size = (
            2 * self.task_embed_size + hidden_size
            if self.no_pointer
            else self.task_embed_size
        )
        output_dim = conv_output_dimension(
            h=h, padding=padding, kernel=kernel_size, stride=stride
        )
        h1_size = self.conv_hidden_size
        if not sum_pool:
            h1_size *= output_dim ** 2

        zeta1_input_size = m_size + h1_size + inventory_hidden_size
        self.zeta1 = init_(nn.Linear(zeta1_input_size, hidden_size))
        z2_size = zeta1_input_size + lower_embed_size
        if self.olsk:
            assert self.ne == 3
            self.upsilon = nn.GRUCell(z2_size, hidden_size)
            self.beta = init_(nn.Linear(hidden_size, self.ne))
        elif self.no_pointer:
            self.upsilon = nn.GRUCell(z2_size, hidden_size)
            self.beta = init_(nn.Linear(hidden_size, self.d_space()))
        else:
            self.upsilon = init_(nn.Linear(z2_size, self.ne))
            in_size = (2 if self.no_roll or self.no_scan else 1) * task_embed_size
            out_size = self.ne * self.d_space() if self.no_scan else self.ne
            self.beta = nn.Sequential(init_(nn.Linear(in_size, out_size)))
        self.d_gate = Categorical(z2_size, 2)
        self.kernel_net = nn.Linear(m_size, conv_hidden_size * kernel_size ** 2 * d)
        self.conv_bias = nn.Parameter(torch.zeros(conv_hidden_size))
        self.critic_a = init_(nn.Linear(hidden_size, 1))
        self.critic_d = init_(nn.Linear(z2_size, 1))
        state_sizes = self.state_sizes._asdict()
        with lower_level_config.open() as f:
            lower_level_params = json.load(f)
        ll_action_space = spaces.Discrete(Action(*action_space.nvec).lower)
        del state_sizes["v"]
        self.state_sizes = RecurrentState(
            **state_sizes,
            va=1,
            vd=1,
            vdg=1,
            dg_probs=2,
            dg=1,
            l=1,
            l_probs=ll_action_space.n,
            lh=lower_level_params["hidden_size"],
        )
        self.lower_level = Agent(
            obs_spaces=observation_space,
            entropy_coef=0,
            action_space=ll_action_space,
            lower_level=True,
            num_layers=1,
            **lower_level_params,
        )
        if lower_level_load_path is not None:
            state_dict = torch.load(lower_level_load_path, map_location="cpu")
            self.lower_level.load_state_dict(state_dict["agent"])
            print(f"Loaded lower_level from {lower_level_load_path}.")

    def get_obs_sections(self, obs_spaces):
        try:
            obs_spaces = Obs(**obs_spaces)
        except TypeError:
            pass
        return super().get_obs_sections(obs_spaces)

    def set_obs_space(self, obs_space):
        super().set_obs_space(obs_space)
        self.obs_spaces = Obs(**self.obs_spaces)

    def pack(self, hxs):
        def pack():
            for name, size, hx in zip(
                RecurrentState._fields, self.state_sizes, zip(*hxs)
            ):
                x = torch.stack(hx).float()
                assert np.prod(x.shape[2:]) == size
                yield x.view(*x.shape[:2], -1)

        hx = torch.cat(list(pack()), dim=-1)
        return hx, hx[-1:]

    def parse_hidden(self, hx: torch.Tensor) -> RecurrentState:
        state_sizes = self.state_sizes._replace(P=0)
        if hx.size(-1) == sum(self.state_sizes):
            state_sizes = self.state_sizes
        return RecurrentState(*torch.split(hx, state_sizes, dim=-1))

    def parse_input(self, x: torch.Tensor) -> ParsedInput:
        return ParsedInput(
            *torch.split(
                x,
                ParsedInput(obs=sum(self.obs_sections), actions=self.action_size),
                dim=-1,
            )
        )

    def inner_loop(self, raw_inputs, rnn_hxs):
        T, N, dim = raw_inputs.shape
        inputs = self.parse_input(raw_inputs)

        # parse non-action inputs
        state = Obs(*self.parse_obs(inputs.obs))
        state = state._replace(obs=state.obs.view(T, N, *self.obs_spaces.obs.shape))
        lines = state.lines.view(T, N, *self.obs_spaces.lines.shape)

        # build memory
        nl = len(self.obs_spaces.lines.nvec)
        M = self.embed_task(self.preprocess_embed(N, T, state)).view(
            N, -1, self.task_embed_size
        )
        new_episode = torch.all(rnn_hxs == 0, dim=-1).squeeze(0)
        hx = self.parse_hidden(rnn_hxs)
        for _x in hx:
            _x.squeeze_(0)

        if not self.olsk:
            P = self.build_P(M, N, rnn_hxs.device, nl)
            half = P.size(2) // 2 if self.no_scan else nl

        p = hx.p.long().squeeze(-1)
        h = hx.h
        hx.a[new_episode] = self.n_a - 1
        R = torch.arange(N, device=rnn_hxs.device)
        ones = self.ones.expand_as(R)
        actions = Action(*inputs.actions.unbind(dim=2))
        A = torch.cat([actions.upper, hx.a.view(1, N)], dim=0).long()
        L = torch.cat([actions.lower, hx.l.view(1, N) - 1], dim=0).long()
        D = torch.cat([actions.delta, hx.d.view(1, N)], dim=0).long()
        DG = torch.cat([actions.dg, hx.dg.view(1, N)], dim=0).long()

        for t in range(T):
            self.print("p", p)
            m = torch.cat([P, h], dim=-1) if self.no_pointer else M[R, p]
            conv_kernel = self.kernel_net(m).view(
                N,
                self.conv_hidden_size,
                self.obs_dim,
                self.kernel_size,
                self.kernel_size,
            )
            h1 = torch.cat(
                [
                    F.conv2d(
                        input=o.unsqueeze(0),
                        weight=k,
                        bias=self.conv_bias,
                        stride=self.stride,
                        padding=self.padding,
                    )
                    for o, k in zip(state.obs[t].unbind(0), conv_kernel.unbind(0))
                ],
                dim=0,
            ).relu()
            if self.sum_pool:
                h1 = h1.sum(-1).sum(-1)
            else:
                h1 = h1.view(N, -1)
            inventory = self.embed_inventory(state.inventory[t])
            zeta1_input = torch.cat([m, h1, inventory], dim=-1)
            z1 = F.relu(self.zeta1(zeta1_input))
            a_dist = self.actor(z1)
            self.sample_new(A[t], a_dist)
            a = A[t]
            self.print("a_probs", a_dist.probs)
            # line_type, be, it, _ = lines[t][R, hx.p.long().flatten()].unbind(-1)
            # a = 3 * (it - 1) + (be - 1)

            ll_output = self.lower_level(
                Obs(**{k: v[t] for k, v in state._asdict().items()}),
                hx.lh,
                masks=None,
                action=None,
                upper=a,
            )
            if torch.any(L[0] < 0):
                assert torch.all(L[0] < 0)
                L[t] = ll_output.action.flatten()

            if self.fuzz:
                ac, be, it, _ = lines[t][R, p].long().unbind(-1)  # N, 2
                sell = (be == 2).long()
                channel_index = 3 * sell + (it - 1) * (1 - sell)
                channel = state.obs[t][R, channel_index]
                agent_channel = state.obs[t][R, -1]
                # self.print("channel", channel)
                # self.print("agent_channel", agent_channel)
                is_subtask = (ac == 0).flatten()
                standing_on = (channel * agent_channel).view(N, -1).sum(-1)
                # correct_action = ((be - 1) == L[t]).float()
                # self.print("be", be)
                # self.print("L[t]", L[t])
                # self.print("correct_action", correct_action)
                # dg = standing_on * correct_action + not_subtask
                fuzz = (
                    is_subtask.long()
                    * (1 - standing_on).long()
                    * torch.randint(2, size=(len(standing_on),), device=rnn_hxs.device)
                )
                lt = (fuzz * (be - 1) + (1 - fuzz) * L[t]).long()
                self.print("fuzz", fuzz, lt)
                # dg = dg.view(N, 1)
                # correct_action = ((be - 1) == lt).float()
            else:
                lt = L[t]

            embedded_lower = self.embed_lower(lt.clone())
            self.print("L[t]", L[t])
            self.print("lines[R, p]", lines[t][R, p])
            z2 = torch.cat([zeta1_input, embedded_lower], dim=-1)
            d_gate = self.d_gate(z2)
            self.sample_new(DG[t], d_gate)
            dg = DG[t].unsqueeze(-1).float()

            # _, _, it, _ = lines[t][R, p].long().unbind(-1)  # N, 2
            # sell = (be == 2).long()
            # index1 = it - 1
            # index2 = 1 + ((it - 3) % 3)
            # channel1 = state.obs[t][R, index1].sum(-1).sum(-1)
            # channel2 = state.obs[t][R, index2].sum(-1).sum(-1)
            # z = (channel1 > channel2).unsqueeze(-1).float()

            if self.olsk or self.no_pointer:
                h = self.upsilon(z2, h)
                u = self.beta(h).softmax(dim=-1)
                d_dist = gate(dg, u, ones)
                self.sample_new(D[t], d_dist)
                delta = D[t].clone() - 1
            else:
                u = self.upsilon(z2).softmax(dim=-1)
                self.print("u", u)
                w = P[p, R]
                d_probs = (w @ u.unsqueeze(-1)).squeeze(-1)

                self.print("dg prob", d_gate.probs[:, 1])
                self.print("dg", dg)
                d_dist = gate(dg, d_probs, ones * half)
                self.print("d_probs", d_probs[:, half:])
                self.sample_new(D[t], d_dist)
                # D[:] = float(input("D:")) + half
                delta = D[t].clone() - half
                self.print("D[t], delta", D[t], delta)
                P.view(N, *self.P_shape())
            p = p + delta
            p = torch.clamp(p, min=0, max=M.size(1) - 1)

            # try:
            # A[:] = float(input("A:"))
            # except ValueError:
            # pass
            vd = self.critic_d(z2)
            yield RecurrentState(
                a=A[t],
                l=L[t],
                lh=hx.lh,
                va=self.critic_a(z1),
                vd=vd,
                vdg=vd,
                h=h,
                p=p,
                d=D[t],
                dg=dg,
                a_probs=a_dist.probs,
                d_probs=d_dist.probs,
                dg_probs=d_gate.probs,
                l_probs=ll_output.dist.probs,
                P=hx.P if (self.olsk or self.no_pointer) else P.transpose(0, 1),
            )
