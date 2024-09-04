"""
Reward-weighted regression (RWR) for diffusion policy.

"""

import os
import pickle
import numpy as np
import torch
import logging
import wandb

log = logging.getLogger(__name__)
from util.timer import Timer
from agent.finetune.train_agent import TrainAgent
from util.scheduler import CosineAnnealingWarmupRestarts


class TrainRWRDiffusionAgent(TrainAgent):

    def __init__(self, cfg):
        super().__init__(cfg)

        # note the discount factor gamma here is applied to reward every act_steps, instead of every env step
        self.gamma = cfg.train.gamma

        # Build optimizer
        self.optimizer = torch.optim.AdamW(
            self.model.parameters(),
            lr=cfg.train.lr,
            weight_decay=cfg.train.weight_decay,
        )
        self.lr_scheduler = CosineAnnealingWarmupRestarts(
            self.optimizer,
            first_cycle_steps=cfg.train.lr_scheduler.first_cycle_steps,
            cycle_mult=1.0,
            max_lr=cfg.train.lr,
            min_lr=cfg.train.lr_scheduler.min_lr,
            warmup_steps=cfg.train.lr_scheduler.warmup_steps,
            gamma=1.0,
        )

        # Reward exponential
        self.beta = cfg.train.beta

        # Max weight for AWR
        self.max_reward_weight = cfg.train.max_reward_weight

        # Updates
        self.update_epochs = cfg.train.update_epochs

    def run(self):

        # Start training loop
        timer = Timer()
        run_results = []
        done_venv = np.zeros((1, self.n_envs))
        while self.itr < self.n_train_itr:

            # Prepare video paths for each envs --- only applies for the first set of episodes if allowing reset within iteration and each iteration has multiple episodes from one env
            options_venv = [{} for _ in range(self.n_envs)]
            if self.itr % self.render_freq == 0 and self.render_video:
                for env_ind in range(self.n_render):
                    options_venv[env_ind]["video_path"] = os.path.join(
                        self.render_dir, f"itr-{self.itr}_trial-{env_ind}.mp4"
                    )

            # Define train or eval - all envs restart
            eval_mode = self.itr % self.val_freq == 0 and not self.force_train
            self.model.eval() if eval_mode else self.model.train()
            firsts_trajs = np.zeros((self.n_steps + 1, self.n_envs))

            # Reset env at the beginning of an iteration
            if self.reset_at_iteration or eval_mode or last_itr_eval:
                prev_obs_venv = self.reset_env_all(options_venv=options_venv)
                firsts_trajs[0] = 1
            else:
                firsts_trajs[0] = (
                    done_venv  # if done at the end of last iteration, then the envs are just reset
                )
            last_itr_eval = eval_mode
            reward_trajs = np.empty((0, self.n_envs))

            # Holders
            obs_trajs = np.empty((0, self.n_envs, self.n_cond_step, self.obs_dim))
            samples_trajs = np.empty(
                (
                    0,
                    self.n_envs,
                    self.horizon_steps,
                    self.action_dim,
                )
            )

            # Collect a set of trajectories from env
            for step in range(self.n_steps):
                if step % 10 == 0:
                    print(f"Processed step {step} of {self.n_steps}")

                # Select action
                with torch.no_grad():
                    samples = (
                        self.model(
                            cond=torch.from_numpy(prev_obs_venv)
                            .float()
                            .to(self.device),
                            deterministic=eval_mode,
                        )
                        .cpu()
                        .numpy()
                    )  # n_env x horizon x act
                action_venv = samples[:, : self.act_steps]
                obs_trajs = np.vstack((obs_trajs, prev_obs_venv[None]))
                samples_trajs = np.vstack((samples_trajs, samples[None]))

                # Apply multi-step action
                obs_venv, reward_venv, done_venv, info_venv = self.venv.step(
                    action_venv
                )
                reward_trajs = np.vstack((reward_trajs, reward_venv[None]))
                firsts_trajs[step + 1] = done_venv
                prev_obs_venv = obs_venv

            # Summarize episode reward --- this needs to be handled differently depending on whether the environment is reset after each iteration. Only count episodes that finish within the iteration.
            episodes_start_end = []
            for env_ind in range(self.n_envs):
                env_steps = np.where(firsts_trajs[:, env_ind] == 1)[0]
                for i in range(len(env_steps) - 1):
                    start = env_steps[i]
                    end = env_steps[i + 1]
                    if end - start > 1:
                        episodes_start_end.append((env_ind, start, end - 1))
            if len(episodes_start_end) > 0:
                # Compute transitions for completed trajectories
                obs_trajs_split = [
                    obs_trajs[start : end + 1, env_ind]
                    for env_ind, start, end in episodes_start_end
                ]
                samples_trajs_split = [
                    samples_trajs[start : end + 1, env_ind]
                    for env_ind, start, end in episodes_start_end
                ]
                reward_trajs_split = [
                    reward_trajs[start : end + 1, env_ind]
                    for env_ind, start, end in episodes_start_end
                ]
                num_episode_finished = len(reward_trajs_split)

                # Compute episode returns
                discounted_reward_trajs_split = [
                    [
                        self.gamma**t * r
                        for t, r in zip(
                            list(range(end - start + 1)),
                            reward_trajs[start : end + 1, env_ind],
                        )
                    ]
                    for env_ind, start, end in episodes_start_end
                ]
                returns_trajs_split = [
                    np.cumsum(y[::-1])[::-1] for y in discounted_reward_trajs_split
                ]
                returns_trajs_split = np.concatenate(returns_trajs_split)
                episode_reward = np.array(
                    [np.sum(reward_traj) for reward_traj in reward_trajs_split]
                )
                episode_best_reward = np.array(
                    [
                        np.max(reward_traj) / self.act_steps
                        for reward_traj in reward_trajs_split
                    ]
                )
                avg_episode_reward = np.mean(episode_reward)
                avg_best_reward = np.mean(episode_best_reward)
                success_rate = np.mean(
                    episode_best_reward >= self.best_reward_threshold_for_success
                )
            else:
                episode_reward = np.array([])
                num_episode_finished = 0
                avg_episode_reward = 0
                avg_best_reward = 0
                success_rate = 0
                log.info("[WARNING] No episode completed within the iteration!")

            # Update
            if not eval_mode:

                # Tensorize data and put them to device
                # k for environment step
                obs_k = (
                    torch.tensor(np.concatenate(obs_trajs_split))
                    .float()
                    .to(self.device)
                )

                samples_k = (
                    torch.tensor(np.concatenate(samples_trajs_split))
                    .float()
                    .to(self.device)
                )

                # Normalize reward
                returns_trajs_split = (
                    returns_trajs_split - np.mean(returns_trajs_split)
                ) / (returns_trajs_split.std() + 1e-3)

                rewards_k = (
                    torch.tensor(returns_trajs_split)
                    .float()
                    .to(self.device)
                    .reshape(-1)
                )

                rewards_k_scaled = torch.exp(self.beta * rewards_k)
                rewards_k_scaled.clamp_(max=self.max_reward_weight)

                # rewards_k_scaled = rewards_k_scaled / rewards_k_scaled.mean()

                # Update policy and critic
                total_steps = len(rewards_k_scaled)
                inds_k = np.arange(total_steps)
                for _ in range(self.update_epochs):

                    # for each epoch, go through all data in batches
                    np.random.shuffle(inds_k)
                    num_batch = max(1, total_steps // self.batch_size)  # skip last ones
                    for batch in range(num_batch):
                        start = batch * self.batch_size
                        end = start + self.batch_size
                        inds_b = inds_k[start:end]  # b for batch
                        obs_b = obs_k[inds_b]
                        samples_b = samples_k[inds_b]
                        rewards_b = rewards_k_scaled[inds_b]

                        # Update policy with collected trajectories
                        loss = self.model.loss(
                            samples_b,
                            obs_b,
                            rewards_b,
                        )
                        self.optimizer.zero_grad()
                        loss.backward()
                        if self.max_grad_norm is not None:
                            torch.nn.utils.clip_grad_norm_(
                                self.model.parameters(), self.max_grad_norm
                            )
                        self.optimizer.step()

            # Update lr
            self.lr_scheduler.step()

            # Save model
            if self.itr % self.save_model_freq == 0 or self.itr == self.n_train_itr - 1:
                self.save_model()

            # Log loss and save metrics
            run_results.append(
                {
                    "itr": self.itr,
                }
            )
            if self.itr % self.log_freq == 0:
                if eval_mode:
                    log.info(
                        f"eval: success rate {success_rate:8.4f} | avg episode reward {avg_episode_reward:8.4f} | avg best reward {avg_best_reward:8.4f}"
                    )
                    if self.use_wandb:
                        wandb.log(
                            {
                                "success rate - eval": success_rate,
                                "avg episode reward - eval": avg_episode_reward,
                                "avg best reward - eval": avg_best_reward,
                                "num episode - eval": num_episode_finished,
                            },
                            step=self.itr,
                            commit=False,
                        )
                    run_results[-1]["eval_success_rate"] = success_rate
                    run_results[-1]["eval_episode_reward"] = avg_episode_reward
                    run_results[-1]["eval_best_reward"] = avg_best_reward
                else:
                    log.info(
                        f"{self.itr}: loss {loss:8.4f} | reward {avg_episode_reward:8.4f} |t:{timer():8.4f}"
                    )
                    if self.use_wandb:
                        wandb.log(
                            {
                                "loss": loss,
                                "avg episode reward - train": avg_episode_reward,
                                "num episode - train": num_episode_finished,
                            },
                            step=self.itr,
                            commit=True,
                        )
                    run_results[-1]["loss"] = loss
                    run_results[-1]["train_episode_reward"] = avg_episode_reward
                run_results[-1]["time"] = timer()
                with open(self.result_path, "wb") as f:
                    pickle.dump(run_results, f)
            self.itr += 1