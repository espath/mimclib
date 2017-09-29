from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import copy
import gc
import numpy as np
import itertools
import warnings
from . import setutil
from scipy.stats import norm

__all__ = []
import argparse

VERBOSE_INFO = 1
VERBOSE_DEBUG = 10

def public(sym):
    __all__.append(sym.__name__)
    return sym

class Timer():
    def __init__(self, clock=time.clock):
        self._tics = []
        self._clock = clock
        self.tic()

    def tic(self):
        self._tics.append(self._clock())

    def toc(self, pop=True):
        assert(len(self._tics) > 0)
        if pop:
            return self._clock()-self._tics.pop()
        else:
            return self._clock()-self._tics[-1]

    def ptoc(self, msg='Time since last tic: {:.4f} sec.', pop=False):
        assert(len(self._tics) > 0)
        print(msg.format(self.toc(pop=pop)))


def _calcTheoryM(TOL, theta, Vl, Wl,
                 active_lvls, ceil=True, minM=1,
                 Ca=2):
    act = active_lvls >= 0
    with np.errstate(divide='ignore', invalid='ignore'):
        M = (theta * TOL / Ca)**-2 *\
            np.sum(np.sqrt(Wl[act] * Vl[act])) * np.sqrt(Vl / Wl)
        M = np.maximum(M, minM)
        M[np.isnan(M)] = minM
        M[np.logical_not(act)] = 0
    if ceil:
        M = np.ceil(M).astype(np.int)
    return M


@public
def calc_ml2r_weights(alpha, L):
    ell = np.arange(L+1)
    denom = np.hstack(([1.], np.cumprod(1-np.exp(-alpha*(ell+1)))))
    w = (-1) ** (L-ell) * np.exp(-alpha*(L-ell)*(L+1-ell)/2.) / (denom[ell] * denom[L-ell])
    return np.cumsum(w[::-1])[::-1]

@public
class custom_obj(object):
    # Used to add samples. Supposedly not used if M is fixed to 1
    def __add__(self, d):  # d type is custom_obj
        raise NotImplementedError("You should implement the __add__ function")

    # Used to compute delta samples. Supposedly only multiples 1 and -1
    # Might be more if the combination technique is used.
    def __mul__(self, scale): # scale is float
        raise NotImplementedError("You should implement the __mul__ function")

    # Used for moment computation. Supposedly not used if moments==1
    def __pow__(self, power): # power is float
        raise NotImplementedError("You should implement the __mul__ function")

    # Used to compute moments from sums. Supposedly not used if M is fixed to 1
    def __truediv__(self, scale): # scale is integer
        if scale == 1:
            return self
        raise NotImplementedError("You should implement the __truediv__ function")

    def __sub__(self, d):
        return self + d*-1

class _empty_obj(object):
    def __add__(self, newarr):
        return newarr  # Forget about this object

@public
def compute_raw_moments(psums, M):
    '''
    Returns the raw moments or None when M=0.
    '''
    idx = M != 0
    val = np.empty_like(psums)
    val[idx] = psums[idx] * (1./ _expand(M[idx], 0, val[idx].shape))
    #np.tile(M[idx].reshape((-1,1)), (1, psums.shape[1]))
    val[M == 0, :] = None
    return val

@public
def compute_central_moment(psums, M, moment):
    '''
    Returns the centralized moments or None when M=0.
    '''
    raw = compute_raw_moments(psums, M)
    if moment == 1:
        return raw[:, 0]

    n = moment
    pn = np.array([n])
    val = (raw[:, 0]**pn) * (-1)**n
    # From http://mathworld.wolfram.com/CentralMoment.html
    nfact = np.math.factorial(n)
    for k in range(1, moment+1):
        nchoosek = nfact / (np.math.factorial(k) * np.math.factorial(n-k))
        val +=  (raw[:, k-1] * raw[:, 0]**(pn-k)) * nchoosek * (-1)**(n-k)

    if moment % 2 == 1:
        return val
    return val
    # The moment should be positive
    # TODO: Debug for objects
    if np.min(val)<0.0:
        """
        There might be kurtosis values that are actually
        zero but slightly negative, smaller in magnitude
        than the machine precision. Fixing these manually.
        """
        idx = np.abs(val) < np.finfo(float).eps
        val[idx] = np.abs(val[idx])
        if np.min(val)<0.0:
            warnings.warn("Significantly negative {}'th moment = {}! \
Possible problem in computing sums.".format(moment, np.min(val)))
    return val

def _expand(b, i, shape):
    assert(len(b) == shape[i])
    b_shape = np.ones(len(shape), dtype=np.int)
    b_shape[i] = len(b)
    b_reps = list(shape)
    b_reps[i] = 1
    return np.tile(b.reshape(b_shape), b_reps)

@public
class MIMCItrData(object):
    """
    MIMC Data is a class for describing necessary data
    for a MIMC data, such as the dimension of the problem,
    list of levels, times exerted, sample sizes, etc...

    In a MIMC Run object, the data is stored in a MIMCItrData object

    """
    def __init__(self, parent=None, min_dim=0, moments=None, lvls=None):
        self.parent = parent
        self.userdata = None
        self.moments = moments
        self._lvls = lvls if lvls is not None else setutil.VarSizeList(min_dim=min_dim)
        self.psums_delta = None
        self.psums_fine = None
        self.tT = np.zeros(0)      # Time of lvls
        self.tW = np.zeros(0)      # Time of lvls
        self.M = np.zeros(0, dtype=np.int)      # Number of samples in each lvl
        self.bias = np.inf           # Approximation of the discretization error
        self.stat_error = np.inf     # Sampling error (based on M)
        self.exact_error = np.nan    # Sampling error (based on M)
        self.TOL = None              # Target tolerance
        self.total_time = None
        self.Q = None
        self.Vl_estimate = np.zeros(0)
        self._lvls_count = 0
        self.weights = np.zeros(0)

        # Any level < start_lvl is not active
        self.active_lvls = np.array([])  # -1: inactive, 0: base, 1: active
        self._levels_added()

    def next_itr(self):
        return self.copy(copy_lvls=False)

    def copy(self, copy_lvls=True):
        ret = MIMCItrData(parent=self.parent,
                          moments=self.moments,
                          lvls=self._lvls.copy() if copy_lvls else self._lvls)
        ret._lvls_count = self._lvls_count
        ret.psums_delta = self.psums_delta.copy() if self.psums_delta is not None else None
        ret.psums_fine = self.psums_fine.copy() if self.psums_fine is not None else None
        ret.tT = self.tT.copy()
        ret.tW = self.tW.copy()
        ret.M = self.M.copy()
        ret.bias = self.bias
        ret.stat_error = self.stat_error
        ret.total_time = self.total_time
        ret.TOL = self.TOL
        ret.Q = copy.copy(self.Q)
        ret.weights = copy.copy(self.weights)
        ret.Vl_estimate = self.Vl_estimate.copy() if self.Vl_estimate is not None else None

        ret.active_lvls = self.active_lvls.copy()
        return ret

    def calcEg(self):
        """
        Return the sum of the sample estimators for
        all the levels
        """
        return np.sum(self.calcEl(active_only=True), axis=0)

    def calcEl(self, moment=1, active_only=False, weighted=True):
        El = self.calcDeltaCentralMoment(moment=moment, weighted=weighted)
        El[self.active_lvls==0, ...] = self.calcFineCentralMoment(moment=moment,
                                                                  weighted=weighted)[self.active_lvls==0, ...]
        El[self.active_lvls<0, ...] = None
        if active_only:
            return El[self.active_lvls >= 0, ...]
        return El

    def calcVl(self, active_only=False, weighted=True):
        return self.calcEl(moment=2, active_only=active_only, weighted=weighted)

    # def computedMoments(self):
    #     return self.moments

    # def calcDeltaVl(self):
    #     return self.calcDeltaCentralMoment(2)

    # def calcDeltaEl(self):
    #     '''
    #     Returns the sample estimators for moments for each level.
    #     '''
    #     if self.psums_delta is None:
    #         return np.array([])
    #     idx = self.M != 0
    #     val = np.empty_like(self.psums_delta[:, 0])
    #     val[idx] = self.psums_delta[idx, 0] * \
    #                (1./_expand(self.M[idx], 0 ,self.psums_delta[idx, 0].shape))
    #     val[np.logical_not(idx)] = None
    #     return val

    def calcDeltaCentralMoment(self, moment, weighted=True):
        if self.psums_delta is None:
            return np.array([])
        B = compute_central_moment(self.psums_delta, self.M, moment)
        if not weighted:
            return B
        w = self.weights**moment
        slicer = [slice(None)] + [None]*(len(B.shape)-1)
        return w[slicer] * B

    def calcFineCentralMoment(self, moment, weighted=True):
        if self.psums_delta is None:
            return np.array([])
        B = compute_central_moment(self.psums_fine, self.M, moment)
        if not weighted:
            return B
        w = self.weights**moment
        slicer = [slice(None)] + [None]*(len(B.shape)-1)
        return w[slicer] * B

    def calcTl(self):
        idx = self.M != 0
        val = np.zeros_like(self.M, dtype=np.float)
        val[idx] = self.tT[idx] / self.M[idx]
        return val

    def calcWl(self):
        idx = self.M != 0
        val = np.zeros_like(self.M, dtype=np.float)
        val[idx] = self.tW[idx] / self.M[idx]
        return val

    def calcTotalTime(self, ind=None):
        return np.sum(self.tT, axis=0)

    def addSamples(self, lvl_idx, M, psums_delta, psums_fine, tT, tW):
        assert psums_delta.shape == psums_fine.shape and \
            psums_fine.shape[0] == self.moments, "Inconsistent arguments "
        #assert lvl_idx is not None, "Level was not found"
        if self.M[lvl_idx] == 0:
            if self.psums_delta is None:
                self.psums_delta = np.zeros((self.lvls_count,)
                                            + psums_delta.shape, dtype=psums_delta.dtype)

            if self.psums_fine is None:
                self.psums_fine = np.zeros((self.lvls_count,)
                                            + psums_fine.shape, dtype=psums_fine.dtype)

            self.psums_delta[lvl_idx] = psums_delta
            self.psums_fine[lvl_idx] = psums_fine
            self.M[lvl_idx] = M
            self.tT[lvl_idx] = tT
            self.tW[lvl_idx] = tW
        else:
            self.psums_delta[lvl_idx] += psums_delta
            self.psums_fine[lvl_idx] += psums_fine
            self.M[lvl_idx] += M
            self.tT[lvl_idx] += tT
            self.tW[lvl_idx] += tW
        if psums_delta.dtype != self.psums_delta.dtype:
            self.psums_delta = self.psums_delta.astype(psums_delta.dtype)
        if psums_fine.dtype != self.psums_fine.dtype:
            self.psums_fine = self.psums_fine.astype(psums_fine.dtype)

    def _levels_added(self):
        new_count = len(self._lvls)
        assert(new_count >= self._lvls_count)
        if new_count == self._lvls_count:
            return  # Levels were not really added
        if self.psums_delta is not None:
            self.psums_delta.resize((new_count, ) + self.psums_delta.shape[1:], refcheck=False)
        if self.psums_fine is not None:
            self.psums_fine.resize((new_count, ) + self.psums_fine.shape[1:], refcheck=False)

        self.Vl_estimate.resize(new_count, refcheck=False)
        self.tT.resize(new_count, refcheck=False)
        self.tW.resize(new_count, refcheck=False)
        self.M.resize(new_count, refcheck=False)
        self.weights.resize(new_count, refcheck=False)
        self.active_lvls.resize(new_count, refcheck=False)
        self.active_lvls[self._lvls_count:new_count] = 1 # Make all active
        self.weights[self._lvls_count:new_count] = 1.
        self._lvls_count = new_count

    def calcTotalWork(self):
        return np.sum(self.tW, axis=0)

    @property
    def total_error_est(self):
        return self.bias + (self.stat_error if not np.isnan(self.stat_error) else 0)

    def zero_samples(self, ind=None):
        if ind is None:
            self.M[:] = 0
            self.tT[:] = 0
            self.tW[:] = 0
            if self.psums_delta is not None:
                self.psums_delta[:] = np.zeros_like(self.psums_delta)
            if self.psums_fine is not None:
                self.psums_fine[:] = np.zeros_like(self.psums_fine)
        else:
            self.M[ind] = 0
            self.tT[ind] = 0
            self.tW[ind] = 0
            if self.psums_delta is not None:
                self.psums_delta[ind, :] = np.zeros_like(self.psums_delta[ind, :])
            if self.psums_fine is not None:
                self.psums_fine[ind, :] = np.zeros_like(self.psums_fine[ind, :])

    @property
    def lvls_count(self):
        return self._lvls_count

    def lvls_itr(self, start=0, end=None, min_dim=None):
        if end is None:
            end = self.lvls_count
        assert(end <= self.lvls_count)
        return self._lvls.dense_itr(start, end, min_dim=min_dim)

    def lvls_sparse_itr(self, start=0, end=None):
        if end is None:
            end = self.lvls_count
        assert(end <= self.lvls_count)
        return self._lvls.sparse_itr(start, end)

    def lvls_find(self, ind, j=None):
        i = self._lvls.find(ind=ind, j=j)
        return i if i < self.lvls_count else None

    def lvls_get(self, i):
        assert i < self.lvls_count
        return self._lvls[i]

    def lvls_add_from_list(self, inds, j=None):
        self._lvls.add_from_list(inds=inds, j=j)
        self._levels_added()

    def lvls_max_dim(self):
        return np.max(self._lvls.get_dim()[:self.lvls_count])

    def get_lvls(self):
        if self.lvls_count != len(self._lvls):
            raise Exception("Iteration does not use all levels!")
        return self._lvls

    def to_string(self, fnNorm):
        has_var = self.moments >= 2

        Wl = self.calcWl()
        if has_var:
            V = self.Vl_estimate
            V_fine = fnNorm(self.calcFineCentralMoment(moment=2))
            V_sample = fnNorm(self.calcDeltaCentralMoment(moment=2))

        E = fnNorm(self.calcEl())
        T = self.calcTl()

        columns = []

        def add_column(title, fmt, value_fmt, fn):
            columns.append([title, fmt, value_fmt, fn])

        add_column("Level", "<8", "<8", lambda i: str(self.lvls_get(i)))
        add_column("E", "^20", ">20.12e", lambda i: E[i])
        if has_var:
            add_column("V", "^20", ">20.12e", lambda i: V[i])
            add_column("fineV", "^20", ">20.12e", lambda i: V_fine[i])
            add_column("DeltaV", "^20", ">20.12e", lambda i: V_sample[i])
        add_column("W", "^20", ">20.12e", lambda i: Wl[i])
        add_column("M", "^8", ">8", lambda i: self.M[i])
        add_column("Time", "^15", ">15.6e", lambda i: T[i])

        header_fmt = "".join(sum([["{:", c[1], "}"] for c in columns], [])) + "\n"
        value_fmt = "".join(sum([["{:", c[2], "}"] for c in columns], [])) + "\n"

        output = header_fmt.format(*[c[0] for c in columns])

        for i in range(0, self.lvls_count):
            #,100 * np.sqrt(V[i]) / np.abs(E[i])
            output += value_fmt.format(*[c[3](i) for c in columns])
        return output

class Bunch(object):
    def __init__(self, **kwargs):
        self.__dict__ = dict([i for i in kwargs.items() if i[1] is not None])

    def getDict(self):
        return self.__dict__

    def __getattr__(self, name):
        raise AttributeError("Argument '{}' is required but not \
provided!".format(name))


@public
class MIMCRun(object):

    """
    Object for a Multi-Index Monte Carlo run.

    Data levels, moment estimators, sample sizes etc. are
    stored in the *.data attribute that is of the MIMCItrData type

    """
    def __init__(self, **kwargs):
        self.fn = Bunch(# Hierarchy=None, ExtendLvls=None,
                        # WorkModel=None, SampleAll=None,
                        Norm=np.abs)
        self.params = Bunch(**kwargs)
        self.cur_start_level = 0
        self.iters = []
        dims = np.array([len(getattr(self.params, a))
                for a in ["w", "s", "gamma", "beta"] if hasattr(self.params, a)])
        if len(dims) > 0 and np.any(dims != dims[0]):
            raise ValueError("Size of beta, w, s and gamma must be of size dim")

        # if self.params.bayesian:
        #     self.Q = Bunch(S=np.inf, W=np.inf,
        #                    w=self.params.w, s=self.params.s,
        #                    theta=np.nan)
        # else:
        #     self.Q = Bunch(theta=np.nan)

    def _get_dim(self):
        dims = np.array([len(getattr(self.params, a))
                for a in ["w", "s", "gamma", "beta"] if hasattr(self.params, a)])
        if len(dims) > 0 and np.any(dims != dims[0]):
            raise ValueError("Size of beta, w, s and gamma must be of size dim")
        return dims[0] if len(dims) > 0 else None

    @property
    def last_itr(self):
        return self.iters[-1] if len(self.iters) > 0 else None

    @property
    def all_itr(self):
        return self.last_itr if self.params.reuse_samples else self._all_itr

    @property
    def Vl_estimate(self):
        return self.last_itr.Vl_estimate

    @property
    def Wl_estimate(self):
        return self.last_itr.calcWl()

    @property
    def Q(self):
        return self.last_itr.Q

    @property
    def bias(self):
        return self.last_itr.bias

    @property
    def stat_error(self):
        return self.last_itr.stat_error

    @property
    def iter_total_times(self):
        return np.cumsum([itr.total_time for itr in self.iters])

    @property
    def iter_calc_total_times(self):
        return np.cumsum([itr.calcTotalTime() for itr in self.iters])

    @property
    def _Ca(self):
        return norm.ppf((1+self.params.confidence)/2)

    def calcEg(self):
        return self.last_itr.calcEg()

    @property
    def total_error_est(self):
        return self.last_itr.total_error_est

    def _checkFunctions(self):
        # If self.params.reuse_samples is True then
        # all_itr will always equal last_itr
        if not hasattr(self.fn, "Hierarchy"):
            self.fn.Hierarchy = lambda lvls: get_geometric_hl(lvls,
                                                             self.params.h0inv,
                                                             self.params.beta)

        if not hasattr(self.fn, "EstimateBias"):
            if self.params.ml2r:
                self.fn.EstimateBias = lambda r=self: estimate_bias_ml2r(r)
            elif self.params.lsq_est:
                self.fn.EstimateBias = self._estimateBayesianBias
            elif self.params.bias_calc == 'setutil':
                self.fn.EstimateBias = lambda r=self: estimate_bias_setutil(r)
            elif self.params.bias_calc == 'bnd':
                self.fn.EstimateBias = lambda r=self: estimate_bias_bnd(r)
            elif self.params.bias_calc == 'abs-bnd':
                self.fn.EstimateBias = lambda r=self: estimate_bias_abs_bnd(r)

        if self.params.lsq_est and not hasattr(self.fn, "WorkModel"):
            raise NotImplementedError("Bayesian parameter fitting is only \
supported with a given work model")

        if self.fn.SampleAll is None:
            raise ValueError("Must set the sampling functions fnSampleAll")

        if not hasattr(self.fn, "ExtendLvls"):
            if np.all(np.array([hasattr(self.params, a) for a in ["w", "s", "gamma", "beta"]])):
                weights = np.log(self.params.beta) * (self.params.w +
                                                      (self.params.gamma -
                                                       self.params.s)/2.)
                weights /= np.sum(weights, axis=0)
            elif self._get_dim() is not None or self.params.min_dim > 0:
                d = self._get_dim()
                if d is None:
                    d = self.params.min_dim
                weights = np.ones(d) / d
            else:
                raise ValueError("No default ExtendLvls for ")

            profCalc = setutil.TDFTProfCalculator(weights)
            self.fn.ExtendLvls = lambda s=self: extend_prof_lvls(s,
                                                                 profCalc,
                                                                 s.params.min_lvl)

        if not hasattr(self.fn, "WorkModel") and hasattr(self.params, "gamma"):
            if hasattr(self.params, "beta"):
                self.fn.WorkModel = lambda lvls: work_estimate(lvls,
                                                               self.params.gamma*np.log(self.params.beta))
            else:
                self.fn.WorkModel = lambda lvls: work_estimate(lvls,
                                                               self.params.gamma)

    def setFunctions(self, **kwargs):
        # fnSampleLvl(moments, mods, inds, M):
        #    Returns M, array: M sums of mods*inds, and total
        #    (linear) time it took to compute them
        # fnItrDone(i, TOLs, total_time): Called at the end of iteration
        #    i out of TOLs
        # fnWorkModel(lvls): Returns work estimate of lvls
        # fnHierarchy(lvls): Returns associated hierarchy of lvls
        for k in kwargs.keys():
            kk = k[2:] if k.startswith('fn') else k
            if kk not in ["SampleLvl", "SampleLvlSums", "SampleAll",
                          "ExtendLvls", "ItrStart", "ItrDone",
                          "WorkModel", "EstimateBias", "Hierarchy",
                          "SampleQoI", "Norm"]:
                raise KeyError("Invalid function name `{}`".format(kk))
            if kk == "SampleLvl":
                if kwargs[k] is not None:
                    self.fn.SampleAll = lambda lvls, M, moments: \
                                        default_sample_all(lvls, M,
                                                           moments, kwargs[k],
                                                           fnWorkModel=self.fn.WorkModel if hasattr(self.fn, "WorkModel") else None,
                                                           verbose=self.params.verbose)
            elif kk == "SampleLvlSums":
                self.fn.SampleAll = lambda lvls, M, moments: \
                                        default_sample_all_sums(lvls, M,
                                                                moments, kwargs[k],
                                                                verbose=self.params.verbose)
            else:
                setattr(self.fn, kk, kwargs[k])

    @staticmethod
    def addOptionsToParser(parser, pre='-mimc_', additional=True, default_bayes=True):
        mimcgrp = parser.add_argument_group('MIMC', 'Arguments to control MIMC logic')

        class Store_as_array(argparse._StoreAction):
            def __call__(self, parser, namespace, values, option_string=None):
                setattr(namespace, self.dest, np.array(values))

        def add_store(name, action="store", dest=None, **kwargs):
            if dest is None:
                dest = name
            if "default" in kwargs and "help" in kwargs:
                kwargs["help"] += " (default: {})".format(kwargs["default"])
            mimcgrp.add_argument(pre + name, dest=dest, action=action,
                                 **kwargs)

        add_store('ml2r', default=False, action='store_true',
                  help="Enable ML2R. Only For 1 dim, requires 'w'")
        add_store('min_dim', type=int, default=0, help="Number of minimum dimensions used in the index set.")
        add_store('verbose', type=int, default=0, help="Verbose output")
        add_store('lsq_est', action='store_true',
                  help="Use Bayesian fitting to estimate bias, variance and optimize number \
of levels in every iteration. This is based on CMLMC.")
        add_store('dynamic_first_lvl', action='store_true',
                  help="If true, the first level will be found dynamically")
        add_store('moments', type=int, default=4, help="Number of moments to compute")
        add_store('discard_samples', action='store_false',
                  dest='reuse_samples',
                  default=True, help="Discard samples between iterations")
        add_store('bias_calc', type=str, default='bnd',
                  choices=['setutil', 'bnd', 'abs-bnd'],
                  help="setutil, bnd or abs-bnd")
        add_store('const_theta', action='store_true',
                  help="Use the same theta for all iterations")
        add_store('confidence', type=float, default=0.95,
                  help="Parameter to control confidence level")
        add_store('theta', type=float, default=0.5,
                  help="Minimum theta or error splitting parameter.")
        add_store('incL', type=int, default=2,
                  help="Maximum increment of number of levels \
between iterations")
        add_store('w', nargs='+', type=float, action=Store_as_array,
                  help="Weak convergence rates. \
Not needed if a profit calculator is specified and -lsq_est is False.")
        add_store('s', nargs='+', type=float, action=Store_as_array,
                  help="Strong convergence rates.  \
Not needed if a profit calculator is specified and -lsq_est is False.")
        add_store('TOL', type=float,
                  help="The required tolerance for the MIMC run")
        add_store('beta', type=float, nargs='+', action=Store_as_array,
                  help="Level separation parameter. to be used \
with get_geometric_hl. Not needed if fnHierarchy is provided.")
        add_store('gamma', type=float, nargs='+', action=Store_as_array,
                  help="Work exponent to be used with work_estimate.\
Not needed if fnWorkModel and profit calculator are provided.")

        # The following arguments are not needed if bayes is False
        if default_bayes:
            add_store('bayes_k0', type=float, default=0.1,
                      help="Variance in prior of the constant \
in the weak convergence model. Not needed if -lsq_est is False.")
            add_store('bayes_k1', type=float, default=0.1,
                      help="Variance in prior of the constant \
in the strong convergence model. Not needed if -lsq_est is False.")
            add_store('bayes_w_sig', type=float, default=-1,
                      help="Variance in prior of the power \
in the weak convergence model, negative values lead to disabling the fitting. \
Not needed if -lsq_est is False.")
            add_store('bayes_s_sig', type=float, default=-1,
                      help="Variance in prior of the power \
in the weak convergence model, negative values lead to disabling the fitting. \
Not needed if -lsq_est is False.")
            add_store('bayes_fit_lvls', type=int, default=1000,
                      help="Maximum number of levels used to fit data. \
Not needed if -lsq_est is False.")

        # The following arguments are not always needed, and they have
        # a default value
        if additional:
            add_store('max_TOL', type=float,
                      help="The (approximate) tolerance for \
the first iteration. Not needed if TOLs is provided to doRun.")
            add_store('M0', nargs='+', type=int, default=np.array([1]),
                      action=Store_as_array,
                      help="Initial number of samples used to estimate the \
sample variance on levels when not using the Bayesian estimators. \
Not needed if a profit calculator is provided.")
            add_store('M0_coeff', type=float, default=0.,
                      help="The reduction of initial number of samples from\
previous estimates.")
            add_store('min_lvl', type=int, default=3,
                      help="The initial number of levels to run \
the first iteration. Not needed if a profit calculator is provided.")
            add_store('max_add_itr', type=int, default=2,
                      help="Maximum number of additonal iterations\
to run when the MIMC is expected to but is not converging.\
Not needed if TOLs is provided to doRun.")
            add_store('r1', type=float, default=np.sqrt(2),
                      help="A parameters to control to tolerance sequence \
for tolerance larger than TOL. Not needed if TOLs is provided to doRun.")
            add_store('r2', type=float, default=1.1,
                      help="A parameters to control to tolerance sequence \
for tolerance smaller than TOL. Not needed if TOLs is provided to doRun.")
            add_store('h0inv', type=float, nargs='+', action=Store_as_array,
                      default=2,
                      help="Minimum element size get_geometric_hl. \
Not needed if fnHierarchy is provided.")
        return mimcgrp

    def output(self, verbose):
        output = ''
        if verbose >= VERBOSE_INFO:
            output += '''Eg             = {}
Bias           = {:.12g} | {:.12g}
Stat. err      = {:.12g} | {:.12g}
Error est.     = {:.12g} | {:.12g}
Iteration Work = {:.12g}
Iteration Time = {:.12g}
TotalTime      = {:.12g}
max_lvl        = {}
'''.format(str(self.last_itr.calcEg()),
           self.bias, (1-self.params.theta) * self.last_itr.TOL,
           self.stat_error, self.params.theta * self.last_itr.TOL,
           self.total_error_est, self.last_itr.TOL,
           self.last_itr.calcTotalWork(),
           self.last_itr.total_time,
           self.iter_total_times[-1],
           self.last_itr._lvls.to_sparse_matrix().max(axis=0).todense())

        if verbose < VERBOSE_DEBUG:
            print(output, end="")
            return

        output += self.last_itr.to_string(self.fn.Norm)
        print(output, end="")

    def fnNorm1(self, x):
        """ Helper function to return norm of a single element
        """
        return self.fn.Norm(np.array([x]))[0]

    def estimateMonteCarloSampleCount(self, TOL):
        # TODO: Should only be based on base_level
        theta = self._calcTheta(TOL, self.bias)
        V = self.Vl_estimate[self.last_itr.lvls_find([])]
        if np.isnan(V):
            return np.nan
        return np.maximum(np.reshape(self.params.M0, (1,))[-1],
                          int(np.ceil((theta * TOL / self._Ca)**-2 * V)))

    ################## Bayesian specific functions
    def _estimateBayesianBias(self, L=None):
        L = L or self.all_itr.lvls_count-1
        if L <= 1:
            raise Exception("Must have at least 2 levels")
        return self.Q.W * self._get_hl(L)[-1]**self.Q.w[0]

    def _get_hl(self, L):
        lvls = np.arange(0, L+1).reshape((-1, 1))
        return  self.fn.Hierarchy(lvls=lvls).reshape(1, -1)[0]

    def _estimateBayesianVl(self, L=None):
        if np.sum(self.all_itr.M, axis=0) == 0:
            return self.fn.Norm(self.all_itr.calcVl())
        # TODO: This assumes that we have correct ordering
        # of levels
        oL = self.all_itr.lvls_count-1
        L = L or oL
        if L <= 1:
            raise Exception("Must have at least 2 levels")
        included = self.all_itr.M > 0
        included[0] = False
        included = np.nonzero(included)[0]
        hl = self._get_hl(L)
        M = self.all_itr.M[included]
        s1 = self.all_itr.psums_delta[included, 0]
        m1 = self.all_itr.calcEl()[included]
        s2 = self.all_itr.psums_delta[included, 1]
        mu = self.Q.W*(hl[included-1]**self.Q.w[0] - hl[included]**self.Q.w[0])

        Lambda = 1./(self.Q.S*(hl[:-1]**(self.Q.s[0]/2.) -
                               hl[1:]**(self.Q.s[0]/2.))**2)

        tmpM = np.concatenate((self.all_itr.M[1:], np.zeros(L-oL)))
        G_3 = self.params.bayes_k1 * Lambda + tmpM/2.0
        G_4 = self.params.bayes_k1*np.ones(L)
        G_4[included-1] += 0.5*(self.fn.Norm(s2 - s1*m1*2 + s1*m1) + \
                                M*self.params.bayes_k0*(
                                    self.fn.Norm(m1)-mu)**2/
                                (self.params.bayes_k0+M) )
        Vl = np.empty(L+1)
        act = self.all_itr.active_lvls
        fine_Vl = self.fn.Norm(self.all_itr.calcFineCentralMoment(moment=2))
        Vl[0] = fine_Vl[0]
        Vl[1:] = (G_4 / G_3)
        Vl[np.nonzero(act==0)[0]] = fine_Vl[np.nonzero(act==0)[0]]
        Vl[act < 0] = np.nan
        return Vl

    def _estimateQParams(self):
        if not self.params.lsq_est:
            return
        if np.sum(self.all_itr.M, axis=0) == 0:
            return   # Cannot really estimate anything without at least some samples
        L = self.all_itr.lvls_count-1
        if L <= 1:
            raise Exception("Must have at least 2 levels")
        hl = self._get_hl(L)
        included = np.nonzero(\
                np.logical_and(self.all_itr.M > 0,
                               np.arange(0, self.all_itr.lvls_count)
                               >= np.maximum(1, L-self.params.bayes_fit_lvls)))[0]
        M = self.all_itr.M[included]
        s1 = self.all_itr.psums_delta[included, 0]
        s2 = self.all_itr.psums_delta[included, 1]
        t1 = hl[included-1]**self.Q.w[0] - hl[included]**self.Q.w[0]
        t2 = (hl[included-1]**(self.Q.s[0]/2.) - hl[included]**(self.Q.s[0]/2.))**-2

        self.Q.W = self.fnNorm1(np.sum(s1 * _expand(t1, 0, s1.shape) *
                                       _expand(t2, 0, s1.shape), axis=0) /
                                np.sum(M * t1**2 * t2, axis=0) )
        self.Q.S = (np.sum(self.fn.Norm(s2* _expand(t2, 0, s2.shape)) -
                           self.fn.Norm(s1*_expand(self.Q.W*t1*2*t2,
                                                 0, s1.shape)), axis=0) +
                    np.sum(M*self.Q.W**2*t1**2*t2)) / np.sum(M)
        if self.params.bayes_w_sig > 0 or self.params.bayes_s_sig > 0:
            # TODO: Estimate w=q_1, s=q_2
            raise NotImplemented("TODO, estimate w and s")

    def _estimateOptimalL(self, TOL):
        assert self.params.lsq_est, "MIMC should be Bayesian to \
estimate optimal number of levels"
        minL = self.last_itr.lvls_count
        minWork = np.inf
        LsRange = range(self.last_itr.lvls_count,
                        self.last_itr.lvls_count+1+self.params.incL)
        for L in LsRange:
            bias_est = self._estimateBayesianBias(L)
            if bias_est >= TOL and L < LsRange[-1]:
                continue
            lvls = setutil.VarSizeList(np.arange(0, L+1).reshape((-1, 1)), min_dim=1)
            Wl = self.fn.WorkModel(lvls=lvls)
            M = _calcTheoryM(TOL, theta=self._calcTheta(TOL,
                                                        bias_est),
                             Vl=self._estimateBayesianVl(L),
                             Wl=Wl,
                             active_lvls=self.last_itr.active_lvls,
                             Ca=self._Ca)
            totalWork = np.sum(Wl*M)
            if totalWork < minWork:
                minL = L
                minWork = totalWork
        return minL
    ################## END: Bayesian specific function

    def _estimateAll(self):
        self._estimateQParams()
        self.iters[-1].Vl_estimate = np.empty(len(self.last_itr.get_lvls()))
        self.iters[-1].Vl_estimate.fill(np.nan)
        self.iters[-1].stat_error = np.nan
        if self.iters[-1].moments >= 2:
            act = self.last_itr.active_lvls >= 0
            if self.params.lsq_est:
                self.iters[-1].Vl_estimate = self._estimateBayesianVl()
            else:
                self.iters[-1].Vl_estimate[act] = self.fn.Norm(self.all_itr.calcVl()[act])
            self.iters[-1].stat_error = np.inf if np.any(self.last_itr.M[act] == 0) \
                                    else self._Ca * \
                                         np.sqrt(np.sum(self.Vl_estimate[act]
                                                        / self.last_itr.M[act]))
        self.iters[-1].bias = self.fn.EstimateBias()

    def _extendLevels(self, new_lvls=None):
        todoM = None
        if new_lvls is not None:
            self.last_itr.lvls_add_from_list(new_lvls)
        else:
            todoM = self.fn.ExtendLvls()
        self.last_itr._levels_added()
        self.all_itr._levels_added()
        self.update_ml2r_weights()
        if todoM is None:
            # TODO: Allow for array todoM
            todoM = np.ones(self.last_itr.lvls_count, dtype=np.int) * self.params.M0[0]
        return np.maximum(self.last_itr.M, todoM)
        # newTodoM = self.params.M0
        # if self.params.M0_coeff > 0:
        #     newTodoM = np.maximum(newTodoM, (int)(self.params.M0_coeff *
        #                                           np.min(self.last_itr.M)))
        # if len(newTodoM) < self.last_itr.lvls_count:
        #     newTodoM = np.pad(newTodoM,
        #                       (0,self.last_itr.lvls_count-len(newTodoM)), 'constant',
        #                       constant_values=newTodoM[-1])
        # return np.concatenate((self.last_itr.M[:prev], newTodoM[prev:self.last_itr.lvls_count]))

    #@profile
    def _genSamples(self, totalM):
        totalM = totalM.copy()
        lvls = self.last_itr.get_lvls()
        lvls_count = self.last_itr.lvls_count
        assert(lvls_count == len(lvls))
        t = np.zeros(lvls_count)
        active = np.logical_and(totalM > self.last_itr.M,
                                self.last_itr.active_lvls >= 0)
        totalM[np.logical_not(active)] = 0    # No need to do any samples
        totalM[active] -= self.last_itr.M[active]
        if np.sum(totalM) == 0:
            return False
        calcM, psums_delta, psums_fine, \
            total_time, total_work = self.fn.SampleAll(lvls, totalM,
                                                       self.last_itr.moments)
        for i in range(0, lvls_count):
            if calcM[i] <= 0:
                continue
            self.last_itr.addSamples(i, calcM[i], psums_delta[i], \
                                     psums_fine[i], total_time[i],
                                     total_work[i])
            if self.last_itr != self.all_itr:
                self.all_itr.addSamples(i, calcM[i], psums_delta[i], \
                                        psums_fine[i], total_time[i],
                                        total_work[i])
        self._estimateAll()
        return True

    def _calcTheta(self, TOL, bias_est):
        if not self.params.const_theta:
            return np.maximum((1 - bias_est/TOL) if TOL > 0 else
                              np.inf, self.params.theta)
        return self.params.theta

    def _check_levels(self):
        if not self.params.dynamic_first_lvl:
            return
        if self.last_itr.lvls_max_dim() > 1:
            raise NotImplementedError("Dynamic first level is not implemented for more than one dimension")
        if self.cur_start_level >= self.last_itr.lvls_count-1:
            return   # Not enough levels to skip.
        # Y: Old var              (level 1)
        # X: Control variate      (level 0)
        # Z: Y-X                  (difference)
        # \sqrt{W_Z V_Z} + \sqrt{W_X V_X} \leq \sqrt{W_Y V_Y}
        deltaWl = self.last_itr.calcWl()
        deltaVl = self.fn.Norm(self.last_itr.calcVl())
        fineVl = self.fn.Norm(self.last_itr.calcFineCentralMoment(moment=2))
        VW_Z = deltaVl[self.cur_start_level+1]*deltaWl[self.cur_start_level+1]
        VW_Y = fineVl[self.cur_start_level+1]*deltaWl[self.cur_start_level+1]
        VW_X = fineVl[self.cur_start_level]*deltaWl[self.cur_start_level]
        if np.sqrt(VW_Z) + np.sqrt(VW_X) > np.sqrt(VW_Y):
            # Increase minimum level one at a time.
            self.cur_start_level += 1
            self.print_info("New start level", self.cur_start_level)
            self._update_active_lvls()
            self._estimateAll()

    def _update_active_lvls(self):
        if hasattr(self, "cur_start_level"):
           lvls = self.last_itr.get_lvls().to_dense_matrix()
           self.last_itr.active_lvls = -1*np.ones(len(lvls))
           self.last_itr.active_lvls[lvls[:, 0] > self.cur_start_level] = 1
           self.last_itr.active_lvls[lvls[:, 0] == self.cur_start_level] = 0
           self.update_ml2r_weights()

    def print_info(self, *args, **kwargs):
        if self.params.verbose >= VERBOSE_INFO:
            print(*args, **kwargs)
    def print_debug(self, *args, **kwargs):
        if self.params.verbose >= VERBOSE_DEBUG:
            print(*args, **kwargs)

    def is_itr_tol_satisfied(self, itr=None):
        if itr is None:
            itr = self.last_itr
        return itr.total_error_est < np.maximum(self.params.TOL, itr.TOL)

    def update_ml2r_weights(self):
        if not self.params.ml2r:
            return
        ell = self.last_itr.get_lvls().to_dense_matrix()
        assert ell.shape[1] == 1, "ML2R is supported for one dimensional problems only."
        ell = ell[self.last_itr.active_lvls >= 0, 0]
        L = np.max(ell) - np.min(ell)
        alpha = float(self.params.w[0] * np.log(self.params.beta[0]))
        self.last_itr.weights = np.ones(self.last_itr.lvls_count)
        from . import ipdb
        ipdb.embed()
        self.last_itr.weights[self.last_itr.active_lvls >= 0] = calc_ml2r_weights(alpha, L)

    def doRun(self, TOLs=None):
        timer = Timer()
        self._checkFunctions()
        finalTOL = self.params.TOL
        if TOLs is None:
            TOLs = [finalTOL] if not hasattr(self.params, "max_TOL") \
                   else get_tol_sequence(finalTOL, self.params.max_TOL,
                                         max_additional_itr=self.params.max_add_itr,
                                         r1=self.params.r1,
                                         r2=self.params.r2)
        if not all(x >= y for x, y in zip(TOLs, TOLs[1:])):
            raise Exception("Tolerances must be decreasing")

        def less(a, b, rel_tol=1e-09, abs_tol=0.0):
            return a-b <= max(rel_tol * max(abs(a), abs(b)), abs_tol)
        add_new_iteration = False
        for TOL in TOLs:
            self.print_info("TOL", TOL)
            timer.tic()
            samples_added = False
            while True:
                # Skip adding an iteration if the previous one is empty
                timer.tic()
                # Add new iteration
                if len(self.iters) == 0:
                    self.iters.append(MIMCItrData(parent=self,
                                                  min_dim=self.params.min_dim,
                                                  moments=self.params.moments))
                    if self.params.lsq_est:
                        self.last_itr.Q = Bunch(S=np.inf, W=np.inf,
                                                w=self.params.w,
                                                s=self.params.s,
                                                theta=self.params.theta)
                    else:
                        self.last_itr.Q = Bunch(theta=self.params.theta)
                    if not self.params.reuse_samples:
                        self._all_itr = self.last_itr.next_itr()
                elif add_new_iteration:
                    self.iters.append(self.last_itr.next_itr())

                add_new_iteration = False
                self.last_itr.TOL = TOL

                if hasattr(self.fn, "ItrStart"):
                    self.fn.ItrStart()

                gc.collect()

                # TODO: This is a temporary solution. Essentially sometimes the
                # user would like to discard samples when the first level changes
                # Right now this is done in level extension.
                # Added levels if bayesian
                if self.params.lsq_est and self.last_itr.lvls_count > 0:
                    # TODO: Do we really need this?
                    L = self._estimateOptimalL(TOL)
                    if L > self.last_itr.lvls_count:
                        self._extendLevels(new_lvls=np.arange(
                            self.last_itr.lvls_count, L+1).reshape((-1, 1)))
                        self._update_active_lvls()
                        self._estimateAll()
                else:
                    # Bias is not satisfied (or this is the first iteration)
                    # Add more levels
                    newTodoM = self._extendLevels()
                    if newTodoM is None:
                        self.print_info("WARNING: MIMC did not converge with the maximum number of levels")
                        break
                    self._update_active_lvls()
                    # TODO: We might not need newTodoM is some of the
                    # levels are inactive. This is needed for MIMC, not MLMC
                    samples_added = self._genSamples(newTodoM) or samples_added

                self.Q.theta = self._calcTheta(TOL, self.bias)
                not self._check_levels()

                todoM = _calcTheoryM(TOL, self.Q.theta,
                                     self.Vl_estimate,
                                     self.last_itr.calcWl(),
                                     self.last_itr.active_lvls,
                                     Ca=self._Ca)
                self.print_debug("theta", self.Q.theta)
                self.print_debug("Wl: ", self.Wl_estimate)
                self.print_debug("Vl: ", self.Vl_estimate)
                self.print_debug("New M: ", todoM)
                if not self.params.reuse_samples:
                    self.last_itr.zero_samples()

                samples_added = self._genSamples(todoM) or samples_added
                self.last_itr.total_time = timer.toc()
                self.output(verbose=self.params.verbose)
                self.print_info("------------------------------------------------")
                if samples_added:
                    if self.fn.ItrDone is not None:
                        # ItrDone should return true if a new iteration must be added
                        add_new_iteration = self.fn.ItrDone()
                if self.params.lsq_est or self.is_itr_tol_satisfied():
                    break
            self.print_info("MIMC iteration for TOL={} took {} seconds".format(TOL, timer.toc()))
            self.print_info("################################################")
            if less(TOL, finalTOL) and self.total_error_est <= finalTOL:
                break
            add_new_iteration = True  # Force adding a new iteration for new tolerance

        self.print_info("MIMC run for TOL={} took {} seconds".format(finalTOL, timer.toc()))

    def reduceDims(self, dim_to_keep, profits, bins=np.inf):
        new_run = MIMCRun()
        new_run.fn = self.fn
        new_run.params = self.params

        itr = self.last_itr
        new_run.iters.append(MIMCItrData(parent=new_run,
                                         min_dim=self.params.min_dim,
                                         moments=self.params.moments))
        new_itr = new_run.last_itr
        new_itr.bias = itr.bias
        new_itr.stat_error = itr.stat_error
        new_itr.exact_error = itr.exact_error
        new_itr.TOL = itr.exact_error
        new_itr.total_time = itr.exact_error
        new_itr.Q = itr.Q
        if hasattr(itr, "db_data"):
            new_itr.db_data = itr.db_data

        # The new index is the index of the iteration
        if isinstance(dim_to_keep, np.ndarray) and dim_to_keep.dtype == np.bool:
            dim_to_keep = np.nonzero(dim_to_keep)[0]
        else:
            dim_to_keep = np.array(dim_to_keep, np.uint)

        dim_to_discard = np.ones(self.last_itr.lvls_max_dim(), dtype=np.bool)
        dim_to_discard[dim_to_keep] = False
        dim_to_discard = np.nonzero(dim_to_discard)[0]

        dicard_indset, discard_indices = self.last_itr._lvls.reduce_set(dim_to_discard)
        keep_indset, keep_indices = self.last_itr._lvls.reduce_set(dim_to_keep)

        max_dim = len(dim_to_keep)
        prev_new_count = 0
        prev_old_count = 0

        # adjust profits
        min_dist = 0
        for ii, ind in enumerate(keep_indset):
            sel = keep_indices == ii
            prof_base = profits[self.last_itr.lvls_find(ind)]
            profits[sel] -= prof_base
            if np.sum(sel) > 1:
                min_dist = np.maximum(min_dist, np.max(np.diff(np.sort(profits[sel]))))

        max_profits, min_profits = np.max(profits), np.min(profits)
        max_bins = np.floor((max_profits-min_profits) / min_dist)
        bins = np.minimum(bins, max_bins)
        prof_step = (max_profits-min_profits) / bins

        for ind_idx in xrange(len(keep_indset)):
            keep_sel = np.nonzero(keep_indices == ind_idx)[0]
            ind_profits = profits[keep_sel]
            min_prof = min_profits
            set_idx = 0
            while True:
                sel_prof = np.logical_and(ind_profits > min_prof,
                                          ind_profits <= min_prof+prof_step)
                if np.sum(sel_prof) == 0:
                    assert(np.sum(ind_profits > min_prof) == 0)
                    break
                # Add index to set
                j, data = keep_indset.get_item(ind_idx, dim=-1)
                new_itr.lvls_add_from_list(j=[np.concatenate((j, [max_dim]))],
                                           inds=[np.concatenate((data, [set_idx]))])
                # Combine these
                ##### Combine deltas
                sel = keep_sel[sel_prof]
                scale = (1./itr.M[sel])[[slice(None)] + [np.newaxis]*(len(itr.psums_delta.shape)-1)]
                lvl_idx = new_itr.lvls_count-1
                new_itr.addSamples(lvl_idx, 1,
                                   np.sum(itr.psums_delta[sel] * scale, axis=0),
                                   np.sum(itr.psums_fine[sel] * scale, axis=0),
                                   np.sum(itr.tT[sel]),
                                   np.sum(itr.tW[sel]))
                new_itr.Vl_estimate[lvl_idx] = np.sum(itr.Vl_estimate[sel])
                set_idx += 1
                min_prof += prof_step
        assert(np.all(new_itr._lvls.check_admissibility())) # TEMP
        return new_run


@public
def work_estimate(lvls, gamma):
    return np.prod(np.exp(lvls.to_dense_matrix(base=0)*gamma), axis=1)

@public
def expand_delta(lvl):
    """
    This routine takes a multi-index level and produces
    a list of levels and weights that are needed to evaluate
    the multi-dimensional difference estimator.

    For example, in the MLMC setting the function
    x,y = expand_delta([N])

    sets y to an array of [N] and [N-1]
    and x to an array of 1 and -1.

    """
    lvl = np.array(lvl, dtype=np.int)
    seeds = list()
    for i in range(0, lvl.shape[0]):
        if lvl[i] == 0:
            seeds.append([0])
        else:
            seeds.append([0, 1])
    inds = np.array(list(itertools.product(*seeds)), dtype=np.int)
    mods = (2 * np.sum(lvl) % 2 - 1) * (2 * (np.sum(inds, axis=1) % 2) - 1)
    return mods, np.tile(lvl, (inds.shape[0], 1)) - inds


@public
def get_geometric_hl(lvls, h0inv, beta):
    return beta**(-np.array(lvls, dtype=np.float))/h0inv


@public
def get_tol_sequence(TOL, maxTOL, max_additional_itr=1, r1=2, r2=1.1):
    # number of iterations until TOL
    eni = int(-(np.log(TOL)-np.log(maxTOL))/np.log(r1))
    return np.concatenate((TOL*r1**np.arange(eni, -1, -1),
                           TOL*r2**-np.arange(1, max_additional_itr+1)))

@public
def get_optimal_hl(mimc):
    # TODO: Get formula from HajiAli 2015, Optimizing MLMC hierarchies
    raise NotImplemented("TODO: get_optimal_hl")


@public
def calcMIMCRate(w, s, gamma):
    w, s, gamma = np.array(w), np.array(s), np.array(gamma)
    d = len(w)
    if len(s) != d or len(gamma) != d:
        raise ValueError("w,s and gamma must have the same size")
    delta = (gamma-s)/(2*w)
    zeta = np.max(delta)
    xi = np.min((2.*w - s) / gamma)
    d2 = np.sum(delta == 0)
    dz = np.sum(delta == zeta)
    rate = -2.*(1. + np.maximum(0, zeta))
    log_rate = np.nan
    if (zeta <= 0 and zeta < xi) or (zeta == xi and zeta == 0 and d <= 2):
        log_rate = 2*d2
    elif zeta > 0 and xi > 0:
        log_rate = 2*(dz-1)*(zeta+1)
    elif zeta == 0 and xi == 0 and d > 2:
        log_rate = 2*d2 + d - 3
    elif zeta > 0 and xi == 0:
        log_rate = d-1 + 2*(dz-1)*(1+zeta)
    return rate, log_rate

def extend_prof_lvls(run, profCalc, min_lvls):
    # TODO: Do we need to add more levels that are beyond the minimum
    # number of levels
    # Only add levels if bias is not satisfied
    if run.bias < (1-run.params.theta) * run.last_itr.TOL:
        return
    added = 0
    lvls = run.last_itr.get_lvls()
    if len(lvls) == 0:
        # add seed
        lvls.add_from_list([[]])
        added += 1
    while added < 1 or (len(lvls) < min_lvls):
        if hasattr(profCalc, "max_dim"):
            lvls.expand_set(profCalc, max_dim=profCalc.max_dim)
        else:
            lvls.expand_set(profCalc, -1)
        added += 1


def default_sample_all(lvls, M, moments, fnSample, fnWorkModel=None,
                       verbose=0):
    # fnSampleLvl(inds, M) -> Returns a matrix of size (M, len(ind)) and
    # the time estimate
    lvls_count = len(lvls)
    psums_delta = np.empty(lvls_count, dtype=object)
    psums_fine = np.empty(lvls_count, dtype=object)
    calcM = np.zeros(lvls_count, dtype=np.int)
    total_time = np.zeros(lvls_count)
    total_work = np.zeros(lvls_count)
    if fnWorkModel is not None:
        work_per_lvl = fnWorkModel(lvls)
    for i in range(0, lvls_count):
        if M[i] <= 0:
            continue
        if verbose >= VERBOSE_DEBUG:
            print("Sampling", M[i], " at level ", i)
        mods, inds = expand_delta(lvls[i])
        calcM[i] = 0
        total_time[i] = 0
        total_work[i] = 0
        p = moments
        psums_delta[i] = _empty_obj()
        psums_fine[i] = _empty_obj()
        while calcM[i] < M[i]:
            ret = fnSample(inds=inds, M=M[i]-calcM[i])
            assert isinstance(ret, tuple), "Must return a tuple of (solves, time, work)"
            values = ret[0]
            samples_time = ret[1]
            if len(ret) < 3:  # Backward compatibility
                assert(fnWorkModel is not None)
                samples_work = work_per_lvl[i] * len(values)
            else:
                samples_work = ret[2]
            total_time[i] += samples_time
            total_work[i] += samples_work
            # psums_delta_j = \sum_{i} (\sum_{k} mod_k values_{i,k})**p_j
            delta = np.sum(values * \
                           _expand(mods, 1, values.shape),
                           axis=1)
            # TODO: We should optimize to use the fact that p is integer with
            # specific numbers. Use cumprod
            A1 = np.tile(delta, (p,) + (1,)*len(delta.shape) )
            A2 = np.tile(values[:, 0], (p,) + (1,)*len(delta.shape) )
            psums_delta[i] += np.sum(np.cumprod(A1, axis=0), axis=1)
            psums_fine[i] += np.sum(np.cumprod(A2, axis=0), axis=1)
            calcM[i] += values.shape[0]
    return calcM, psums_delta, psums_fine, total_time, total_work


def default_sample_all_sums(lvls, M, moments, fnSampleSums,
                            fnWorkModel=None,
                            verbose=0):
    # fnSampleLvl(inds, M) -> Returns a matrix of size (M, len(ind)) and
    # the time estimate
    lvls_count = len(lvls)
    psums_delta = np.empty(lvls_count, dtype=object)
    psums_fine = np.empty(lvls_count, dtype=object)
    calcM = np.zeros(lvls_count, dtype=np.int)
    total_time = np.zeros(lvls_count)
    total_work = np.zeros(lvls_count)
    if fnWorkModel is not None:
        work_per_lvl = fnWorkModel(lvls)
    for i in range(0, lvls_count):
        if M[i] <= 0:
            continue
        if verbose >= VERBOSE_DEBUG:
            print("Sampling", M[i], " at level ", i)
        mods, inds = expand_delta(lvls[i])
        calcM[i] = 0
        total_time[i] = 0
        total_work[i] = 0
        p = moments
        psums_delta[i] = _empty_obj()
        psums_fine[i] = _empty_obj()
        while calcM[i] < M[i]:
            cur_M, cur_fsums, cur_diffsums, cur_totalTime, cur_totalWork = fnSampleSums(inds=inds, M=M[i]-calcM[i])
            total_work[i] += cur_totalWork
            total_time[i] += cur_totalTime
            psums_fine[i] += cur_fsums
            if cur_diffsums is None:
                psums_delta[i] += cur_fsums
            else:
                psums_delta[i] += cur_diffsums
            calcM[i] += cur_M
    return calcM, psums_delta, psums_fine, total_time, total_work


def estimate_bias_setutil(run):
    itr = run.last_itr
    El = itr.calcEl()
    El[itr.active_lvls == 0] = np.inf
    El_norm = np.empty(itr.lvls_count)
    El_norm.fill(np.inf)
    act = itr.active_lvls > 0
    El_norm[act] = run.fn.Norm(El[act])
    bias = itr.get_lvls().estimate_bias(El_norm)
    return bias

def estimate_bias_abs_bnd(run):
    itr = run.last_itr
    El = itr.calcEl()
    El[itr.active_lvls == 0] = np.inf
    El_norm = np.empty(len(itr.lvls_count))
    El_norm.fill(np.inf)
    act = itr.active_lvls >= 0
    El_norm[act] = self.fn.Norm(El[act])
    return np.sum(El_norm[itr.get_lvls().is_boundary()])


def estimate_bias_bnd(run):
    itr = run.last_itr
    El = itr.calcEl()
    El[itr.active_lvls == 0] = np.inf
    El_bnd = El[itr.get_lvls().is_boundary()]
    if np.any([e is None for e in El_bnd]):
        bias = np.inf
    else:
        bias = run.fnNorm1(np.sum(El_bnd))
    return bias


def estimate_bias_ml2r(run):
    itr = run.last_itr
    El = itr.calcEl(weighted=False)[itr.active_lvls >= 0]
    ell = itr.get_lvls().to_dense_matrix()
    assert ell.shape[1] == 1, "ML2R is supported for one dimensional problems only."
    ell = ell[itr.active_lvls >= 0, 0]
    L = np.max(ell) - np.min(ell)
    alpha = float(run.params.w[0] * np.log(run.params.beta[0]))
    slicer = [slice(None)] + [None]*(len(El.shape)-1)
    WL = calc_ml2r_weights(alpha, L)
    WL_1 = calc_ml2r_weights(alpha, L-1)
    return np.abs(np.sum(WL_1[slicer] * El[:-1, ...]) - np.sum(WL[slicer]*El))


    # def update_ml2r_weights(self):
    #     if not self.params.ml2r:
    #         return
    #     ell = self.last_itr.get_lvls().to_dense_matrix()
    #     assert ell.shape[1] == 1, "ML2R is supported for one dimensional problems only."
    #     ell = ell[self.last_itr.active_lvls >= 0, 0]
    #     L = np.max(ell) - np.min(ell)
    #     alpha = float(self.params.w[0] * np.log(self.params.beta[0]))
    #     self.last_itr.weights = np.ones(self.last_itr.lvls_count)
    #     self.last_itr.weights[self.last_itr.active_lvls >= 0] = calc_ml2r_weights(alpha, L)
