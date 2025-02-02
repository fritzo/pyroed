from typing import Callable, Dict, Optional

import pyro
import torch
from pyro.infer import SVI, JitTrace_ELBO, Trace_ELBO
from pyro.infer.autoguide import AutoLowRankMultivariateNormal
from pyro.infer.mcmc import MCMC, NUTS
from pyro.optim import ClippedAdam


def fit_svi(
    model: Callable,
    *,
    lr: float = 0.01,
    num_steps: int = 201,
    jit_compile: Optional[bool] = None,
    log_every: int = 100,
    plot: bool = False,
) -> Callable[[], Dict[str, torch.Tensor]]:
    """
    Fits a model via stochastic variational inference.

    :param callable model: A Bayesian regression model from :mod:`pyroed.models`.
    :returns: A variational distribution that can generate samples.
    :rtype: callable
    """
    if jit_compile is None:
        jit_compile = False  # default to False to avoid jit error

    pyro.clear_param_store()
    guide: Callable[[], Dict[str, torch.Tensor]] = AutoLowRankMultivariateNormal(model)
    optim = ClippedAdam({"lr": lr, "lrd": 0.1 ** (1 / num_steps)})
    elbo = (JitTrace_ELBO if jit_compile else Trace_ELBO)()
    svi = SVI(model, guide, optim, elbo)
    losses = []
    for step in range(num_steps):
        loss = svi.step()
        losses.append(loss)
        if log_every and step % log_every == 0:
            print(f"svi step {step} loss = {loss:0.6g}")

    if plot:
        import matplotlib.pyplot as plt

        plt.plot(losses)
        plt.xlabel("SVI step")
        plt.ylabel("loss")

    return guide


def fit_mcmc(
    model: Callable,
    *,
    num_samples: int = 500,
    warmup_steps: int = 500,
    num_chains: int = 1,
    jit_compile: Optional[bool] = None,
) -> Callable[[], Dict[str, torch.Tensor]]:
    """
    Fits a model via Hamiltonian Monte Carlo.

    :param callable model: A Bayesian regression model from :mod:`pyroed.models`.
    :returns: A sampler that draws from the empirical distribution.
    :rtype: Sampler
    """
    if jit_compile is None:
        jit_compile = True  # default to True for speed

    kernel = NUTS(model, jit_compile=jit_compile)
    mcmc = MCMC(
        kernel,
        num_samples=num_samples,
        warmup_steps=warmup_steps,
        num_chains=num_chains,
    )
    mcmc.run()
    samples = mcmc.get_samples()
    return Sampler(samples)


class Sampler:
    """
    Helper to sample from an empirical distribution.

    :param dict samples: A dictionary of batches of samples.
    """

    def __init__(self, samples: Dict[str, torch.Tensor]):
        self.samples = samples
        self.num_samples = len(next(iter(samples.values())))

    def __call__(self) -> Dict[str, torch.Tensor]:
        i = torch.randint(0, self.num_samples, ())
        return {k: v[i] for k, v in self.samples.items()}
