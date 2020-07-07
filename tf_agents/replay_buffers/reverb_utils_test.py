# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Tests for tf_agents.replay_buffers.reverb_utils."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import functools

from absl.testing import parameterized
import mock
import reverb
from six.moves import range
import tensorflow.compat.v2 as tf

from tf_agents.drivers import py_driver
from tf_agents.environments import parallel_py_environment
from tf_agents.environments import test_envs
from tf_agents.policies import random_py_policy
from tf_agents.replay_buffers import reverb_replay_buffer
from tf_agents.replay_buffers import reverb_utils
from tf_agents.specs import tensor_spec
from tf_agents.system import system_multiprocessing as multiprocessing
from tf_agents.trajectories import time_step as ts
from tf_agents.utils import test_utils


class ReverbTableTests(test_utils.TestCase):

  def test_queue_table(self):
    table_name = 'test_queue_table'
    queue_table = reverb.Table.queue(table_name, 3)
    reverb_server = reverb.Server([queue_table])
    data_spec = tensor_spec.TensorSpec((), dtype=tf.int64)
    replay = reverb_replay_buffer.ReverbReplayBuffer(
        data_spec,
        table_name,
        local_server=reverb_server,
        sequence_length=1,
        dataset_buffer_size=1)

    with replay.py_client.writer(max_sequence_length=1) as writer:
      for i in range(3):
        writer.append(i)
        writer.create_item(
            table=table_name, num_timesteps=1, priority=1)

    dataset = replay.as_dataset(
        sample_batch_size=1, num_steps=None, num_parallel_calls=None)

    iterator = iter(dataset)
    for i in range(3):
      sample = next(iterator)[0]
      self.assertEqual(sample, i)

  def test_uniform_table(self):
    table_name = 'test_uniform_table'
    queue_table = reverb.Table(
        table_name,
        sampler=reverb.selectors.Uniform(),
        remover=reverb.selectors.Fifo(),
        max_size=1000,
        rate_limiter=reverb.rate_limiters.MinSize(3))
    reverb_server = reverb.Server([queue_table])
    data_spec = tensor_spec.TensorSpec((), dtype=tf.int64)
    replay = reverb_replay_buffer.ReverbReplayBuffer(
        data_spec,
        table_name,
        local_server=reverb_server,
        sequence_length=1,
        dataset_buffer_size=1)

    with replay.py_client.writer(max_sequence_length=1) as writer:
      for i in range(3):
        writer.append(i)
        writer.create_item(
            table=table_name, num_timesteps=1, priority=1)

    dataset = replay.as_dataset(
        sample_batch_size=1, num_steps=None, num_parallel_calls=None)

    iterator = iter(dataset)
    counts = [0] * 3
    for i in range(1000):
      item_0 = next(iterator)[0].numpy()  # This is a matrix shaped 1x1.
      counts[int(item_0)] += 1

    # Comparing against 200 to avoid flakyness
    self.assertGreater(counts[0], 200)
    self.assertGreater(counts[1], 200)
    self.assertGreater(counts[2], 200)

  def test_uniform_table_max_sample(self):
    table_name = 'test_uniform_table'
    table = reverb.Table(
        table_name,
        sampler=reverb.selectors.Uniform(),
        remover=reverb.selectors.Fifo(),
        max_size=3,
        max_times_sampled=10,
        rate_limiter=reverb.rate_limiters.MinSize(1))
    reverb_server = reverb.Server([table])
    data_spec = tensor_spec.TensorSpec((), dtype=tf.int64)
    replay = reverb_replay_buffer.ReverbReplayBuffer(
        data_spec,
        table_name,
        local_server=reverb_server,
        sequence_length=1,
        dataset_buffer_size=1)

    with replay.py_client.writer(max_sequence_length=1) as writer:
      for i in range(3):
        writer.append(i)
        writer.create_item(table_name, num_timesteps=1, priority=1)

    dataset = replay.as_dataset(sample_batch_size=3)

    self.assertTrue(table.can_sample(3))
    iterator = iter(dataset)
    counts = [0] * 3
    for i in range(10):
      item_0 = next(iterator)[0].numpy()  # This is a matrix shaped 1x3.
      for item in item_0:
        counts[int(item)] += 1
    self.assertFalse(table.can_sample(3))

    # Same number of counts due to limit on max_times_sampled
    self.assertEqual(counts[0], 10)
    self.assertEqual(counts[1], 10)
    self.assertEqual(counts[2], 10)

  def test_prioritized_table(self):
    table_name = 'test_prioritized_table'
    queue_table = reverb.Table(
        table_name,
        sampler=reverb.selectors.Prioritized(1.0),
        remover=reverb.selectors.Fifo(),
        rate_limiter=reverb.rate_limiters.MinSize(1),
        max_size=3)
    reverb_server = reverb.Server([queue_table])
    data_spec = tensor_spec.TensorSpec((), dtype=tf.int64)
    replay = reverb_replay_buffer.ReverbReplayBuffer(
        data_spec,
        table_name,
        sequence_length=1,
        local_server=reverb_server,
        dataset_buffer_size=1)

    with replay.py_client.writer(max_sequence_length=1) as writer:
      for i in range(3):
        writer.append(i)
        writer.create_item(
            table=table_name, num_timesteps=1, priority=i)

    dataset = replay.as_dataset(
        sample_batch_size=1, num_steps=None, num_parallel_calls=None)

    iterator = iter(dataset)
    counts = [0] * 3
    for i in range(1000):
      item_0 = next(iterator)[0].numpy()  # This is a matrix shaped 1x1.
      counts[int(item_0)] += 1

    self.assertEqual(counts[0], 0)  # priority 0
    self.assertGreater(counts[1], 250)  # priority 1
    self.assertGreater(counts[2], 600)  # priority 2

  def test_prioritized_table_max_sample(self):
    table_name = 'test_prioritized_table'
    table = reverb.Table(
        table_name,
        sampler=reverb.selectors.Prioritized(1.0),
        remover=reverb.selectors.Fifo(),
        max_times_sampled=10,
        rate_limiter=reverb.rate_limiters.MinSize(1),
        max_size=3)
    reverb_server = reverb.Server([table])
    data_spec = tensor_spec.TensorSpec((), dtype=tf.int64)
    replay = reverb_replay_buffer.ReverbReplayBuffer(
        data_spec,
        table_name,
        sequence_length=1,
        local_server=reverb_server,
        dataset_buffer_size=1)

    with replay.py_client.writer(max_sequence_length=1) as writer:
      for i in range(3):
        writer.append(i)
        writer.create_item(table_name, num_timesteps=1, priority=i)

    dataset = replay.as_dataset(sample_batch_size=3)

    self.assertTrue(table.can_sample(3))
    iterator = iter(dataset)
    counts = [0] * 3
    for i in range(10):
      item_0 = next(iterator)[0].numpy()  # This is a matrix shaped 1x3.
      for item in item_0:
        counts[int(item)] += 1
    self.assertFalse(table.can_sample(3))

    # Same number of counts due to limit on max_times_sampled
    self.assertEqual(counts[0], 10)  # priority 0
    self.assertEqual(counts[1], 10)  # priority 1
    self.assertEqual(counts[2], 10)  # priority 2


def _create_add_trajectory_observer_fn(*args, **kwargs):

  @contextlib.contextmanager
  def _create_and_yield(client):
    yield reverb_utils.ReverbAddTrajectoryObserver(client, *args, **kwargs)

  return _create_and_yield


def _create_add_episode_observer_fn(*args, **kwargs):

  @contextlib.contextmanager
  def _create_and_yield(client):
    yield reverb_utils.ReverbAddEpisodeObserver(client, *args, **kwargs)

  return _create_and_yield


def _env_creator(episode_len=3):
  return functools.partial(test_envs.CountingEnv, steps_per_episode=episode_len)


def _create_env_spec(episode_len=3):
  return ts.time_step_spec(_env_creator(episode_len)().observation_spec())


def _parallel_env_creator(collection_batch_size=1, episode_len=3):
  return functools.partial(
      parallel_py_environment.ParallelPyEnvironment,
      env_constructors=[
          _env_creator(episode_len) for _ in range(collection_batch_size)
      ])


def _create_random_policy_from_env(env):
  return random_py_policy.RandomPyPolicy(
      ts.time_step_spec(env.observation_spec()), env.action_spec())


class ReverbObserverTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self._client = mock.MagicMock()
    self._writer = mock.MagicMock()
    self._client.writer = self._writer
    self._writer.return_value = self._writer

  @parameterized.named_parameters(
      ('add_trajectory_observer',
       _create_add_trajectory_observer_fn(
           table_names=['test_table'],
           sequence_lengths=[2]), _env_creator(3),
       3,   # expected_items
       2,   # writer_call_counts
       4,   # max_steps
       5),  # append_count
      ('add_trajectory_episode_observer',
       _create_add_episode_observer_fn(
           table_name='test_table', max_sequence_length=8,
           priority=3), _env_creator(3),
       2,    # expected_items
       3,    # writer_call_counts
       8,    # max_steps
       10),  # append_count
      ('add_trajectory_observer_stride2',
       _create_add_trajectory_observer_fn(
           table_names=['test_table'], sequence_lengths=[2],
           stride_lengths=[2]), _env_creator(3),
       2,   # expected_items
       2,   # writer_call_counts
       4,   # max_steps
       5))  # append_count
  def test_observer_writes(self, create_observer_fn, env_fn, expected_items,
                           writer_call_counts, max_steps, append_count):
    env = env_fn()
    with create_observer_fn(self._client) as observer:
      policy = _create_random_policy_from_env(env)
      driver = py_driver.PyDriver(
          env, policy, observers=[observer], max_steps=max_steps)
      driver.run(env.reset())

    self.assertEqual(writer_call_counts, self._writer.call_count)
    self.assertEqual(append_count, self._writer.append.call_count)
    self.assertEqual(expected_items, self._writer.create_item.call_count)

  @parameterized.named_parameters(
      ('add_trajectory_observer',
       _create_add_trajectory_observer_fn(
           table_names=['test_table1', 'test_table2'],
           sequence_lengths=[2, 3]), _env_creator(3),
       5,    # expected_items
       4,    # writer_call_counts
       4,    # max_steps
       10))  # append_count
  def test_observer_writes_multiple_tables(self, create_observer_fn, env_fn,
                                           expected_items, writer_call_counts,
                                           max_steps, append_count):
    env = env_fn()
    with create_observer_fn(self._client) as observer:
      policy = _create_random_policy_from_env(env)
      driver = py_driver.PyDriver(
          env, policy, observers=[observer], max_steps=max_steps)
      driver.run(env.reset())

    self.assertEqual(writer_call_counts, self._writer.call_count)
    self.assertEqual(append_count, self._writer.append.call_count)
    self.assertEqual(expected_items, self._writer.create_item.call_count)

  def test_episodic_observer_assert_priority_is_numeric(self):
    with self.assertRaisesRegex(ValueError,
                                r'`priority` must be a numeric value'):
      reverb_utils.ReverbAddEpisodeObserver(
          self._client,
          table_name='test_table1',
          max_sequence_length=8,
          priority=[3])

  def test_episodic_observer_assert_table_is_string(self):
    with self.assertRaisesRegex(ValueError,
                                r'`table_name` must be a string.'):
      _ = reverb_utils.ReverbAddEpisodeObserver(
          self._client,
          table_name=['test_table'],
          max_sequence_length=8,
          priority=3)

  def test_episodic_observer_overflow_episode_bypass(self):
    env1 = _env_creator(3)()
    env2 = _env_creator(4)()
    with _create_add_episode_observer_fn(
        table_name='test_table', max_sequence_length=4,
        priority=1,
        bypass_partial_episodes=True)(self._client) as observer:
      policy = _create_random_policy_from_env(env1)
      # env1 -> writes only ONE episode. Note that `max_sequence_length`
      # must be one more than episode length. As in TF-Agents, we append
      # a trajectory as the `LAST` step.
      driver = py_driver.PyDriver(
          env1, policy, observers=[observer], max_steps=6)
      driver.run(env1.reset())
      # env2 -> writes NO episodes (all of them has length >
      # `max_sequence_length`)
      policy = _create_random_policy_from_env(env2)
      driver = py_driver.PyDriver(
          env2, policy, observers=[observer], max_steps=6)
      driver.run(env2.reset())
    self.assertEqual(1, self._writer.create_item.call_count)

  def test_episodic_observer_overflow_episode_raise_value_error(self):
    env = _env_creator(3)()
    with _create_add_episode_observer_fn(
        table_name='test_table', max_sequence_length=2,
        priority=1)(self._client) as observer:
      policy = _create_random_policy_from_env(env)
      driver = py_driver.PyDriver(
          env, policy, observers=[observer], max_steps=4)
      with self.assertRaises(ValueError):
        driver.run(env.reset())

  def test_episodic_observer_assert_sequence_length_positive(self):
    with self.assertRaises(ValueError):
      _ = reverb_utils.ReverbAddEpisodeObserver(
          self._client,
          table_name='test_table',
          max_sequence_length=-1,
          priority=3)

  def test_episodic_observer_update_priority(self):
    observer = reverb_utils.ReverbAddEpisodeObserver(
        self._client,
        table_name='test_table',
        max_sequence_length=1,
        priority=3)
    self.assertEqual(observer._priority, 3)
    observer.update_priority(4)
    self.assertEqual(observer._priority, 4)

  def test_episodic_observer_assert_update_priority_numeric(self):
    observer = reverb_utils.ReverbAddEpisodeObserver(
        self._client,
        table_name='test_table',
        max_sequence_length=1,
        priority=3)
    self.assertEqual(observer._priority, 3)
    with self.assertRaises(ValueError):
      observer.update_priority([4])

  def test_episodic_observer_num_steps(self):
    create_observer_fn = _create_add_episode_observer_fn(
        table_name='test_table',
        max_sequence_length=8,
        priority=3)
    env = _env_creator(3)()
    with create_observer_fn(self._client) as observer:
      policy = _create_random_policy_from_env(env)
      driver = py_driver.PyDriver(
          env, policy, observers=[observer], max_steps=10)
      driver.run(env.reset())
      # After each episode, we reset `cached_steps`.
      # We run the driver for 3 full episode and one step.
      self.assertEqual(observer._cached_steps, 1)


if __name__ == '__main__':
  multiprocessing.handle_test_main(tf.test.main)