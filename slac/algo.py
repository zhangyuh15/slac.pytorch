import os

import numpy as np
import torch
from torch.optim import Adam

from slac.buffer import ReplayBuffer
from slac.network import GaussianPolicy, LatentModel, TwinnedQNetwork
from slac.utils import (
    calculate_gaussian_log_prob,
    create_feature_actions,
    grad_false,
    kl_divergence_loss,
    soft_update,
)


class SlacAlgorithm:
    """
    Stochactic Latent Actor-Critic(SLAC).

    Paper: https://arxiv.org/abs/1907.00953
    """

    def __init__(
        self,
        state_shape,
        action_shape,
        action_repeat,
        device,
        seed,
        gamma=0.99,
        batch_size_sac=256,
        batch_size_latent=32,
        buffer_size=10 ** 5,
        num_sequences=8,
        lr_sac=3e-4,
        lr_latent=1e-4,
        feature_dim=256,
        z1_dim=32,
        z2_dim=256,
        hidden_units=(256, 256),
        tau=5e-3,
    ):
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)

        # Replay buffer.
        self.buffer = ReplayBuffer(buffer_size, num_sequences, state_shape, action_shape, device)

        # Networks.
        self.actor = GaussianPolicy(action_shape, num_sequences, feature_dim, hidden_units).to(device)
        self.critic = TwinnedQNetwork(action_shape, z1_dim, z2_dim, hidden_units).to(device)
        self.critic_target = TwinnedQNetwork(action_shape, z1_dim, z2_dim, hidden_units).to(device)
        self.latent = LatentModel(state_shape, action_shape, feature_dim, z1_dim, z2_dim, hidden_units).to(device)

        soft_update(self.critic_target, self.critic, 1.0)
        grad_false(self.critic_target)

        # Target entropy is -|A|.
        self.target_entropy = -float(action_shape[0])
        # We optimize log(alpha) because alpha is always bigger than 0.
        self.log_alpha = torch.zeros(1, requires_grad=True, device=device)
        with torch.no_grad():
            self.alpha = self.log_alpha.exp()

        # NOTE: Policy is updated without the encoder.
        self.optim_actor = Adam(self.actor.parameters(), lr=lr_sac)
        self.optim_critic = Adam(self.critic.parameters(), lr=lr_sac)
        self.optim_alpha = Adam([self.log_alpha], lr=lr_sac)
        self.optim_latent = Adam(self.latent.parameters(), lr=lr_latent)

        self.learning_steps_sac = 0
        self.learning_steps_latent = 0
        self.state_shape = state_shape
        self.action_shape = action_shape
        self.action_repeat = action_repeat
        self.device = device
        self.gamma = gamma
        self.batch_size_sac = batch_size_sac
        self.batch_size_latent = batch_size_latent
        self.num_sequences = num_sequences
        self.tau = tau

        # JIT compile to speed up.
        fake_feature = torch.empty(1, num_sequences + 1, feature_dim, device=device)
        fake_action = torch.empty(1, num_sequences, action_shape[0], device=device)
        fake_z1 = torch.empty(1, num_sequences + 1, z1_dim, device=device)
        self.create_feature_actions = torch.jit.trace(create_feature_actions, (fake_feature, fake_action))
        self.kl_divergence = torch.jit.trace(kl_divergence_loss, (fake_z1, fake_z1, fake_z1, fake_z1))

    def preprocess(self, input):
        state = torch.tensor(input.state, dtype=torch.uint8, device=self.device).float().div_(255.0)
        with torch.no_grad():
            feature = self.latent.encoder(state).view(1, -1)
        action = torch.tensor(input.action, dtype=torch.float, device=self.device)
        feature_action = torch.cat([feature, action], dim=1)
        return feature_action

    def explore(self, input):
        feature_action = self.preprocess(input)
        with torch.no_grad():
            action = self.actor.sample(feature_action)[0]
        return action.cpu().numpy()[0]

    def exploit(self, input):
        feature_action = self.preprocess(input)
        with torch.no_grad():
            action = self.actor(feature_action)
        return action.cpu().numpy()[0]

    def step(self, env, input, t, is_random):
        t += 1

        if is_random:
            action = env.action_space.sample()
        else:
            action = self.explore(input)

        state, reward, done, _ = env.step(action)
        mask = False if t == env._max_episode_steps else done
        self.buffer.append(action, reward, mask, state, done)

        if done:
            t = 0
            state = env.reset()
            input.append(state, action)
            self.buffer.reset_episode(state)

        return t

    def update_latent(self, writer):
        self.learning_steps_latent += 1
        state_, action_, reward_, done_ = self.buffer.sample_latent(self.batch_size_latent)

        # Calculate the sequence of features.
        feature_ = self.latent.encoder(state_)

        # Sample from posterior dynamics.
        z1_mean_post_, z1_std_post_, z1_, z2_mean_post_, z2_std_post_, z2_ = self.latent.sample_post(feature_, action_)
        # Sample from prior dynamics.
        z1_mean_pri_, z1_std_pri_, _, z2_mean_pri_, z2_std_pri_, _ = self.latent.sample_prior(action_)

        # Calculate KL divergence loss.
        loss_kld = self.kl_divergence(z1_mean_post_, z1_std_post_, z1_mean_pri_, z1_mean_post_)

        # Reconstruction loss of images.
        state_mean_, state_std_ = self.latent.decoder(torch.cat([z1_, z2_], dim=-1))
        loss_image = -calculate_gaussian_log_prob(torch.log(state_std_), state_mean_ - state_).mean(dim=0).sum()

        # Prediction loss of rewards.
        reward_mean_, reward_std_ = self.latent.reward(
            torch.cat([z1_[:, :-1], z2_[:, :-1], action_, z1_[:, 1:], z2_[:, 1:]], dim=-1)
        )
        loss_reward = -calculate_gaussian_log_prob(torch.log(reward_std_), reward_mean_ - reward_) * (1.0 - done_)
        loss_reward = loss_reward.mean(dim=0).sum()

        loss_latent = loss_kld + loss_image + loss_reward

        self.optim_latent.zero_grad()
        loss_latent.backward()
        self.optim_latent.step()

        if self.learning_steps_latent % 1000 == 0:
            writer.add_scalar("loss/kld", loss_kld.item(), self.learning_steps_latent)
            writer.add_scalar("loss/reward", loss_reward.item(), self.learning_steps_latent)
            writer.add_scalar("loss/image", loss_image.item(), self.learning_steps_latent)

    def update_sac(self, writer):
        self.learning_steps_sac += 1
        state_, action_, reward = self.buffer.sample_sac(self.batch_size_sac)

        # NOTE: Don't update the encoder part of the policy here.
        with torch.no_grad():
            # f(1:t+1)
            feature_ = self.latent.encoder(state_)
            _, _, z1_, _, _, z2_ = self.latent.sample_post(feature_, action_)

        # z(t), z(t+1)
        z_ = torch.cat([z1_, z2_], dim=-1)
        z = z_[:, -2]
        next_z = z_[:, -1]
        # a(t)
        action = action_[:, -1]
        # fa(t)=(x(1:t), a(1:t-1)), fa(t+1)=(x(2:t+1), a(2:t))
        feature_action, next_feature_action = self.create_feature_actions(feature_, action_)

        self.update_critic(z, next_z, action, next_feature_action, reward, writer)
        self.update_actor(z, feature_action, writer)
        soft_update(self.critic_target, self.critic, self.tau)

    def update_critic(self, z, next_z, action, next_feature_action, reward, writer):
        curr_q1, curr_q2 = self.critic(z, action)
        with torch.no_grad():
            next_action, log_pi = self.actor.sample(next_feature_action)
            next_q1, next_q2 = self.critic_target(next_z, next_action)
            next_q = torch.min(next_q1, next_q2) - self.alpha * log_pi
        target_q = reward + self.gamma * next_q

        # Critic's losses are mean squared TD errors.
        loss_critic = (curr_q1 - target_q).pow_(2).mean() + (curr_q2 - target_q).pow_(2).mean()

        self.optim_critic.zero_grad()
        loss_critic.backward(retain_graph=False)
        self.optim_critic.step()

        if self.learning_steps_sac % 1000 == 0:
            writer.add_scalar("loss/critic", loss_critic.item(), self.learning_steps_sac)

    def update_actor(self, z, feature_action, writer):
        action, log_pi = self.actor.sample(feature_action)
        q1, q2 = self.critic(z, action)

        # Actor's objective is the maximization of (Q + alpha * entropy).
        loss_actor = torch.mean(-(torch.min(q1, q2) - self.alpha * log_pi))

        self.optim_actor.zero_grad()
        loss_actor.backward(retain_graph=False)
        self.optim_actor.step()

        # Intuitively, we increse alpha when entropy is less than target entropy, vice versa.
        entropy = -log_pi.detach_().mean()
        loss_alpha = -self.log_alpha * (self.target_entropy - entropy)

        self.optim_alpha.zero_grad()
        loss_alpha.backward(retain_graph=False)
        self.optim_alpha.step()

        with torch.no_grad():
            self.alpha = self.log_alpha.exp().item()

        if self.learning_steps_sac % 1000 == 0:
            writer.add_scalar("loss/actor", loss_actor.item(), self.learning_steps_sac)
            writer.add_scalar("loss/alpha", loss_alpha.item(), self.learning_steps_sac)
            writer.add_scalar("stats/alpha", self.alpha, self.learning_steps_sac)
            writer.add_scalar("stats/entropy", entropy.item(), self.learning_steps_sac)

    def save_model(self, save_dir):
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        # We don't save target network to reduce workloads.
        torch.save(self.latent.encoder.state_dict(), os.path.join(save_dir, "encoder.pth"))
        torch.save(self.latent.state_dict(), os.path.join(save_dir, "latent.pth"))
        torch.save(self.actor.state_dict(), os.path.join(save_dir, "actor.pth"))
        torch.save(self.critic.state_dict(), os.path.join(save_dir, "critic.pth"))