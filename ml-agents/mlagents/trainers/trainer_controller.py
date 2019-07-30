# # Unity ML-Agents Toolkit
# ## ML-Agent Learning
"""Launches trainers for each External Brains in a Unity Environment."""

import os
import json
import logging
from typing import *

import numpy as np
import tensorflow as tf
from time import time

from mlagents.envs import BrainParameters
from mlagents.envs.env_manager import EnvManager, AgentStep
from mlagents.envs.exception import UnityEnvironmentException
from mlagents.envs.timers import hierarchical_timer, get_timer_tree, timed
from mlagents.trainers import Trainer, TrainerMetrics
from mlagents.trainers.ppo.trainer import PPOTrainer
from mlagents.trainers.bc.offline_trainer import OfflineBCTrainer
from mlagents.trainers.bc.online_trainer import OnlineBCTrainer
from mlagents.trainers.meta_curriculum import MetaCurriculum


class TrainerController(object):
    def __init__(
        self,
        model_path: str,
        summaries_dir: str,
        run_id: str,
        save_freq: int,
        meta_curriculum: Optional[MetaCurriculum],
        load: bool,
        train: bool,
        keep_checkpoints: int,
        lesson: Optional[int],
        training_seed: int,
        fast_simulation: bool,
    ):
        """
        :param model_path: Path to save the model.
        :param summaries_dir: Folder to save training summaries.
        :param run_id: The sub-directory name for model and summary statistics
        :param save_freq: Frequency at which to save model
        :param meta_curriculum: MetaCurriculum object which stores information about all curricula.
        :param load: Whether to load the model or randomly initialize.
        :param train: Whether to train model, or only run inference.
        :param keep_checkpoints: How many model checkpoints to keep.
        :param lesson: Start learning from this lesson.
        :param training_seed: Seed to use for Numpy and Tensorflow random number generation.
        """

        self.model_path = model_path
        self.summaries_dir = summaries_dir
        self.logger = logging.getLogger("mlagents.envs")
        self.run_id = run_id
        self.save_freq = save_freq
        self.lesson = lesson
        self.load_model = load
        self.train_model = train
        self.keep_checkpoints = keep_checkpoints
        self.trainers: Dict[str, Trainer] = {}
        self.trainer_metrics: Dict[str, TrainerMetrics] = {}
        self.meta_curriculum = meta_curriculum
        self.seed = training_seed
        self.training_start_time = time()
        self.fast_simulation = fast_simulation
        np.random.seed(self.seed)
        tf.set_random_seed(self.seed)

    def _get_measure_vals(self):
        brain_names_to_measure_vals = {}
        if self.meta_curriculum:
            for (
                brain_name,
                curriculum,
            ) in self.meta_curriculum.brains_to_curriculums.items():
                if curriculum.measure == "progress":
                    measure_val = (
                        self.trainers[brain_name].get_step
                        / self.trainers[brain_name].get_max_steps
                    )
                    brain_names_to_measure_vals[brain_name] = measure_val
                elif curriculum.measure == "reward":
                    measure_val = np.mean(self.trainers[brain_name].reward_buffer)
                    brain_names_to_measure_vals[brain_name] = measure_val
        else:
            for brain_name, trainer in self.trainers.items():
                measure_val = np.mean(trainer.reward_buffer)
                brain_names_to_measure_vals[brain_name] = measure_val
        return brain_names_to_measure_vals

    def _save_model(self):
        """
        Saves current model to checkpoint folder.
        """
        for brain_name in self.trainers.keys():
            self.trainers[brain_name].save_model()
        self.logger.info("Saved Model")

    def _save_model_when_interrupted(self):
        self.logger.info(
            "Learning was interrupted. Please wait while the graph is generated."
        )
        self._save_model()

    def _write_training_metrics(self):
        """
        Write all CSV metrics
        :return:
        """
        for brain_name in self.trainers.keys():
            if brain_name in self.trainer_metrics:
                self.trainers[brain_name].write_training_metrics()

    def _write_timing_tree(self) -> None:
        timing_path = f"{self.summaries_dir}/{self.run_id}_timers.json"
        try:
            with open(timing_path, "w") as f:
                json.dump(get_timer_tree(), f, indent=2)
        except FileNotFoundError:
            self.logger.warning(
                f"Unable to save to {timing_path}. Make sure the directory exists"
            )

    def _export_graph(self):
        """
        Exports latest saved models to .nn format for Unity embedding.
        """
        for brain_name in self.trainers.keys():
            self.trainers[brain_name].export_model()

    def initialize_trainers(
        self,
        trainer_config: Dict[str, Any],
        external_brains: Dict[str, BrainParameters],
    ) -> None:
        """
        Initialization of the trainers
        :param trainer_config: The configurations of the trainers
        """
        trainer_parameters_dict = {}
        for brain_name in external_brains:
            trainer_parameters = trainer_config["default"].copy()
            trainer_parameters["summary_path"] = "{basedir}/{name}".format(
                basedir=self.summaries_dir, name=str(self.run_id) + "_" + brain_name
            )
            trainer_parameters["model_path"] = "{basedir}/{name}".format(
                basedir=self.model_path, name=brain_name
            )
            trainer_parameters["keep_checkpoints"] = self.keep_checkpoints
            if brain_name in trainer_config:
                _brain_key: Any = brain_name
                while not isinstance(trainer_config[_brain_key], dict):
                    _brain_key = trainer_config[_brain_key]
                trainer_parameters.update(trainer_config[_brain_key])
            trainer_parameters_dict[brain_name] = trainer_parameters.copy()
        for brain_name in external_brains:
            if trainer_parameters_dict[brain_name]["trainer"] == "offline_bc":
                self.trainers[brain_name] = OfflineBCTrainer(
                    brain=external_brains[brain_name],
                    trainer_parameters=trainer_parameters_dict[brain_name],
                    training=self.train_model,
                    load=self.load_model,
                    seed=self.seed,
                    run_id=self.run_id,
                )
            elif trainer_parameters_dict[brain_name]["trainer"] == "online_bc":
                self.trainers[brain_name] = OnlineBCTrainer(
                    brain=external_brains[brain_name],
                    trainer_parameters=trainer_parameters_dict[brain_name],
                    training=self.train_model,
                    load=self.load_model,
                    seed=self.seed,
                    run_id=self.run_id,
                )
            elif trainer_parameters_dict[brain_name]["trainer"] == "ppo":
                self.trainers[brain_name] = PPOTrainer(
                    brain=external_brains[brain_name],
                    reward_buff_cap=self.meta_curriculum.brains_to_curriculums[
                        brain_name
                    ].min_lesson_length
                    if self.meta_curriculum
                    else 1,
                    trainer_parameters=trainer_parameters_dict[brain_name],
                    training=self.train_model,
                    load=self.load_model,
                    seed=self.seed,
                    run_id=self.run_id,
                )
                self.trainer_metrics[brain_name] = self.trainers[
                    brain_name
                ].trainer_metrics
            else:
                raise UnityEnvironmentException(
                    "The trainer config contains "
                    "an unknown trainer type for "
                    "brain {}".format(brain_name)
                )

    @staticmethod
    def _create_model_path(model_path):
        try:
            if not os.path.exists(model_path):
                os.makedirs(model_path)
        except Exception:
            raise UnityEnvironmentException(
                "The folder {} containing the "
                "generated model could not be "
                "accessed. Please make sure the "
                "permissions are set correctly.".format(model_path)
            )

    def _reset_env(self, env: EnvManager) -> List[AgentStep]:
        """Resets the environment.

        Returns:
            A Data structure corresponding to the initial reset state of the
            environment.
        """
        if self.meta_curriculum is not None:
            return env.reset(
                train_mode=self.fast_simulation,
                config=self.meta_curriculum.get_config(),
            )
        else:
            return env.reset(train_mode=self.fast_simulation)

    def _should_save_model(self, global_step: int) -> bool:
        return (
            global_step % self.save_freq == 0 and global_step != 0 and self.train_model
        )

    def _not_done_training(self) -> bool:
        return (
            any([t.get_step <= t.get_max_steps for k, t in self.trainers.items()])
            or not self.train_model
        )

    def write_to_tensorboard(self, global_step: int) -> None:
        for brain_name, trainer in self.trainers.items():
            # Write training statistics to Tensorboard.
            delta_train_start = time() - self.training_start_time
            if self.meta_curriculum is not None:
                trainer.write_summary(
                    global_step,
                    delta_train_start,
                    lesson_num=self.meta_curriculum.brains_to_curriculums[
                        brain_name
                    ].lesson_num,
                )
            else:
                trainer.write_summary(global_step, delta_train_start)

    def start_learning(
        self, env_manager: EnvManager, trainer_config: Dict[str, Any]
    ) -> None:
        # TODO: Should be able to start learning at different lesson numbers
        # for each curriculum.
        if self.meta_curriculum is not None:
            self.meta_curriculum.set_all_curriculums_to_lesson_num(self.lesson)
        self._create_model_path(self.model_path)

        tf.reset_default_graph()

        # Prevent a single session from taking all GPU memory.
        self.initialize_trainers(trainer_config, env_manager.external_brains)
        for _, t in self.trainers.items():
            self.logger.info(t)

        global_step = 0

        if self.train_model:
            for brain_name, trainer in self.trainers.items():
                trainer.write_tensorboard_text("Hyperparameters", trainer.parameters)
        try:
            for brain_name, trainer in self.trainers.items():
                env_manager.set_policy(brain_name, trainer.policy)
            self._reset_env(env_manager)
            while self._not_done_training():
                n_steps = self.advance(env_manager)
                for i in range(n_steps):
                    global_step += 1
                    if self._should_save_model(global_step):
                        # Save Tensorflow model
                        self._save_model()
                    self.write_to_tensorboard(global_step)
            # Final save Tensorflow model
            if global_step != 0 and self.train_model:
                self._save_model()
        except KeyboardInterrupt:
            if self.train_model:
                self._save_model_when_interrupted()
            pass
        env_manager.close()
        if self.train_model:
            self._write_training_metrics()
            self._export_graph()
        self._write_timing_tree()

    @timed
    def advance(self, env: EnvManager) -> int:
        if self.meta_curriculum:
            # Get the sizes of the reward buffers.
            reward_buff_sizes = {
                k: len(t.reward_buffer) for (k, t) in self.trainers.items()
            }
            # Attempt to increment the lessons of the brains who
            # were ready.
            lessons_incremented = self.meta_curriculum.increment_lessons(
                self._get_measure_vals(), reward_buff_sizes=reward_buff_sizes
            )
            # If any lessons were incremented or the environment is
            # ready to be reset
            if any(lessons_incremented.values()):
                self._reset_env(env)
                for brain_name, trainer in self.trainers.items():
                    trainer.end_episode()
                for brain_name, changed in lessons_incremented.items():
                    if changed:
                        self.trainers[brain_name].reward_buffer.clear()

        with hierarchical_timer("env_step"):
            time_start_step = time()
            new_agent_steps = env.step()
            new_env_steps = env.num_env_steps_returned
            delta_time_step = time() - time_start_step

        for agent_step in new_agent_steps:
            for brain_name, trainer in self.trainers.items():
                trainer.add_experiences(agent_step)
                trainer.process_experiences(agent_step)
        for brain_name, trainer in self.trainers.items():
            if brain_name in self.trainer_metrics:
                self.trainer_metrics[brain_name].add_delta_step(delta_time_step)
            if self.train_model and trainer.get_step <= trainer.get_max_steps:
                trainer.increment_step(new_env_steps)
                if trainer.is_ready_update():
                    # Perform gradient descent with experience buffer
                    with hierarchical_timer("update_policy"):
                        trainer.update_policy()
                    env.set_policy(brain_name, trainer.policy)
        return new_env_steps
