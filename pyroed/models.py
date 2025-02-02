from collections import OrderedDict
from typing import Dict, Optional

import pyro
import pyro.distributions as dist
import torch

from .typing import Blocks, Coefs, Schema, validate


def linear_response(
    schema: Schema,
    coefs: Coefs,
    sequence: torch.Tensor,
    extra_features: Optional[torch.Tensor],
) -> torch.Tensor:
    """
    Linear response function.

    :param OrderedDict schema: A schema dict.
    :param dict coefs: A dictionary mapping feature tuples to coefficient
        tensors.
    :param torch.Tensor sequence: A tensor representing a sequence.
    :param torch.Tensor extra_features: An optional tensor of extra features,
        i.e. those computed by a custom ``features_fn`` rather than standard
        cross features from ``FEATURE_BLOCKS``.
    :returns: The response.
    :rtype: torch.Tensor
    """
    if not torch._C._get_tracing_state():
        assert isinstance(schema, OrderedDict)
        assert isinstance(coefs, dict)
        assert sequence.dtype == torch.long
        assert sequence.size(-1) == len(schema)
        if extra_features is None:
            assert None not in coefs
        else:
            assert None in coefs
            assert coefs[None].dim() == 1
            assert extra_features.shape == sequence.shape[:-1] + coefs[None].shape
        assert (extra_features is not None) == (None in coefs)
    choices = dict(zip(schema, sequence.unbind(-1)))

    result = torch.tensor(0.0)
    for key, coef in coefs.items():
        if key is None:
            assert extra_features is not None
            result = result + extra_features @ coefs[None]
        else:
            if not torch._C._get_tracing_state():
                assert isinstance(key, tuple)
                assert coef.dim() == len(key)
            index = tuple(choices[name] for name in key)
            result = result + coef[index]

    return result


def model(
    schema: Schema,
    feature_blocks: Blocks,
    extra_features: Optional[torch.Tensor],
    experiment: Dict[str, torch.Tensor],  # sequences, batch_id, optional(response)
    *,
    max_batch_id: Optional[int] = None,
    response_type: str = "unit_interval",
    quantization_bins: int = 100,
):
    """
    A `Pyro <https://pyro.ai>`_ model for Bayesian linear regression.

    :param OrderedDict schema: A schema dict.
    :param list feature_blocks: A list of choice blocks for linear regression.
    :param dict experiment: A dict containing all old experiment data.
    :param str response_type: Type of response, one of: "real", "unit_interval".
    :param int quantization_bins: Number of bins in which to quantize the
        "unit_interval" response response_type.
    :returns: A dictionary mapping feature tuples to coefficient tensors.
    :rtype: dict
    """
    if max_batch_id is None:
        max_batch_id = int(experiment["batch_ids"].max())
    N = experiment["sequences"].size(0)
    B = 1 + max_batch_id
    if __debug__ and not torch._C._get_tracing_state():
        validate(schema, experiment=experiment)
        if extra_features is not None:
            assert extra_features.dim() == 2
            assert extra_features.size(0) == N
    name_to_int = {name: i for i, name in enumerate(schema)}

    # Hierarchically sample linear coefficients.
    coef_scale_loc = pyro.sample("coef_scale_loc", dist.Normal(-2, 1))
    coef_scale_scale = pyro.sample("coef_scale_scale", dist.LogNormal(0, 1))
    coefs: Coefs = {}
    trivial_blocks: Blocks = [[]]  # For the constant term.
    for block in trivial_blocks + feature_blocks:
        shape = tuple(len(schema[name]) for name in block)
        ps = tuple(name_to_int[name] for name in block)
        suffix = "_".join(map(str, ps))
        # Within-component variance of coefficients.
        coef_scale = pyro.sample(
            f"coef_scale_{suffix}",
            dist.LogNormal(coef_scale_loc, coef_scale_scale),
        )
        # Linear coefficients. Note this overparametrizes; there are only
        # len(choices) - 1 degrees of freedom and 1 nuisance dim.
        coefs[tuple(block)] = pyro.sample(
            f"coef_{suffix}",
            dist.Normal(torch.zeros(shape), coef_scale).to_event(len(shape)),
        )
    if extra_features is not None:
        # Sample coefficients for all extra user-provided features.
        shape = extra_features.shape[-1:]
        coef_scale = pyro.sample(
            "coef_scale", dist.LogNormal(coef_scale_loc, coef_scale_scale)
        )
        coefs[None] = pyro.sample(
            "coef", dist.Normal(torch.zeros(shape), coef_scale).to_event(1)
        )

    # Compute the linear response function.
    response_loc = linear_response(
        schema,
        coefs,
        experiment["sequences"],
        extra_features,
    )

    # Observe a noisy response.
    within_batch_scale = pyro.sample("within_batch_scale", dist.LogNormal(0, 1))
    if B == 1:
        within_batch_loc = response_loc
    else:
        # Model batch effects.
        across_batch_scale = pyro.sample("across_batch_scale", dist.LogNormal(0, 1))
        with pyro.plate("batch", B):
            batch_response = pyro.sample(
                "batch_response", dist.Normal(0, across_batch_scale)
            )
            if not torch._C._get_tracing_state():
                assert batch_response.shape == (B,)
        within_batch_loc = response_loc + batch_response[experiment["batch_ids"]]

    # This likelihood can be generalized to counts or other datatype.
    with pyro.plate("data", N):
        if response_type == "real":
            pyro.sample(
                "responses",
                dist.Normal(within_batch_loc, within_batch_scale),
                obs=experiment.get("responses"),
            )

        elif response_type == "unit_interval":
            logits = pyro.sample(
                "logits",
                dist.Normal(within_batch_loc, within_batch_scale),
            )

            # Quantize the observation to avoid numerical artifacts near 0 and 1.
            quantized_obs = None
            response = experiment.get("responses")
            if response is not None:  # during inference
                quantized_obs = (response * quantization_bins).round()
            quantized_obs = pyro.sample(
                "quantized_response",
                dist.Binomial(quantization_bins, logits=logits),
                obs=quantized_obs,
            )
            assert quantized_obs is not None
            if response is None:  # during simulation
                pyro.deterministic("responses", quantized_obs / quantization_bins)

        else:
            raise ValueError(f"Unknown response_type type {repr(response_type)}")

    return coefs
