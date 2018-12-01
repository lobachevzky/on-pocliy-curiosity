# third party
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from ppo.storage import RolloutStorage


class PPO:
    def __init__(self,
                 actor_critic,
                 clip_param,
                 ppo_epoch,
                 num_mini_batch,
                 value_loss_coef,
                 entropy_coef,
                 lr=None,
                 eps=None,
                 max_grad_norm=None,
                 use_clipped_value_loss=True,
                 unsupervised=False,
                 reward_structure=None):

        self.unsupervised = unsupervised
        self.actor_critic = actor_critic

        self.clip_param = clip_param
        self.ppo_epoch = ppo_epoch
        self.num_mini_batch = num_mini_batch

        self.value_loss_coef = value_loss_coef
        self.entropy_coef = entropy_coef

        self.max_grad_norm = max_grad_norm
        self.use_clipped_value_loss = use_clipped_value_loss

        if reward_structure is not None:
            self.reward_optimizer = reward_structure.optimizer
        self.optimizer = optim.Adam(actor_critic.parameters(), lr=lr, eps=eps)
        self.reward_function = None

    def update(self, rollouts: RolloutStorage):
        advantages = rollouts.returns[:-1] - rollouts.value_preds[:-1]
        advantages = (advantages - advantages.mean()) / (
            advantages.std() + 1e-5)

        value_loss_epoch = 0
        action_loss_epoch = 0
        dist_entropy_epoch = 0

        unsupervised_vals = {}

        for e in range(self.ppo_epoch):
            if self.actor_critic.is_recurrent:
                data_generator = rollouts.recurrent_generator(
                    advantages, self.num_mini_batch)
            else:
                data_generator = rollouts.feed_forward_generator(
                    advantages, self.num_mini_batch)

            for sample in data_generator:
                obs_batch, recurrent_hidden_states_batch, actions_batch, \
                value_preds_batch, return_batch, masks_batch, \
                old_action_log_probs_batch, \
                adv_targ, raw_returns = sample

                # Reshape to do in a single forward pass for all steps
                values, action_log_probs, dist_entropy, \
                _ = self.actor_critic.evaluate_actions(
                    obs_batch, recurrent_hidden_states_batch, masks_batch,
                    actions_batch)

                ratio = torch.exp(action_log_probs -
                                  old_action_log_probs_batch)
                surr1 = ratio * adv_targ
                surr2 = torch.clamp(ratio, 1.0 - self.clip_param,
                                    1.0 + self.clip_param) * adv_targ
                action_loss = -torch.min(surr1, surr2).mean()

                if self.use_clipped_value_loss:

                    value_pred_clipped = value_preds_batch + \
                                         (values - value_preds_batch).clamp(
                                             -self.clip_param, self.clip_param)
                    value_losses = (values - return_batch).pow(2)
                    value_losses_clipped = (
                        value_pred_clipped - return_batch).pow(2)
                    value_loss = .5 * torch.max(value_losses,
                                                value_losses_clipped).mean()
                else:
                    value_loss = 0.5 * F.mse_loss(return_batch, values)

                self.optimizer.zero_grad()
                (value_loss * self.value_loss_coef + action_loss -
                 dist_entropy * self.entropy_coef).backward(retain_graph=True)

                if self.unsupervised and e == self.ppo_epoch - 1:
                    G = raw_returns - value_preds_batch
                    ratio = torch.log(
                        action_log_probs / old_action_log_probs_batch)
                    expected_return_delta = torch.mean(G * ratio)
                    rollouts.reward_params.grad = None
                    expected_return_delta.backward(retain_graph=True)
                    self.reward_optimizer.step()
                    unsupervised_vals.update(
                        estimated_return_delta=expected_return_delta,
                        G=G,
                        ratio=ratio)

                nn.utils.clip_grad_norm_(self.actor_critic.parameters(),
                                         self.max_grad_norm)
                self.optimizer.step()

                value_loss_epoch += value_loss.item()
                action_loss_epoch += action_loss.item()
                dist_entropy_epoch += dist_entropy.item()

        num_updates = self.ppo_epoch * self.num_mini_batch

        value_loss_epoch /= num_updates
        action_loss_epoch /= num_updates
        dist_entropy_epoch /= num_updates

        return dict(
            value_loss=value_loss_epoch,
            action_loss=action_loss_epoch,
            entropy=dist_entropy_epoch,
            **unsupervised_vals)
