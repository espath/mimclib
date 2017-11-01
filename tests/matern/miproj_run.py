from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import warnings
import os.path
import numpy as np
import mimclib.test
import mimclib.miproj as miproj
from mimclib import setutil
from mimclib import mimc, ipdb
import argparse

warnings.filterwarnings("error")
warnings.filterwarnings("always", category=mimclib.test.ArgumentWarning)
warnings.filterwarnings("always", category=UserWarning)

class MyRun:
    def solveFor_sin(self, alpha, arrY):
        j = np.arange(0, arrY.shape[1], dtype=np.float)
        output = np.sin(np.sum(1. / (1+((1.+j[None, :]) **
                                        (-(self.params.qoi_df_nu+0.5)) *
                                        arrY)**2.), axis=1))
        return output

    def solveFor_sf(self, alpha, arrY):
        output = np.zeros(len(arrY))
        max_dim = np.max([len(Y) for Y in arrY])
        assert(max_dim <= self.params.miproj_max_vars)
        self.sf.BeginRuns(alpha, max_dim)
        for i, Y in enumerate(arrY):
            output[i] = self.sf.SolveFor(np.array(Y))
        self.sf.EndRuns()
        return output

    def solveFor_kl1D(self, alpha, arrY):
        from kl1D import kl1D
        assert(len(alpha) == 1)
        return kl1D(arrY, 2**alpha[0], self.params.qoi_df_nu + 0.5)[:, 0]

    def solveFor_matern(self, alpha, arrY):
        from matern_fem import matern
        assert(len(alpha) == 1)
        return matern(arrY, 2**alpha[0],
                      nu=self.params.qoi_df_nu,
                      df_sig=self.params.qoi_df_sig,
                      df_L=self.params.qoi_df_L,
                      qoi_x0=self.params.qoi_x0[0],
                      qoi_sig=self.params.qoi_sigma)[:, 0]

    def solveFor_seq(self, alpha, arrY):
        if len(alpha) == 0:
            alpha = [self.params.miproj_fix_lvl] * self.params.qoi_dim
        if self.params.qoi_example.startswith('sf'):
            return self.solveFor_sf(alpha, arrY)
        if self.params.qoi_example == 'matern-py':
            return self.solveFor_matern(alpha, arrY)
        if self.params.qoi_example == 'kl1D':
            return self.solveFor_kl1D(alpha, arrY)
        if self.params.qoi_example == 'sin':
            return self.solveFor_sin(alpha, arrY)

    def mySampleQoI(self, run, lvls, M, moments):
        return self.proj.sample_all(run, lvls, M, moments,
                                    fnSample=self.solveFor_seq)

    def initRun(self, run):
        self.prev_val = 0
        self.params = run.params

        if run.params.miproj_min_vars > run.params.miproj_max_vars:
            warnings.warn("miproj_min_vars is greater than run.params.miproj_max_vars, setting both to the minimum")
            run.params.miproj_min_vars = np.minimum(run.params.miproj_min_vars,
                                                    run.params.miproj_max_vars)
            run.params.miproj_max_vars = run.params.miproj_min_vars

        if run.params.miproj_set == 'adaptive':
            fnBasisFromLvl = miproj.exp_basis_from_level
            fnBasisFromLvl = lambda beta, d=self.params.miproj_max_vars: miproj.td_basis_from_level(d, beta)
        elif run.params.miproj_set == 'apriori' or run.params.miproj_set == 'apriori-adapt':
            fnBasisFromLvl = lambda beta, d=self.params.miproj_max_vars: miproj.td_basis_from_level(d, beta)
        else:
            raise NotImplementedError("Unknown lvls to basis")

        if run.params.miproj_pts_sampler == 'optimal':
            fnSamplePoints = miproj.sample_optimal_leg_pts
            fnWeightPoints = lambda x, b: miproj.optimal_weights(b)
        elif run.params.miproj_pts_sampler == 'arcsine':
            fnSamplePoints = miproj.sample_arcsine_pts
            fnWeightPoints = lambda x, b: miproj.arcsine_weights(x)
        else:
            raise NotImplementedError("Unknown points sampler")

        if run.params.min_dim > 0:
            fnWorkModel = lambda lvls, w=np.log(run.params.beta) * run.params.qoi_dim*run.params.gamma: \
                          mimc.work_estimate(lvls, w)
        else:
            fnWorkModel = lambda lvls, \
                          w=run.params.beta ** \
                             (run.params.qoi_dim*run.params.gamma*run.params.miproj_fix_lvl):\
                          w * np.ones(len(lvls))

        self.proj = miproj.MIWProjSampler(d=run.params.min_dim,
                                          min_dim=run.params.miproj_min_vars,
                                          max_dim=run.params.miproj_max_vars,
                                          fnBasis=miproj.legendre_polynomials,
                                          proj_sample_ratio=run.params.miproj_s_proj_sample_ratio,
                                          fnBasisFromLvl=fnBasisFromLvl,
                                          fnSamplePoints=fnSamplePoints,
                                          fnWeightPoints=fnWeightPoints,
                                          fnWorkModel=fnWorkModel,
                                          reuse_samples=run.params.miproj_reuse_samples)
        self.proj.init_mimc_run(run)
        if self.params.qoi_example.startswith('sf'):
            from matern import SField_Matern
            SField_Matern.Init()
            self.sf = SField_Matern(run.params)

        run.setFunctions(ExtendLvls=lambda r=run: self.extendLvls(run),
                         fnNorm=lambda arr: np.array([x.norm() for x in arr]))

        self.profit_calc = None
        if run.params.miproj_set == 'xi_exp':
            dexp = 0
            if run.params.min_dim > 0:
                qoi_N = run.params.miproj_max_vars
                dexp = (run.params.miproj_d_beta +
                        run.params.miproj_d_gamma) / \
                        (qoi_N + run.params.miproj_s_alpha)
            self.profit_calc = setutil.MIProfCalculator([dexp] * run.params.min_dim,
                                                        run.params.miproj_set_xi,
                                                        1.,
                                                        run.params.miproj_set_mul)
        elif run.params.miproj_set == 'apriori':
            if run.params.min_dim == 0:
                self.profit_calc = setutil.TDFTProfCalculator([1.])
            else:
                self.profit_calc_td = setutil.TDFTProfCalculator([run.params.miproj_d_beta +
                                                                  run.params.miproj_d_gamma,
                                                                  run.params.miproj_s_theta + run.params.miproj_s_alpha])
                # self.profit_calc = setutil.MIProjProfCalculator(run.params.min_dim,
                #                                                 run.params.miproj_max_vars,
                #                                                 run.params.miproj_d_beta,
                #                                                 run.params.miproj_d_gamma,
                #                                                 run.params.miproj_s_alpha,
                #                                                 run.params.miproj_s_theta,
                #                                                 run.params.miproj_s_proj_sample_ratio)
                self.profit_calc = self.profit_calc_td
        else:
            assert run.params.miproj_set == 'adaptive' or run.params.miproj_set == 'apriori-adapt'

    def extendLvls(self, run):
        lvls = run.last_itr.get_lvls()
        max_added = None
        if run.params.miproj_set == 'apriori' or run.params.miproj_set == 'apriori-adapt':
            max_dim = run.params.min_dim+1
        else:
            max_dim = 5 + (0 if len(lvls) == 0 else np.max(lvls.get_dim()))
            max_dim = np.minimum(run.params.miproj_max_vars + run.params.min_dim,
                                 np.maximum(run.params.miproj_min_vars + run.params.min_dim,
                                            max_dim))
        if self.profit_calc is None:
            # Adaptive
            error = run.fn.Norm(run.last_itr.calcEl())
            if run.params.miproj_time:
                work = run.last_itr.calcTl()
            else:
                work = run.last_itr.calcWl()
            prof = setutil.calc_log_prof_from_EW(error, work)
            max_added = run.params.miproj_set_maxadd
            lvls.expand_set(prof, max_dim=max_dim, max_added=max_added)
            self.proj.update_index_set(lvls)
        else:
            # non-adaptive
            prof = self.profit_calc
            prev_total_work = self.proj.estimateWork()
            while True:
                lvls.expand_set(prof, max_dim=max_dim, max_added=max_added)
                self.proj.update_index_set(lvls)
                new_total_work = self.proj.estimateWork()
                if not self.params.miproj_double_work or new_total_work >= 2*prev_total_work:
                    break
        if len(lvls) >= 1:
            max_lvls = lvls.to_sparse_matrix().max(axis=0).todense()
            if max_lvls[0, 0] > run.params.miproj_max_lvl:
                return False  # No more levels
        return True

    def addExtraArguments(self, parser):
        class store_as_array(argparse._StoreAction):
            def __call__(self, parser, namespace, values, option_string=None):
                setattr(namespace, self.dest, np.array(values))

        qoigrp = parser.add_argument_group('qoi', 'Arguments to control QoI')
        pre = '-qoi_'
        qoigrp.add_argument(pre + "dim", type=int, default=1, action="store")
        qoigrp.add_argument(pre + "example", type=str, default="sf-matern",
                            action="store")
        qoigrp.add_argument(pre + "a0", type=float, default=0., action="store")
        qoigrp.add_argument(pre + "f0", type=float, default=1., action="store")
        qoigrp.add_argument(pre + "df_nu", type=float, default=1., action="store")
        qoigrp.add_argument(pre + "df_L", type=float, default=1., action="store")
        qoigrp.add_argument(pre + "df_sig", type=float, default=0.5, action="store")
        qoigrp.add_argument(pre + "scale", type=float, default=10., action="store")
        qoigrp.add_argument(pre + "sigma", type=float, default=0.2, action="store")
        qoigrp.add_argument(pre + "x0", type=float, nargs='+',
                            default=np.array([0.3,0.4,0.6]),
                            action=store_as_array)

        migrp = parser.add_argument_group('miproj', 'Arguments to control projection')
        pre = '-miproj_'
        migrp.add_argument(pre + "double_work",
                           default=False, action="store_true")
        migrp.add_argument(pre + "set", type=str, default="adaptive",
                           action="store")
        migrp.add_argument(pre + "set_xi", type=float, default=2.,
                           action="store")
        migrp.add_argument(pre + "set_mul", type=float, default=1.,
                           action="store")

        migrp.add_argument(pre + "d_beta", type=float, action="store")
        migrp.add_argument(pre + "d_gamma", type=float, action="store")
        migrp.add_argument(pre + "s_alpha", type=float, action="store")
        migrp.add_argument(pre + "s_theta", type=float, action="store")
        migrp.add_argument(pre + "s_proj_sample_ratio", type=float,
                           action="store", default=0.)

        migrp.add_argument(pre + "set_maxadd", type=int, default=30,
                           action="store")
        migrp.add_argument(pre + "pts_sampler", type=str,
                           default="optimal", action="store")
        migrp.add_argument(pre + "discard_samples",
                           dest="miproj_reuse_samples",
                           default=True,
                           action="store_false")
        migrp.add_argument(pre + "fix_lvl", type=int, default=3, action="store")
        migrp.add_argument(pre + "min_vars", type=int, default=10, action="store")
        migrp.add_argument(pre + "max_vars", type=int, default=10**6, action="store")
        migrp.add_argument(pre + "max_lvl", type=int, default=1000, action="store")
        migrp.add_argument(pre + "time", default=False, action="store_true")


    def ItrDone(self, db, run_id, run):
        if db is not None:
            run.last_itr.userdata = self.proj.user_data
            db.writeRunData(run_id, run,
                            iteration_idx=len(run.iters)-1)
        self.proj.user_data = []
        return True

if __name__ == "__main__":
    from mimclib import ipdb
    ipdb.set_excepthook()

    run = MyRun()
    mirun = mimclib.test.RunStandardTest(fnSampleAll=run.mySampleQoI,
                                         fnAddExtraArgs=run.addExtraArguments,
                                         fnInit=run.initRun,
                                         fnItrDone=run.ItrDone)
    if mirun.params.qoi_example.startswith('sf'):
        from matern import SField_Matern
        SField_Matern.Final()