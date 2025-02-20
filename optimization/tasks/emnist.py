# Copyright 2019, Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Federated EMNIST character recognition library using TFF."""

import functools

import tensorflow as tf
import tensorflow_federated as tff

from optimization.tasks import training_specs
from utils.datasets import emnist_dataset
from utils.models import emnist_models

EMNIST_MODELS = ['cnn', '2nn', '1m_cnn']


def configure_training(task_spec: training_specs.TaskSpec,
                       model: str = 'cnn') -> training_specs.RunnerSpec:
  """Configures training for the EMNIST character recognition task.

  This method will load and pre-process datasets and construct a model used for
  the task. It then uses `iterative_process_builder` to create an iterative
  process compatible with `federated_research.utils.training_loop`.

  Args:
    task_spec: A `TaskSpec` class for creating federated training tasks.
    model: A string specifying the model used for character recognition. Can be
      one of `cnn`, `2nn`, or `1m_cnn`, corresponding to a simple CNN model,
      a densely connected 2-layer model, and a CNN model with roughly 1 miilion
      (< 2^20) parameters, respectively.

  Returns:
    A `RunnerSpec` containing attributes used for running the newly created
    federated task.
  """
  emnist_task = 'digit_recognition'
  emnist_train, _ = tff.simulation.datasets.emnist.load_data(only_digits=False)
  _, emnist_test = emnist_dataset.get_centralized_datasets(
      only_digits=False, emnist_task=emnist_task)

  train_preprocess_fn = emnist_dataset.create_preprocess_fn(
      num_epochs=task_spec.client_epochs_per_round,
      batch_size=task_spec.client_batch_size,
      emnist_task=emnist_task)
  emnist_train = emnist_train.preprocess(train_preprocess_fn)
  input_spec = emnist_train.element_type_structure

  if model == 'cnn':
    model_builder = functools.partial(
        emnist_models.create_conv_dropout_model, only_digits=False)
  elif model == '2nn':
    model_builder = functools.partial(
        emnist_models.create_two_hidden_layer_model, only_digits=False)
  elif model == '1m_cnn':
    model_builder = functools.partial(
        emnist_models.create_1m_cnn_model, only_digits=False)
  else:
    raise ValueError(
        'Cannot handle model flag [{!s}], must be one of {!s}.'.format(
            model, EMNIST_MODELS))

  loss_builder = tf.keras.losses.SparseCategoricalCrossentropy
  metrics_builder = lambda: [tf.keras.metrics.SparseCategoricalAccuracy()]

  def tff_model_fn() -> tff.learning.Model:
    return tff.learning.from_keras_model(
        keras_model=model_builder(),
        input_spec=input_spec,
        loss=loss_builder(),
        metrics=metrics_builder())

  iterative_process = task_spec.iterative_process_builder(tff_model_fn)
  training_process = tff.simulation.compose_dataset_computation_with_iterative_process(
      emnist_train.dataset_computation, iterative_process)
  client_ids_fn = functools.partial(
      tff.simulation.build_uniform_sampling_fn(
          emnist_train.client_ids,
          replace=False,
          random_seed=task_spec.client_datasets_random_seed),
      size=task_spec.clients_per_round)
  # We convert the output to a list (instead of an np.ndarray) so that it can
  # be used as input to the iterative process.
  client_sampling_fn = lambda x: list(client_ids_fn(x))

  training_process.get_model_weights = iterative_process.get_model_weights

  evaluate_fn = tff.learning.build_federated_evaluation(tff_model_fn)

  def test_fn(state):
    return evaluate_fn(
        iterative_process.get_model_weights(state), [emnist_test])

  def validation_fn(state, round_num):
    del round_num
    return evaluate_fn(
        iterative_process.get_model_weights(state), [emnist_test])

  return training_specs.RunnerSpec(
      iterative_process=training_process,
      client_datasets_fn=client_sampling_fn,
      validation_fn=validation_fn,
      test_fn=test_fn)
