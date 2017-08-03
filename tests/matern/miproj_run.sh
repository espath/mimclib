#!/bin/bash

# make
# rm -f data.sql
VERBOSE="-db False -mimc_verbose 1 "
SEL_CMD="$1"
EXAMPLE="$2"
if [ -z "$EXAMPLE" ]; then
    EXAMPLE='sf-kink'
fi
DB_CONN='-db_engine mysql -db_name mimc -db_host 129.67.187.118 '
BASETAG="$EXAMPLE-"
COMMON="-qoi_seed 0 -ksp_rtol 1e-15 -ksp_type gmres  $DB_CONN "
EST_CMD="python miproj_esterr.py $COMMON "
RUN_CMD="OPENBLAS_NUM_THREADS=1 python miproj_run.py -qoi_example $EXAMPLE \
       -mimc_TOL 0 -qoi_seed 0 -mimc_gamma 1 -mimc_h0inv 3 \
       -miproj_reuse_samples True $VERBOSE $COMMON "

function run_cmd {
    echo  $RUN_CMD -miproj_max_lvl $3 \
          -qoi_dim $2 -qoi_df_nu $4 \
          ${@:5} -db_tag $BASETAG$2-$4$1
}

function plot_cmd {
    echo ../plot_prog.py $DB_CONN \
         -o output/self-$BASETAG$2-$4$1.pdf \
         -verbose True -all_itr True -qoi_exact_tag $BASETAG$2-$4$1 \
         -db_tag $BASETAG$2-$4$1
}

function plotest_cmd {
    echo ../plot_prog.py "$DB_CONN" \
         -o "output/$BASETAG$2-$4$1.pdf" \
         -verbose True -all_itr True \
         -db_tag "$BASETAG$2-$4$1"
}


function errest_cmd {
    echo $EST_CMD -miproj_max_lvl "$3" \
         -qoi_dim "$2" -qoi_df_nu "$4" \
         "${@:5}" -db_tag "$BASETAG$2-$4$1" \
         "; " ../plot_prog.py "$DB_CONN" \
         -o "output/$BASETAG$2-$4$1.pdf" \
         -verbose True -all_itr True -db_tag "$BASETAG$2-$4$1"
}

function all_cmds {
    if [ "$SEL_CMD" = "plot" ]; then
        plot_cmd "${@:1}"
    elif [ "$SEL_CMD" = "est" ]; then
        errest_cmd "${@:1}"
    elif [ "$SEL_CMD" = "plot_est" ]; then
        plotest_cmd "${@:1}"
    elif [ "$SEL_CMD" = "run" ]; then
        run_cmd "${@:1}"
    fi;
}

if [ "$EXAMPLE" = "sf-matern" ]; then
    CMN='-mimc_beta 2 -miproj_set_dexp 2.0794'
    for nu in 6.5 #3.5 6.5 4.5 2.5
    do
        max_lvl=9
        z=`echo "$nu+0.5" | bc`
        all_cmds -adapt 1 $max_lvl $nu -mimc_min_dim 1 $CMN
        all_cmds "" 1 $max_lvl $nu -miproj_set_sexp $z -miproj_set xi_exp -mimc_min_dim 1 $CMN
        for (( i=0; i<=$max_lvl; i++ ))
        do
            all_cmds -fix-$i 1 $(($i+1)) $nu -miproj_fix_lvl $i \
                      -miproj_set xi_exp -mimc_min_dim 0 $CMN
        done
    done
fi;

if [ "$EXAMPLE" = "sf-kink" ]; then
    CMN='-qoi_sigma -1 -mimc_beta 1.4142135623730951 -qoi_scale 0.5 '
    for N in 2
    do
        max_lvl=12
	    ALPHA=3.
	    THETA=`echo "$N/2" | bc -l`

        # all_cmds "-td-theory" 2 $max_lvl $N -miproj_max_vars $N \
        #          -miproj_s_alpha $ALPHA -miproj_s_proj_sample_ratio 0. \
        #          -miproj_s_theta $THETA -miproj_d_beta 1. -miproj_d_gamma 1. \
        #          -miproj_set apriori -mimc_min_dim 1 $CMN  -miproj_double_work True

        all_cmds "-adapt" 2 $max_lvl $N -miproj_max_vars $N \
                 -miproj_s_proj_sample_ratio 0. -miproj_set_maxadd 1 \
                 -miproj_set apriori-adapt -mimc_min_dim 1 $CMN

        # all_cmds "-adapt-time" 2 $max_lvl $N -miproj_max_vars $N \
        #          -miproj_s_proj_sample_ratio 0. -miproj_set_maxadd 1 \
        #          -miproj_time True -miproj_set apriori-adapt -mimc_min_dim 1 $CMN
        # all_cmds "-noproj" 2 $max_lvl $N -miproj_max_vars $N \
        #          -miproj_s_alpha $ALPHA -miproj_s_proj_sample_ratio 0. \
        #          -miproj_set apriori -mimc_min_dim 1 $CMN  -miproj_double_work True

        # all_cmds -adapt 2 $max_lvl $N -miproj_max_vars $N -mimc_min_dim 1 \
        #          -miproj_set_maxadd 1 $CMN

        # max_lvl=9
        # for (( i=0; i<=$max_lvl; i++ ))
        # do
        #     # all_cmds -fix-adapt-$i 2 $(($i+2)) $N -mimc_min_dim 0 -miproj_max_vars $N \
        #     #          -miproj_fix_lvl $i -miproj_set adaptive \
        #     #          $CMN

        #     all_cmds -fix-$i 2 $((($i+2))) $N -mimc_min_dim 0 -miproj_max_vars $N \
        #              -miproj_fix_lvl $i -miproj_set apriori -miproj_s_proj_sample_ratio 0. \
        #              $CMN -miproj_double_work True
        # done
    done
fi;
