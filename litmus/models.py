'''
Contains NumPyro generative models.

HM 24
'''

# ============================================
# IMPORTS
import sys

import jax.scipy.optimize
import numpy as np
import numpyro
from numpyro.distributions import MixtureGeneral
from numpyro import distributions as dist
from numpyro import handlers

from tinygp import GaussianProcess
from jax.random import PRNGKey
import jax.numpy as jnp
import tinygp

import litmus._utils
from litmus.gp_working import *

import scipy

from litmus._utils import *
import jaxopt


def quickprior(targ, key):
    p = targ.prior_ranges[key]
    distrib = dist.Uniform(float(p[0]), float(p[1])) if p[0] != p[1] else dist.Delta(float(p[0]))
    out = numpyro.sample(key, distrib)
    return (out)


# ============================================
#

# TODO - update these
# Default prior ranges. Kept in one place as a convenient storage area
_default_config = {
    'logtau': (0, 10),
    'logamp': (0, 10),
    'rel_amp': (0, 10),
    'mean': (-50, 50),
    'rel_mean': (0.0, 1.0),
    'lag': (0, 1000),

    'outlier_spread': 10.0,
    'outlier_frac': 0.25,
}


# ============================================
# Base Class

class stats_model(object):
    '''
    Base class for bayesian generative models. Includes a series of utilities for evaluating likelihoods, gradients etc,
    as well as various

    Todo:
        - Change prior volume calc to be a function call for flexibility
        - Add kwarg support to model_function and model calls to be more flexible / allow for different prior types
        - Fix the _scan method to use jaxopt and be jitted / vmapped
        - Add Hessian & Grad functions
    '''

    def __init__(self, prior_ranges=None):

        # Setting prior boundaries
        if not hasattr(self, "_default_prior_ranges"):
            self._default_prior_ranges = {
                'lag': _default_config['lag'],
            }

        self.prior_ranges = {} | self._default_prior_ranges  # Create empty priors
        self.prior_volume = 1.0

        # Update with args
        self.set_priors(self._default_prior_ranges | prior_ranges) if prior_ranges is not None else self.set_priors(
            self._default_prior_ranges)

        # --------------------------------------
        # Create jitted, vmapped and grad/hessians of all density functions

        for func in [self._log_density, self._log_density_uncon, self._log_prior]:
            name = func.__name__
            # unpacked_func = _utils.pack_function(func, packed_keys=self.paramnames())

            # Take grad, hessian and jit
            jitted_func = jax.jit(func)
            graded_func = jax.jit(jax.grad(func, argnums=0))
            hessed_func = jax.jit(jax.hessian(func, argnums=0))

            jitted_func.__doc__ = func.__doc__ + ", jitted version"
            graded_func.__doc__ = func.__doc__ + ", grad'd and jitted version"
            hessed_func.__doc__ = func.__doc__ + ", hessian'd and jitted version"

            # todo - add vmapped versions to these as well, and possibly leave raw un-jitted calls for better performance down the track
            # todo - Maybe have packed fuction calls for easier math-ing on the hessian? Probably not. We can unpack the interesting ones later

            # Set attributes
            self.__setattr__(name + "_jit", jitted_func)
            self.__setattr__(name + "_grad", graded_func)
            self.__setattr__(name + "_hess", hessed_func)

        # --------------------------------------
        #
        self.uncon_grad = jax.grad(self._uncon_grad, argnums=0)

    def set_priors(self, prior_ranges: dict):
        '''
        Sets the stats model prior ranges for uniform priors. Does some sanity checking to avoid negative priors
        :param prior_ranges:
        :return: 
        '''

        badkeys = [key for key in prior_ranges.keys() if key not in self._default_prior_ranges.keys()]

        for key, val in zip(prior_ranges.keys(), prior_ranges.values()):
            if key in badkeys:
                continue
            assert (isiter(val)), "Bad input shape in set_priors for key %s" % key  # todo - make this go to std.err
            a, b = val
            self.prior_ranges[key] = [float(a), float(b)]

        # Calc and set prior volume
        # Todo - Make this more general. Revisit if we separate likelihood + prior
        prior_volume = 1.0
        for key in self.prior_ranges:
            a, b = self.prior_ranges[key]
            if b != a:
                prior_volume *= b - a
        self.prior_volume = prior_volume

        return

    # --------------------------------
    # MODEL FUNCTIONS
    def prior(self):
        '''
        A NumPyro callable prior
        '''
        lag = numpyro.sample('lag', dist.Uniform(self.prior_ranges['lag'][0], self.prior_ranges['lag'][1]))
        return (lag)

    def model_function(self, data):
        '''
        A NumPyro callable function
        '''
        lag = self.prior()

    def lc_to_data(self, lc_1: lightcurve, lc_2: lightcurve):
        '''
        Converts light-curves into the format required for the model. For most models this will return as some sort
        of sorted dictionary
        :param lc_1: First lightcurve object
        :param lc_2: Second lightcurve object
        :return:
        '''

        T = jnp.array([*lc_1.T, *lc_2.T])
        Y = jnp.array([*lc_1.Y, *lc_2.Y])
        E = jnp.array([*lc_1.E, *lc_2.E])
        bands = jnp.array([*np.zeros(lc_1.N), *np.ones(lc_2.N)]).astype(int)

        I = T.argsort()

        T, Y, E, bands = T[I], Y[I], E[I], bands[I]

        data = {'T': T,
                'Y': Y,
                'E': E,
                'bands': bands
                }

        return (data)

    # --------------------------------
    # Parameter transforms and other utils
    def to_uncon(self, params):
        '''
        Converts model parametes from "real" constrained domain values into HMC friendly unconstrained values.
        Inputs and outputs as keyed dict.
        '''
        out = numpyro.infer.util.unconstrain_fn(self.prior, params=params, model_args=(), model_kwargs={})
        return (out)

    def to_con(self, params):
        '''
        Converts model parametes back into "real" constrained domain values.
        Inputs and outputs as keyed dict.
        '''
        out = numpyro.infer.util.constrain_fn(self.prior, params=params, model_args=(), model_kwargs={})
        return (out)

    def uncon_grad(self, params):
        '''
        Returns the log of det(Jac) by evaluating pi(x) and pi'(x').
        '''
        con_dens = numpyro.infer.util.log_density(self.prior, (), {}, params)[0]

        up = self.to_uncon(params)
        uncon_dens = -numpyro.infer.util.potential_energy(self.prior, (), {}, up)
        out = con_dens - uncon_dens
        return out

    @jax.jacfwd
    def jacobian(self, params):
        '''
        Converts model parametes back into "real" constrained domain values.
        Inputs and outputs as keyed dict.
        '''
        out = numpyro.infer.util.constrain_fn(self.prior, params=params, model_args=(), model_kwargs={})
        return (out)

    def paramnames(self):
        '''
        Returns the names of all model parameters. Purely for brevity.
        '''
        return (list(self.prior_ranges.keys()))

    def fixed_params(self):
        '''
        Returns the names of all model parameters. Purely for brevity.
        '''
        is_fixed = {key: np.ptp(self.prior_ranges[key]) == 0 for key in self.prior_ranges.keys()}
        out = [key for key in is_fixed.keys() if is_fixed[key]]
        return (out)

    def free_params(self):
        '''
        Returns the names of all model parameters. Purely for brevity.
        '''
        is_fixed = {key: np.ptp(self.prior_ranges[key]) == 0 for key in self.prior_ranges.keys()}
        out = [key for key in is_fixed.keys() if not is_fixed[key]]
        return (out)

    def dim(self):
        '''
        Quick and easy call for the number of model parameters
        :return:
        '''
        return (len(self.paramnames()))

    # --------------------------------
    # Un-Jitted / un-vmapped likelihood calls
    '''
    Functions in this sector are in their basic form. Those with names appended by '_forgrad' accept inputs as arrays
    '''

    def _log_density(self, params, data):
        '''
        Constrained space un-normalized posterior log density
        '''
        out = \
            numpyro.infer.util.log_density(self.model_function, params=params, model_args=(),
                                           model_kwargs={'data': data})[
                0]
        return (out)

    def _log_likelihood(self, params, data):
        '''
        WARNING! This function won't work if your model has more than one observation site!
        Constrained space un-normalized posterior log likelihood
        '''
        out = numpyro.infer.util.log_likelihood(self.model_function, posterior_samples=params, data=data)
        out = sum(out.values())
        return (out)

    def _log_density_uncon(self, params, data):
        '''
        Unconstrained space un-normalized posterior log density
        '''
        out = -numpyro.infer.util.potential_energy(self.model_function, params=params, model_args=(),
                                                   model_kwargs={'data': data})
        return (out)

    def _log_prior(self, params, data=None):
        '''
        Model prior density in unconstrained space
        '''
        out = numpyro.infer.util.log_density(self.prior, (), {}, params)[0]
        return (out)

    # --------------------------------
    # Wrapped Function Evaluations
    def log_density(self, params, data, use_vmap=False):

        if isiter_dict(params):
            N = dict_dim(params)[1]
            out = np.zeros(N)
            for i in range(N):
                p = {key: params[key][i] for key in params.keys()}
                out[i] = self._log_density_jit(p, data)
        else:
            out = np.array([self._log_density_jit(params, data)])

        return out

    def log_likelihood(self, params, data, use_vmap=False):

        if isiter_dict(params):
            N = dict_dim(params)[1]
            out = np.zeros(N)
            for i in range(N):
                p = {key: params[key][i] for key in params.keys()}
                out[i] = self._log_likelihood(p, data)
        else:
            out = self._log_likelihood(params, data)

        return out

    def log_density_uncon(self, params, data, use_vmap=False):

        if isiter_dict(params):
            N = dict_dim(params)[1]
            out = np.zeros(N)
            for i in range(N):
                p = {key: params[key][i] for key in params.keys()}
                out[i] = self._log_density_uncon_jit(p, data)
        else:
            out = self._log_density_uncon_jit(params, data)

        return out

    def log_prior(self, params, data=None, use_vmap=False):
        if isiter_dict(params):
            N = dict_dim(params)[1]
            out = np.zeros(N)
            for i in range(N):
                p = {key: params[key][i] for key in params.keys()}
                out[i] = self._log_prior_jit(p)
        else:
            out = self._log_prior_jit(params)

        return out

    # --------------------------------
    # Wrapped Grad evaluations
    def log_density_grad(self, params, data, use_vmap=False):

        if isiter_dict(params):
            m, N = dict_dim(params)
            out = {key: np.zeros([N]) for key in params.keys()}
            for i in range(N):
                p = {key: params[key][i] for key in params.keys()}
                grads = self._log_density_grad(p, data)
                for key in params.keys():
                    out[key][i] = grads[key]
        else:
            out = self._log_density_grad(params, data)

        return out

    def log_density_uncon_grad(self, params, data, use_vmap=False):

        if isiter_dict(params):
            m, N = dict_dim(params)
            out = np.zeros(N)
            for i in range(N):
                p = {key: params[key][i] for key in params.keys()}
                out[i, :] = self._log_density_uncon_grad(p, data)
        else:
            out = self._log_density_uncon_grad(params, data)

        return out

    def log_prior_grad(self, params, data=None, use_vmap=False):
        if isiter(params):
            m, N = dict_dim(params)
            out = np.zeros(N)
            for i in range(N):
                p = {key: params[key][i] for key in params.keys()}
                out[i, :] = self._log_prior_grad(p)
        else:
            out = self._log_prior_grad(params)

        return out

    # --------------------------------
    # Wrapped Hessian evaluations
    def log_density_hess(self, params, data, use_vmap=False):

        if isiter_dict(params):
            m, N = dict_dim(params)
            out = np.zeros([N, m, m])
            for i in range(N):
                p = {key: params[key][i] for key in params.keys()}
                hess_eval = self._log_density_hess(p, data)
                for j, key1 in enumerate(self.paramnames()):
                    for k, key2 in enumerate(self.paramnames()):
                        out[i, j, k] = hess_eval[key1][key2]
        else:
            m = len(self.paramnames())
            out = np.zeros([m, m])
            hess_eval = self._log_density_hess(params, data)
            for j, key1 in enumerate(self.paramnames()):
                for k, key2 in enumerate(self.paramnames()):
                    out[j, k] = hess_eval[key1][key2]

        return out

    def log_density_uncon_hess(self, params, data, use_vmap=False):

        if isiter_dict(params):
            m, N = dict_dim(params)
            out = np.zeros([N, m, m])
            for i in range(N):
                p = {key: params[key][i] for key in params.keys()}
                hess_eval = self._log_density_uncon_hess(p, data)
                for j, key1 in enumerate(self.paramnames()):
                    for k, key2 in enumerate(self.paramnames()):
                        out[i, j, k] = hess_eval[key1][key2]
        else:
            m = len(self.paramnames())
            out = np.zeros([m, m])
            hess_eval = self._log_density_uncon_hess(params, data)
            for j, key1 in enumerate(self.paramnames()):
                for k, key2 in enumerate(self.paramnames()):
                    out[j, k] = hess_eval[key1][key2]

        return out

    def log_prior_hess(self, params, data=None, use_vmap=False):

        if isiter_dict(params):
            m, N = dict_dim(params)
            out = np.zeros([N, m, m])
            for i in range(N):
                p = {key: params[key][i] for key in params.keys()}
                hess_eval = self._log_prior_hess(p)
                for j, key1 in enumerate(self.paramnames()):
                    for k, key2 in enumerate(self.paramnames()):
                        out[i, j, k] = hess_eval[key1][key2]
        else:
            m = len(self.paramnames())
            out = np.zeros([m, m])
            hess_eval = self._log_prior_hess(params)
            for j, key1 in enumerate(self.paramnames()):
                for k, key2 in enumerate(self.paramnames()):
                    out[j, k] = hess_eval[key1][key2]

        return out

    # --------------------------------
    # Wrapped evaluation utilities
    def scan(self, start_params, data, optim_params=None, use_vmap=False, stepsize=0.1, maxiter=1_000, tol=1E-5):
        '''
        Beginning at position 'start_params', optimize parameters in 'optim_params' to find maximum
        '''

        # Convert to unconstrainedc domain
        start_params_uncon = self.to_uncon(start_params)

        if optim_params is None: optim_params = self.paramnames()
        if len(optim_params) == 0: return start_params

        # Get all split into fixed and free params
        x0 = jnp.array([start_params_uncon[key] for key in optim_params])
        y0 = {key: start_params_uncon[key] for key in start_params_uncon.keys() if key not in optim_params}

        # Make a jaxopt friendly packed function
        optfunc = pack_function(self._log_density_uncon, packed_keys=optim_params, fixed_values=y0, invert=True)
        # f = lambda x: optfunc(x, data=data)
        print("At initial uncon position", x0, "with keys", optim_params, "eval for optfunc is", optfunc(x0, data=data))

        # Build and run an optimizer
        solver = jaxopt.BFGS(fun=optfunc,
                             stepsize=stepsize,
                             maxiter=maxiter,
                             tol=tol,
                             jit=True)
        out, state = solver.run(init_params=x0, data=data)

        print("At final uncon position", out, "with keys", optim_params, "eval for optfunc is", optfunc(out, data=data))

        # Unpack the results to a dict
        out = {key: out[i] for i, key in enumerate(optim_params)}
        out = out | y0  # Adjoin the fixed values

        # Convert back to constrained domain
        out = self.to_con(out)

        return out

    def laplace_log_evidence(self, params, data, integrate_axes=None, use_vmap=False, constrained=False):
        '''
        At some point 'params' in parameter space, gets the hessian in unconstrained space and uses to estimate the
        model evidence
        :param data:
        :param params:
        :param use_vmap:
        :return:
        '''

        print("-------------")
        print("Laplace Evidence eval")

        print("Constrained params are:")
        print(params)

        if integrate_axes is None:
            integrate_axes = self.paramnames()

        if not constrained:
            uncon_params = self.to_uncon(params)

            print("Un-Constrained params are:")
            print(uncon_params)

            log_height = self.log_density_uncon(uncon_params, data)
            hess = self.log_density_uncon_hess(uncon_params, data)
        else:
            log_height = self.log_density(params, data)
            hess = self.log_density_hess(params, data)

        I = np.where([key in integrate_axes for key in self.paramnames()])[0]

        hess = hess[I, :][:, I]

        print("For hessian:")
        print(np.around(hess, 1))

        if len(I) > 1:
            dethess = np.linalg.det(hess)
        elif len(I) == 1:
            dethess = hess[0][0]
        else:
            dethess = -1

        print("With determinant:")
        print(dethess)

        print("And log height: %.2f..." % log_height)

        D = len(integrate_axes)
        out = np.log(2 * np.pi) * (D / 2) - np.log(-dethess) / 2 + log_height

        print("HERE!")
        print(np.log(2 * np.pi) * (D / 2), - np.log(-dethess) / 2, log_height)

        print("log-evidence is ~%.2f" % out)
        return out

    def laplace_log_info(self, params, data, integrate_axes=None, use_vmap=False, constrained=False):
        '''
        At some point 'params' in parameter space, gets the hessian in unconstrained space and uses to estimate the
        model information relative to the prior
        :param data:
        :param params:
        :param use_vmap:
        :return:
        '''

        if integrate_axes is None:
            integrate_axes = self.paramnames()

        if not constrained:
            uncon_params = self.to_uncon(params)

            log_height = self.log_density_uncon(uncon_params, data)
            hess = self.log_density_uncon_hess(uncon_params, data)
        else:
            log_height = self.log_density(params, data)
            hess = self.log_density_hess(params, data)

        I = np.where([key in integrate_axes for key in self.paramnames()])[0]

        hess = hess[I, I]
        if len(I) > 1:
            dethess = np.linalg.det(hess)
        else:
            dethess = hess

        # todo - double check sign on the log term. Might be wrong
        # todo - add case check for non-uniform priors.
        D = len(integrate_axes)
        out = -(np.log(2 * np.pi) + 1) * (D / 2) - np.log(-dethess) / 2 + np.log(self.prior_volume)
        return out

    # --------------------------------
    # Sampling Utils
    def prior_sample(self, num_samples: int = 1, seed: int = None) -> dict:
        '''
        Blind sampling from the prior without conditioning. Returns model parameters only
        :param num_samples: Number of realizations to generate
        :return:
        '''

        if seed == None: seed = _utils.randint()

        pred = numpyro.infer.Predictive(self.prior,
                                        num_samples=num_samples,
                                        return_sites=self.paramnames()
                                        )

        params = pred(rng_key=jax.random.PRNGKey(seed))

        if num_samples == 1:
            params = {key: params[key][0] for key in params.keys()}
        return (params)

    def realization(self, data=None, num_samples: int = 1, seed: int = None):
        '''
        Generates realizations by blindly sampling from the prior
        :param num_samples: Number of realizations to generate
        :return:
        '''
        if seed == None: seed = _utils.randint()

        pred = numpyro.infer.Predictive(self.model_function,
                                        num_samples=num_samples,
                                        return_sites=None
                                        )

        params = pred(rng_key=jax.random.PRNGKey(seed), data=data)
        return (params)


# ============================================
# Custom statmodel example
class dummy_statmodel(stats_model):
    '''
    An example of how to construct your own stats_model in the simplest form.
    Requirements are to:
        1. Set a default prior range for all parameters used in model_function
        2. Define a numpyro generative model model_function
    You can add / adjust methods as required, but these are the only main steps
    '''

    def __init__(self, prior_ranges=None):
        self._default_prior_ranges = {
            'lag': _default_config['lag'],
            'test_param': [0.0, 1.0]
        }
        super().__init__(prior_ranges=prior_ranges)
        self.lag_peak = 250.0
        self.amp_peak = 0.5

    # ----------------------------------
    def prior(self):
        '''
        lag = numpyro.sample('lag', dist.Uniform(self.prior_ranges['lag'][0], self.prior_ranges['lag'][1]))
        test_param = numpyro.sample('test_param', dist.Uniform(self.prior_ranges['test_param'][0],
                                                               self.prior_ranges['test_param'][1]))
        '''

        lag = quickprior(self, 'lag')
        test_param = quickprior(self, 'test_param')

        return (lag, test_param)

    def model_function(self, data):
        lag, test_param = self.prior()

        numpyro.sample('test_sample', dist.Normal(lag, 100), obs=self.lag_peak)
        numpyro.sample('test_sample_2', dist.Normal(test_param, 1.0), obs=self.amp_peak)


# ============================================
# Custom statmodel example
class GP_simple(stats_model):
    '''
    An example of how to construct your own stats_model in the simplest form.
    Requirements are to:
        1. Set a default prior range for all parameters used in model_function
        2. Define a numpyro generative model model_function
    You can add / adjust methods as required, but these are the only main steps
    '''

    def __init__(self, prior_ranges=None, **kwargs):
        self._default_prior_ranges = {
            'lag': _default_config['lag'],
            'logtau': _default_config['logtau'],
            'logamp': _default_config['logamp'],
            'rel_amp': _default_config['rel_amp'],
            'mean': _default_config['mean'],
            'rel_mean': _default_config['rel_mean'],
        }
        super().__init__(prior_ranges=prior_ranges)

        self.basekernel = kwargs['basekernel'] if 'basekernel' in kwargs.keys() else tinygp.kernels.quasisep.Exp

    # --------------------
    def prior(self):
        # Sample distributions
        lag = quickprior(self, 'lag')

        logtau = quickprior(self, 'logtau')
        logamp = quickprior(self, 'logamp')

        rel_amp = quickprior(self, 'rel_amp')
        mean = quickprior(self, 'mean')
        rel_mean = quickprior(self, 'rel_mean')

        return (lag, logtau, logamp, rel_amp, mean, rel_mean)

    def model_function(self, data):
        lag, logtau, logamp, rel_amp, mean, rel_mean = self.prior()

        T, Y, E, bands = [data[key] for key in ['T', 'Y', 'E', 'bands']]

        # Conversions to gp-friendly form
        amp, tau = jnp.exp(logamp), jnp.exp(logtau)

        diag = jnp.square(E)

        delays = jnp.array([0, lag])
        amps = jnp.array([amp, rel_amp * amp])
        means = jnp.array([mean, mean + rel_mean])

        T_delayed = T + delays[bands]
        I = T_delayed.argsort()

        # Build and sample GP

        gp = build_gp(T_delayed[I], Y[I], diag[I], bands[I], tau, amps, means, basekernel=self.basekernel)
        numpyro.sample("Y", gp.numpyro_dist(), obs=Y[I])

    # -----------------------


# ============================================
# ============================================
# Testing

if __name__ == '__main__':
    import matplotlib.pyplot as plt

    # Build a test stats model and adjust priors
    test_statmodel = dummy_statmodel()
    test_statmodel.set_priors({
        'lag': [0, 500],
        'test_param': [0.0, 0.0]
    })

    # Generate prior samples and evaluate likelihoods
    test_data = jnp.array([100., 0.25])
    test_params = test_statmodel.prior_sample(num_samples=1_000)
    log_likes = test_statmodel.log_density(data=test_data, params=test_params)
    log_grads = test_statmodel.log_density_grad(data=test_data, params=test_params)['lag']
    log_hess = test_statmodel.log_density_hess(data=test_data, params=test_params)[:, 0, 0]

    # ------------------------------
    fig, (a1, a2) = plt.subplots(2, 1, sharex=True)
    a1.scatter(test_params['lag'], log_likes)
    a2.scatter(test_params['lag'], np.exp(log_likes))

    for a in (a1, a2): a.grid()
    fig.supxlabel("Lag (days)")
    a1.set_ylabel("Log-Density")
    a2.set_ylabel("Density")
    fig.tight_layout()

    plt.show()

    # ------------------------------
    fig, (a1, a2) = plt.subplots(2, 1, sharex=True)
    a1.scatter(test_params['lag'], log_grads)
    a2.scatter(test_params['lag'], log_hess)

    for a in (a1, a2): a.grid()
    fig.supxlabel("Lag (days)")
    a1.set_ylabel("Log-Density Gradient")
    a2.set_ylabel("Log-Density Curvature")
    fig.tight_layout()

    plt.show()

    # ===============================
    # Try a scan
    opt_lag = test_statmodel.scan(start_params={'lag': 10.1, 'test_param': 0.0}, data=test_data,
                                  optim_params=['lag', 'test_param'],
                                  use_vmap=False,
                                  stepsize=0.1,
                                  maxiter=500)['lag']
    print("best lag is", opt_lag)

    Z1 = test_statmodel.laplace_log_evidence(params={'lag': opt_lag, 'test_param': 0.0}, data=test_data,
                                             integrate_axes=['lag'])
    Z2 = test_statmodel.laplace_log_evidence(params={'lag': opt_lag, 'test_param': 0.0}, data=test_data,
                                             integrate_axes=['lag'], constrained=True)
    print("Estimate for evidence is %.2f" % np.exp(Z1))
    print("Estimate for evidence is %.2f" % np.exp(Z2))

    # ------------------------------
    fig = plt.figure()
    plt.title("Normalization Demonstration")
    plt.scatter(test_params['lag'], np.exp(log_likes) / np.exp(log_likes).mean() / 500, s=1, label="Monte Carlo Approx")
    plt.scatter(test_params['lag'], np.exp(log_likes - Z1), s=1, label="Laplace norm from unconstrained domain")
    plt.scatter(test_params['lag'], np.exp(log_likes - Z2), s=1, label="Laplace norm from constrained domain")

    #plt.yscale('log')

    plt.legend()

    plt.ylabel("Posterior Probability")
    fig.tight_layout()
    plt.grid()

    plt.show()
