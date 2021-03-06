#!/usr/bin/python
import numpy as np
import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=True)
    parser.register('type', 'bool',
                    lambda v: v.lower() in ("yes", "true", "t", "1"))
    parser.add_argument("-tries", type=int, action="store",
                        default=0, help="Number of realizations")
    parser.add_argument("-mimc_dim", type=int, action="store",
                        default=1, help="MIMC dim")

    args, unknowns = parser.parse_known_args()

    dim = args.mimc_dim
    if dim == 1:
        base = "mimc_run.py -mimc_TOL {TOL} -mimc_max_TOL 0.01  -mimc_min_dim 1 -qoi_seed {seed} \
    -mimc_theta 0.5 -mimc_M0 25 \
    -mimc_w 1 -mimc_s 2 -mimc_gamma 3 -mimc_beta 2 -mimc_h0inv 5 \
    -mimc_bayes_fit_lvls 3 -mimc_moments 4 \
    -mimc_bayesian {bayesian} ".format(bayesian="{bayesian}", TOL="{TOL}",
                                       seed="{seed}")
    elif dim == 2:
        base = "mimc_run.py -mimc_TOL {TOL} -mimc_max_TOL 0.01  -mimc_min_dim 2 -qoi_seed {seed} \
    -mimc_theta 0.5 -mimc_M0 25 -mimc_h0inv 5 5 \
    -mimc_w 1 1 -mimc_s 2 2 -mimc_gamma 2 1 -mimc_beta 2 2 \
    -mimc_bayes_fit_lvls 3 -mimc_moments 4 \
    -mimc_bayesian {bayesian} ".format(bayesian="{bayesian}", TOL="{TOL}", seed="{seed}")
    else:
        assert False, "Dim must be 1 or 2"
    base += " ".join(unknowns)

    if args.tries == 0:
        cmd_single = "python " + base + " -mimc_verbose 10 -db False "
        print(cmd_single.format(seed=0, bayesian=False, TOL=0.001))
    else:
        cmd_multi = "python " + base + " -mimc_verbose 0 -db True -db_tag {tag} "
        TOL = 5e-5
        for i in range(0, args.tries):
            print cmd_multi.format(bayesian=False,
                                   tag="mckean_{}".format(dim), TOL=TOL,
                                   seed=np.random.randint(2**32-1))
