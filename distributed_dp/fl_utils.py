# Copyright 2021, Google LLC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utils for running experiments with discrete DP and compression."""

import pprint

from absl import logging
import numpy as np
import tensorflow_federated as tff

from distributed_dp import accounting_utils
from distributed_dp import ddpquery_utils
from distributed_dp import modular_clipping_factory


def get_total_dim(client_template):
  """Returns the dimension of the client template as a single vector."""
  return sum(np.prod(x.shape) for x in client_template)


def pad_dim(dim):
  return np.math.pow(2, np.ceil(np.log2(dim)))


def build_aggregator(compression_flags, dp_flags, num_clients,
                     num_clients_per_round, num_rounds, client_template):
  """Create a `tff.aggregator` containing all aggregation operations."""

  clip, epsilon = dp_flags['l2_norm_clip'], dp_flags['epsilon']
  # No DP (but still do the clipping if necessary).
  if epsilon is None or epsilon == -1:
    agg_factory = tff.aggregators.UnweightedMeanFactory()
    if clip is not None:
      assert clip > 0, 'Norm clip must be positive.'
      agg_factory = tff.aggregators.clipping_factory(clip, agg_factory)
    logging.info('Using vanilla aggregation with clipping %s', clip)
    params_dict = {'clip': clip}
    return agg_factory, params_dict

  # Parameters for DP
  assert epsilon > 0, f'Epsilon should be positive, found {epsilon}.'
  assert clip is not None and clip > 0, f'Clip must be positive, found {clip}.'
  sampling_rate = float(num_clients_per_round) / num_clients
  delta = dp_flags['delta'] or 1.0 / num_clients  # Default to delta = 1 / N.
  mechanism = dp_flags['dp_mechanism'].lower()
  dim = get_total_dim(client_template)

  params_dict = {
      'epsilon': epsilon,
      'delta': delta,
      'clip': clip,
      'dim': dim,
      'sampling_rate': sampling_rate,
      'mechanism': mechanism,
      'num_clients': num_clients,
      'num_clients_per_round': num_clients_per_round,
      'num_rounds': num_rounds
  }

  logging.info('Shared DP Parameters:')
  logging.info(pprint.pformat(params_dict))

  # Baseline: continuous Gaussian.
  if mechanism == 'gaussian':
    noise_mult = accounting_utils.get_gauss_noise_multiplier(
        target_eps=epsilon,
        target_delta=delta,
        target_sampling_rate=sampling_rate,
        steps=num_rounds)
    # Operations include clipping on client and noising + averaging on server;
    # No MeanFactory and ClippingFactory needed.
    agg_factory = tff.aggregators.DifferentiallyPrivateFactory.gaussian_fixed(
        noise_multiplier=noise_mult,
        clients_per_round=num_clients_per_round,
        clip=clip)
    gauss_params_dict = {'noise_mult': noise_mult}
    logging.info('Gaussian Parameters:')
    logging.info(gauss_params_dict)
    params_dict.update(gauss_params_dict)

  # Distributed Discrete Gaussian
  elif mechanism == 'ddgauss':
    padded_dim = pad_dim(dim)
    k_stddevs = compression_flags['k_stddevs'] or 4
    beta = compression_flags['beta']
    bits = compression_flags['num_bits']

    # Modular clipping has exclusive upper bound.
    mod_clip_lo, mod_clip_hi = -(2**(bits - 1)), 2**(bits - 1)

    gamma, local_stddev = accounting_utils.ddgauss_params(
        q=sampling_rate,
        epsilon=epsilon,
        l2_clip_norm=clip,
        bits=bits,
        num_clients=num_clients_per_round,
        dim=padded_dim,
        delta=delta,
        beta=beta,
        steps=num_rounds,
        k=k_stddevs)
    scale = 1.0 / gamma

    central_stddev = local_stddev * np.sqrt(num_clients_per_round)
    noise_mult_clip = central_stddev / clip
    inflated_l2 = accounting_utils.rounded_l2_norm_bound(
        clip * scale, beta=beta, dim=padded_dim) / scale
    noise_mult_inflated = central_stddev / inflated_l2

    discrete_params_dict = {
        'bits': bits,
        'beta': beta,
        'dim': dim,
        'padded_dim': padded_dim,
        'gamma': gamma,
        'scale': scale,
        'k_stddevs': k_stddevs,
        'local_stddev': local_stddev,
        'mechanism': mechanism,
        'inflated_l2': inflated_l2,
        'noise_mult_clip': noise_mult_clip,
        'noise_mult_inflated': noise_mult_inflated,
    }

    logging.info('DDGauss Parameters:')
    logging.info(pprint.pformat(discrete_params_dict))
    params_dict.update(discrete_params_dict)

    # Build nested aggregators.
    agg_factory = tff.aggregators.SumFactory()
    # 1. Modular clipping.
    agg_factory = modular_clipping_factory.ModularClippingSumFactory(
        clip_range_lower=mod_clip_lo,
        clip_range_upper=mod_clip_hi,
        inner_agg_factory=agg_factory)

    # 2. Quantization followed by the distributed DP mechanism.
    ddp_query = ddpquery_utils.build_ddp_query(
        mechanism=mechanism,
        local_stddev=local_stddev,
        l2_norm_bound=clip,
        beta=beta,
        padded_dim=padded_dim,
        scale=scale,
        client_template=client_template)

    agg_factory = tff.aggregators.DifferentiallyPrivateFactory(
        query=ddp_query, record_aggregation_factory=agg_factory)

    # 3. L2 norm clipping as the first step.
    agg_factory = tff.aggregators.clipping_factory(
        clipping_norm=clip, inner_agg_factory=agg_factory)

    # 4. Apply a MeanFactory at last (mean can't be part of the discrete
    # DPQueries (like the case of Gaussian) as the records may become floats
    # and hence break the decompression process).
    agg_factory = tff.aggregators.UnweightedMeanFactory(
        value_sum_factory=agg_factory)

  else:
    raise ValueError(f'Unsupported mechanism: {dp_flags["dp_mechanism"]}')

  return agg_factory, params_dict
