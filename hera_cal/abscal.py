# -*- coding: utf-8 -*-
# Copyright 2020 the HERA Project
# Licensed under the MIT License
"""
abscal.py
---------

Calibrate measured visibility
data to a visibility model using
linearizations of the (complex)
antenna-based calibration equation:

V_ij,xy^data = g_i_x * conj(g_j_y) * V_ij,xy^model.

Complex-valued parameters are broken into amplitudes and phases as:

V_ij,xy^model = exp(eta_ij,xy^model + i * phi_ij,xy^model)
g_i_x = exp(eta_i_x + i * phi_i_x)
g_j_y = exp(eta_j_y + i * phi_j_y)
V_ij,xy^data = exp(eta_ij,xy^data + i * phi_ij,xy^data)

where {i,j} index antennas and {x,y} are the polarization of
the i-th and j-th antenna respectively.
"""
import os
from collections import OrderedDict as odict
import copy
import argparse
import numpy as np
import operator
from functools import reduce
from scipy import signal, interpolate, spatial
from scipy.optimize import brute, minimize
from pyuvdata import UVCal, UVData
import linsolve
import warnings

from . import version
from .apply_cal import calibrate_in_place
from .smooth_cal import pick_reference_antenna, rephase_to_refant
from .flag_utils import synthesize_ant_flags
from .noise import predict_noise_variance_from_autos
from . import utils
from . import redcal
from . import io
from . import apply_cal
from .datacontainer import DataContainer
from .utils import echo, polnum2str, polstr2num, reverse_bl, split_pol, split_bl, join_bl

PHASE_SLOPE_SOLVERS = ['linfit', 'dft']  # list of valid solvers for global_phase_slope_logcal
IDEALIZED_BL_TOL = 1e-8  # bl_error_tol for redcal.get_reds when using antenna positions calculated from reds


def abs_amp_logcal(model, data, wgts=None, verbose=True, return_gains=False, gain_ants=[]):
    """
    calculate absolute (array-wide) gain amplitude scalar
    with a linear solver using the logarithmically linearized equation:

    ln|V_ij,xy^data / V_ij,xy^model| = eta_x + eta_y

    where {i,j} index antenna numbers and {x,y} index polarizations
    of the i-th and j-th antennas respectively.

    Parameters:
    -----------
    model : visibility data of refence model, type=DataContainer
            keys are antenna-pair + polarization tuples, Ex. (1, 2, 'nn').
            values are complex ndarray visibilities.
            these must be 2D arrays, with [0] axis indexing time
            and [1] axis indexing frequency.

    data : visibility data of measurements, type=DataContainer
           keys are antenna pair + pol tuples (must match model), values are
           complex ndarray visibilities matching shape of model

    wgts : weights of data, type=DataContainer, [default=None]
           keys are antenna pair + pol tuples (must match model), values are real floats
           matching shape of model and data

    return_gains : boolean. If True, convert result into a dictionary of gain waterfalls.

    gain_ants : list of ant-pol tuples for return_gains dictionary

    verbose : print output, type=boolean, [default=False]

    Output:
    -------
    if not return_gains:
        fit : dictionary with 'eta_{}' key for amplitude scalar for {} polarization,
                which has the same shape as the ndarrays in the model
    else:
        gains: dictionary with gain_ants as keys and gain waterfall arrays as values
    """
    echo("...configuring linsolve data for abs_amp_logcal", verbose=verbose)

    # get keys from model and data dictionary
    keys = sorted(set(model.keys()) & set(data.keys()))

    # abs of amplitude ratio is ydata independent variable
    ydata = odict([(k, np.log(np.abs(data[k] / model[k]))) for k in keys])

    # make weights if None
    if wgts is None:
        wgts = odict()
        for i, k in enumerate(keys):
            wgts[k] = np.ones_like(ydata[k], dtype=np.float)

    # fill nans and infs
    fill_dict_nans(ydata, wgts=wgts, nan_fill=0.0, inf_fill=0.0)

    # setup linsolve equations
    # a{} is a dummy variable to prevent linsolve from overwriting repeated measurements
    eqns = odict([(k, "a{}*eta_{}+a{}*eta_{}".format(i, split_pol(k[-1])[0],
                                                     i, split_pol(k[-1])[1])) for i, k in enumerate(keys)])
    ls_design_matrix = odict([("a{}".format(i), 1.0) for i, k in enumerate(keys)])

    # setup linsolve dictionaries
    ls_data = odict([(eqns[k], ydata[k]) for i, k in enumerate(keys)])
    ls_wgts = odict([(eqns[k], wgts[k]) for i, k in enumerate(keys)])

    # setup linsolve and run
    sol = linsolve.LinearSolver(ls_data, wgts=ls_wgts, **ls_design_matrix)
    echo("...running linsolve", verbose=verbose)
    fit = sol.solve()
    echo("...finished linsolve", verbose=verbose)

    if not return_gains:
        return fit
    else:
        return {ant: np.exp(fit['eta_{}'.format(ant[1])]).astype(np.complex) for ant in gain_ants}


def TT_phs_logcal(model, data, antpos, wgts=None, refant=None, verbose=True, zero_psi=True,
                  four_pol=False, return_gains=False, gain_ants=[]):
    """
    calculate overall gain phase and gain phase Tip-Tilt slopes (East-West and North-South)
    with a linear solver applied to the logarithmically linearized equation:

    angle(V_ij,xy^data / V_ij,xy^model) = angle(g_i_x * conj(g_j_y))
                                        = psi_x - psi_y + PHI^ew_x*r_i^ew + PHI^ns_x*r_i^ns
                                          - PHI^ew_y*r_j^ew - PHI^ns_y*r_j^ns

    where psi is the overall gain phase across the array [radians] for x and y polarizations,
    and PHI^ew, PHI^ns are the gain phase slopes across the east-west and north-south axes
    of the array in units of [radians / meter], where x and y denote the pol of the i-th and j-th
    antenna respectively. The phase slopes are polarization independent by default (1pol & 2pol cal),
    but can be merged with the four_pol parameter (4pol cal). r_i is the antenna position vector
    of the i^th antenna.

    Parameters:
    -----------
    model : visibility data of refence model, type=DataContainer
            keys are antenna-pair + polarization tuples, Ex. (1, 2, 'nn').
            values are complex ndarray visibilities.
            these must 2D arrays, with [0] axis indexing time
            and [1] axis indexing frequency.

    data : visibility data of measurements, type=DataContainer
           keys are antenna pair + pol tuples (must match model), values are
           complex ndarray visibilities matching shape of model

    wgts : weights of data, type=DataContainer, [default=None]
           keys are antenna pair + pol tuples (must match model), values are real floats
           matching shape of model and data

    refant : antenna number integer to use as a reference,
        The antenna position coordaintes are centered at the reference, such that its phase
        is identically zero across all frequencies. If None, use the first key in data as refant.

    antpos : antenna position vectors, type=dictionary
          keys are antenna integers, values are 2D
          antenna vectors in meters (preferably centered at center of array),
          with [0] index containing east-west separation and [1] index north-south separation

    zero_psi : set psi to be identically zero in linsolve eqns, type=boolean, [default=False]

    four_pol : type=boolean, even if multiple polarizations are present in data, make free
                variables polarization un-aware: i.e. one solution across all polarizations.
                This is the same assumption as 4-polarization calibration in omnical.

    verbose : print output, type=boolean, [default=False]

    return_gains : boolean. If True, convert result into a dictionary of gain waterfalls.

    gain_ants : list of ant-pol tuples for return_gains dictionary

    Output:
    -------

    if not return_gains:
        fit : dictionary with psi key for overall gain phase and Phi_ew and Phi_ns array containing
                phase slopes across the EW and NS directions of the array. There is a set of each
                of these variables per polarization.
    else:
        gains: dictionary with gain_ants as keys and gain waterfall arrays as values

    """
    echo("...configuring linsolve data for TT_phs_logcal", verbose=verbose)

    # get keys from model dictionary
    keys = sorted(set(model.keys()) & set(data.keys()))
    ants = np.unique(list(antpos.keys()))

    # angle of phs ratio is ydata independent variable
    # angle after divide
    ydata = odict([(k, np.angle(data[k] / model[k])) for k in keys])

    # make weights if None
    if wgts is None:
        wgts = odict()
        for i, k in enumerate(keys):
            wgts[k] = np.ones_like(ydata[k], dtype=np.float)

    # fill nans and infs
    fill_dict_nans(ydata, wgts=wgts, nan_fill=0.0, inf_fill=0.0)

    # center antenna positions about the reference antenna
    if refant is None:
        refant = keys[0][0]
    assert refant in ants, "reference antenna {} not found in antenna list".format(refant)
    antpos = odict(list(map(lambda k: (k, antpos[k] - antpos[refant]), antpos.keys())))

    # setup antenna position terms
    r_ew = odict(list(map(lambda a: (a, "r_ew_{}".format(a)), ants)))
    r_ns = odict(list(map(lambda a: (a, "r_ns_{}".format(a)), ants)))

    # setup linsolve equations
    if four_pol:
        eqns = odict([((ant1, ant2, pol), 
                       "psi_{}*a1 - psi_{}*a2 + Phi_ew*{} + Phi_ns*{} - Phi_ew*{} - Phi_ns*{}"
                       "".format(split_pol(pol)[0], split_pol(pol)[1], r_ew[ant1],
                                 r_ns[ant1], r_ew[ant2], r_ns[ant2])) 
                      for i, (ant1, ant2, pol) in enumerate(keys)])
    else:
        eqns = odict([((ant1, ant2, pol), 
                       "psi_{}*a1 - psi_{}*a2 + Phi_ew_{}*{} + Phi_ns_{}*{} - Phi_ew_{}*{} - Phi_ns_{}*{}"
                       "".format(split_pol(pol)[0], split_pol(pol)[1], split_pol(pol)[0],
                                 r_ew[ant1], split_pol(pol)[0], r_ns[ant1], split_pol(pol)[1],
                                 r_ew[ant2], split_pol(pol)[1], r_ns[ant2]))
                      for i, (ant1, ant2, pol) in enumerate(keys)])

    # set design matrix entries
    ls_design_matrix = odict(list(map(lambda a: ("r_ew_{}".format(a), antpos[a][0]), ants)))
    ls_design_matrix.update(odict(list(map(lambda a: ("r_ns_{}".format(a), antpos[a][1]), ants))))

    if zero_psi:
        ls_design_matrix.update({"a1": 0.0, "a2": 0.0})
    else:
        ls_design_matrix.update({"a1": 1.0, "a2": 1.0})

    # setup linsolve dictionaries
    ls_data = odict([(eqns[k], ydata[k]) for i, k in enumerate(keys)])
    ls_wgts = odict([(eqns[k], wgts[k]) for i, k in enumerate(keys)])

    # setup linsolve and run
    sol = linsolve.LinearSolver(ls_data, wgts=ls_wgts, **ls_design_matrix)
    echo("...running linsolve", verbose=verbose)
    fit = sol.solve()
    echo("...finished linsolve", verbose=verbose)

    if not return_gains:
        return fit
    else:
        return {ant: np.exp(1.0j * (np.einsum('i,ijk->jk', antpos[ant[0]][:2], 
                                              [fit['Phi_ew_{}'.format(ant[1])], fit['Phi_ns_{}'.format(ant[1])]])
                                    + fit['psi_{}'.format(ant[1])])) for ant in gain_ants}


def amp_logcal(model, data, wgts=None, verbose=True):
    """
    calculate per-antenna gain amplitude via the
    logarithmically linearized equation

    ln|V_ij,xy^data / V_ij,xy^model| = ln|g_i_x| + ln|g_j_y|
                                     = eta_i_x + eta_j_y

    where {x,y} represent the polarization of the i-th and j-th antenna
    respectively.

    Parameters:
    -----------
    model : visibility data of refence model, type=DataContainer
            keys are antenna-pair + polarization tuples, Ex. (1, 2, 'nn').
            values are complex ndarray visibilities.
            these must 2D arrays, with [0] axis indexing time
            and [1] axis indexing frequency.

    data : visibility data of measurements, type=DataContainer
           keys are antenna pair + pol tuples (must match model), values are
           complex ndarray visibilities matching shape of model

    wgts : weights of data, type=DataContainer, [default=None]
           keys are antenna pair + pol tuples (must match model), values are real floats
           matching shape of model and data

    Output:
    -------
    fit : dictionary containing eta_i = ln|g_i| for each antenna
    """
    echo("...configuring linsolve data for amp_logcal", verbose=verbose)

    # get keys from model dictionary
    keys = sorted(set(model.keys()) & set(data.keys()))

    # difference of log-amplitudes is ydata independent variable
    ydata = odict([(k, np.log(np.abs(data[k] / model[k]))) for k in keys])

    # make weights if None
    if wgts is None:
        wgts = odict()
        for i, k in enumerate(keys):
            wgts[k] = np.ones_like(ydata[k], dtype=np.float)

    # fill nans and infs
    fill_dict_nans(ydata, wgts=wgts, nan_fill=0.0, inf_fill=0.0)

    # setup linsolve equations
    eqns = odict([(k, "eta_{}_{} + eta_{}_{}".format(k[0], split_pol(k[-1])[0],
                                                     k[1], split_pol(k[-1])[1])) for i, k in enumerate(keys)])
    ls_design_matrix = odict()

    # setup linsolve dictionaries
    ls_data = odict([(eqns[k], ydata[k]) for i, k in enumerate(keys)])
    ls_wgts = odict([(eqns[k], wgts[k]) for i, k in enumerate(keys)])

    # setup linsolve and run
    sol = linsolve.LinearSolver(ls_data, wgts=ls_wgts, **ls_design_matrix)
    echo("...running linsolve", verbose=verbose)
    fit = sol.solve()
    echo("...finished linsolve", verbose=verbose)

    return fit


def phs_logcal(model, data, wgts=None, refant=None, verbose=True):
    """
    calculate per-antenna gain phase via the
    logarithmically linearized equation

    angle(V_ij,xy^data / V_ij,xy^model) = angle(g_i_x) - angle(g_j_y)
                                        = phi_i_x - phi_j_y

    where {x,y} represent the pol of the i-th and j-th antenna respectively.

    Parameters:
    -----------
    model : visibility data of refence model, type=DataContainer
            keys are antenna-pair + polarization tuples, Ex. (1, 2, 'nn').
            values are complex ndarray visibilities.
            these must 2D arrays, with [0] axis indexing time
            and [1] axis indexing frequency.

    data : visibility data of measurements, type=DataContainer
           keys are antenna pair + pol tuples (must match model), values are
           complex ndarray visibilities matching shape of model

    wgts : weights of data, type=DataContainer, [default=None]
           keys are antenna pair + pol tuples (must match model), values are real floats
           matching shape of model and data

    refant : integer antenna number of reference antenna, defult=None
        The refant phase will be set to identically zero in the linear equations.
        By default this takes the first antenna in data.

    Output:
    -------
    fit : dictionary containing phi_i = angle(g_i) for each antenna
    """
    echo("...configuring linsolve data for phs_logcal", verbose=verbose)

    # get keys from match between data and model dictionary
    keys = sorted(set(model.keys()) & set(data.keys()))

    # angle of visibility ratio is ydata independent variable
    ydata = odict([(k, np.angle(data[k] / model[k])) for k in keys])

    # make weights if None
    if wgts is None:
        wgts = odict()
        for i, k in enumerate(keys):
            wgts[k] = np.ones_like(ydata[k], dtype=np.float)

    # fill nans and infs
    fill_dict_nans(ydata, wgts=wgts, nan_fill=0.0, inf_fill=0.0)

    # setup linsolve equations
    eqns = odict([(k, "phi_{}_{} - phi_{}_{}".format(k[0], split_pol(k[2])[0],
                                                     k[1], split_pol(k[2])[1])) for i, k in enumerate(keys)])
    ls_design_matrix = odict()

    # setup linsolve dictionaries
    ls_data = odict([(eqns[k], ydata[k]) for i, k in enumerate(keys)])
    ls_wgts = odict([(eqns[k], wgts[k]) for i, k in enumerate(keys)])

    # get unique gain polarizations
    gain_pols = np.unique(list(map(lambda k: list(split_pol(k[2])), keys)))

    # set reference antenna phase to zero
    if refant is None:
        refant = keys[0][0]
    assert np.array(list(map(lambda k: refant in k, keys))).any(), "refant {} not found in data and model".format(refant)

    for p in gain_pols:
        ls_data['phi_{}_{}'.format(refant, p)] = np.zeros_like(list(ydata.values())[0])
        ls_wgts['phi_{}_{}'.format(refant, p)] = np.ones_like(list(wgts.values())[0])

    # setup linsolve and run
    sol = linsolve.LinearSolver(ls_data, wgts=ls_wgts, **ls_design_matrix)
    echo("...running linsolve", verbose=verbose)
    fit = sol.solve()
    echo("...finished linsolve", verbose=verbose)

    return fit


def delay_lincal(model, data, wgts=None, refant=None, df=9.765625e4, f0=0., solve_offsets=True, medfilt=True,
                 kernel=(1, 5), verbose=True, antpos=None, four_pol=False, edge_cut=0):
    """
    Solve for per-antenna delays according to the equation

    delay(V_ij,xy^data / V_ij,xy^model) = delay(g_i_x) - delay(g_j_y)

    Can also solve for per-antenna phase offsets with the solve_offsets kwarg.

    Parameters:
    -----------
    model : visibility data of refence model, type=DataContainer
            keys are antenna-pair + polarization tuples, Ex. (1, 2, 'nn').
            values are complex ndarray visibilities.
            these must 2D arrays, with [0] axis indexing time
            and [1] axis indexing frequency.

    data : visibility data of measurements, type=DataContainer
           keys are antenna pair + pol tuples (must match model), values are
           complex ndarray visibilities matching shape of model

    wgts : weights of data, type=DataContainer, [default=None]
           keys are antenna pair + pol tuples (must match model), values are real floats
           matching shape of model and data. These are only used to find delays from
           itegrations that are unflagged for at least two frequency bins. In this case,
           the delays are assumed to have equal weight, otherwise the delays take zero weight.

    refant : antenna number integer to use as reference
        Set the reference antenna to have zero delay, such that its phase is set to identically
        zero across all freqs. By default use the first key in data.

    df : type=float, frequency spacing between channels in Hz

    f0 : type=float, frequency of the first channel in the data (used for offsets)

    medfilt : type=boolean, median filter visiblity ratio before taking fft

    kernel : type=tuple, dtype=int, kernel for multi-dimensional median filter

    antpos : type=dictionary, antpos dictionary. antenna num as key, position vector as value.

    four_pol : type=boolean, if True, fit multiple polarizations together

    edge_cut : int, number of channels to exclude at each band edge in FFT window

    Output:
    -------
    fit : dictionary containing delay (tau_i_x) for each antenna and optionally
            offset (phi_i_x) for each antenna.
    """
    echo("...configuring linsolve data for delay_lincal", verbose=verbose)

    # get shared keys
    keys = sorted(set(model.keys()) & set(data.keys()))

    # make wgts
    if wgts is None:
        wgts = odict()
        for i, k in enumerate(keys):
            wgts[k] = np.ones_like(data[k], dtype=np.float)

    # median filter and FFT to get delays
    ratio_delays = []
    ratio_offsets = []
    ratio_wgts = []
    for i, k in enumerate(keys):
        ratio = data[k] / model[k]

        # replace nans
        nan_select = np.isnan(ratio)
        ratio[nan_select] = 0.0
        wgts[k][nan_select] = 0.0

        # replace infs
        inf_select = np.isinf(ratio)
        ratio[inf_select] = 0.0
        wgts[k][inf_select] = 0.0

        # get delays
        dly, offset = utils.fft_dly(ratio, df, f0=f0, wgts=wgts[k], medfilt=medfilt, kernel=kernel, edge_cut=edge_cut)

        # set nans to zero
        rwgts = np.nanmean(wgts[k], axis=1, keepdims=True)
        isnan = np.isnan(dly)
        dly[isnan] = 0.0
        rwgts[isnan] = 0.0
        offset[isnan] = 0.0

        ratio_delays.append(dly)
        ratio_offsets.append(offset)
        ratio_wgts.append(rwgts)

    ratio_delays = np.array(ratio_delays)
    ratio_offsets = np.array(ratio_offsets)
    ratio_wgts = np.array(ratio_wgts)

    # form ydata
    ydata = odict(zip(keys, ratio_delays))

    # form wgts
    ywgts = odict(zip(keys, ratio_wgts))

    # setup linsolve equation dictionary
    eqns = odict([(k, 'tau_{}_{} - tau_{}_{}'.format(k[0], split_pol(k[2])[0],
                                                     k[1], split_pol(k[2])[1])) for i, k in enumerate(keys)])

    # setup design matrix dictionary
    ls_design_matrix = odict()

    # setup linsolve data dictionary
    ls_data = odict([(eqns[k], ydata[k]) for i, k in enumerate(keys)])
    ls_wgts = odict([(eqns[k], ywgts[k]) for i, k in enumerate(keys)])

    # get unique gain polarizations
    gain_pols = np.unique(list(map(lambda k: [split_pol(k[2])[0], split_pol(k[2])[1]], keys)))

    # set reference antenna phase to zero
    if refant is None:
        refant = keys[0][0]
    assert np.array(list(map(lambda k: refant in k, keys))).any(), "refant {} not found in data and model".format(refant)

    for p in gain_pols:
        ls_data['tau_{}_{}'.format(refant, p)] = np.zeros_like(list(ydata.values())[0])
        ls_wgts['tau_{}_{}'.format(refant, p)] = np.ones_like(list(ywgts.values())[0])

    # setup linsolve and run
    sol = linsolve.LinearSolver(ls_data, wgts=ls_wgts, **ls_design_matrix)
    echo("...running linsolve", verbose=verbose)
    fit = sol.solve()
    echo("...finished linsolve", verbose=verbose)

    # setup linsolve parameters
    ydata = odict(zip(keys, ratio_offsets))
    eqns = odict([(k, 'phi_{}_{} - phi_{}_{}'.format(k[0], split_pol(k[2])[0],
                                                     k[1], split_pol(k[2])[1])) for i, k in enumerate(keys)])
    ls_data = odict([(eqns[k], ydata[k]) for i, k in enumerate(keys)])
    ls_wgts = odict([(eqns[k], ywgts[k]) for i, k in enumerate(keys)])
    ls_design_matrix = odict()
    for p in gain_pols:
        ls_data['phi_{}_{}'.format(refant, p)] = np.zeros_like(list(ydata.values())[0])
        ls_wgts['phi_{}_{}'.format(refant, p)] = np.ones_like(list(ywgts.values())[0])
    sol = linsolve.LinearSolver(ls_data, wgts=ls_wgts, **ls_design_matrix)
    echo("...running linsolve", verbose=verbose)
    offset_fit = sol.solve()
    echo("...finished linsolve", verbose=verbose)
    fit.update(offset_fit)

    return fit


def delay_slope_lincal(model, data, antpos, wgts=None, refant=None, df=9.765625e4, medfilt=True,
                       kernel=(1, 5), verbose=True, four_pol=False, edge_cut=0, 
                       return_gains=False, gain_ants=[]):
    """
    Solve for an array-wide delay slope according to the equation

    delay(V_ij,xy^data / V_ij,xy^model) = dot(T_x, r_i) - dot(T_y, r_j)

    This does not solve for per-antenna delays, but rather a delay slope across the array.

    Parameters:
    -----------
    model : visibility data of refence model, type=DataContainer
            keys are antenna-pair + polarization tuples, Ex. (1, 2, 'nn').
            values are complex ndarray visibilities.
            these must 2D arrays, with [0] axis indexing time
            and [1] axis indexing frequency.

    data : visibility data of measurements, type=DataContainer
           keys are antenna pair + pol tuples (must match model), values are
           complex ndarray visibilities matching shape of model

    antpos : type=dictionary, antpos dictionary. antenna num as key, position vector as value.

    wgts : weights of data, type=DataContainer, [default=None]
           keys are antenna pair + pol tuples (must match model), values are real floats
           matching shape of model and data. These are only used to find delays from
           itegrations that are unflagged for at least two frequency bins. In this case,
           the delays are assumed to have equal weight, otherwise the delays take zero weight.

    refant : antenna number integer to use as a reference,
        The antenna position coordaintes are centered at the reference, such that its phase
        is identically zero across all frequencies. If None, use the first key in data as refant.

    df : type=float, frequency spacing between channels in Hz

    medfilt : type=boolean, median filter visiblity ratio before taking fft

    kernel : type=tuple, dtype=int, kernel for multi-dimensional median filter

    four_pol : type=boolean, if True, fit multiple polarizations together

    edge_cut : int, number of channels to exclude at each band edge of vis in FFT window

    return_gains : boolean. If True, convert result into a dictionary of gain waterfalls.

    gain_ants : list of ant-pol tuples for return_gains dictionary

    Output:
    -------
    if not return_gains:
        fit : dictionary containing delay slope (T_x) for each pol [seconds / meter].
    else:
        gains: dictionary with gain_ants as keys and gain waterfall arrays as values
    """
    echo("...configuring linsolve data for delay_slope_lincal", verbose=verbose)

    # get shared keys
    keys = sorted(set(model.keys()) & set(data.keys()))
    ants = np.unique(list(antpos.keys()))

    # make wgts
    if wgts is None:
        wgts = odict()
        for i, k in enumerate(keys):
            wgts[k] = np.ones_like(data[k], dtype=np.float)

    # center antenna positions about the reference antenna
    if refant is None:
        refant = keys[0][0]
    assert refant in ants, "reference antenna {} not found in antenna list".format(refant)
    antpos = odict(list(map(lambda k: (k, antpos[k] - antpos[refant]), antpos.keys())))

    # median filter and FFT to get delays
    ratio_delays = []
    ratio_offsets = []
    ratio_wgts = []
    for i, k in enumerate(keys):
        ratio = data[k] / model[k]

        # replace nans
        nan_select = np.isnan(ratio)
        ratio[nan_select] = 0.0
        wgts[k][nan_select] = 0.0

        # replace infs
        inf_select = np.isinf(ratio)
        ratio[inf_select] = 0.0
        wgts[k][inf_select] = 0.0

        # get delays
        dly, _ = utils.fft_dly(ratio, df, wgts=wgts[k], medfilt=medfilt, kernel=kernel, edge_cut=edge_cut)

        # set nans to zero
        rwgts = np.nanmean(wgts[k], axis=1, keepdims=True)
        isnan = np.isnan(dly)
        dly[isnan] = 0.0
        rwgts[isnan] = 0.0

        ratio_delays.append(dly)
        ratio_wgts.append(rwgts)

    ratio_delays = np.array(ratio_delays)
    ratio_wgts = np.array(ratio_wgts)

    # form ydata
    ydata = odict(zip(keys, ratio_delays))

    # form wgts
    ywgts = odict(zip(keys, ratio_wgts))

    # setup antenna position terms
    r_ew = odict(list(map(lambda a: (a, "r_ew_{}".format(a)), ants)))
    r_ns = odict(list(map(lambda a: (a, "r_ns_{}".format(a)), ants)))

    # setup linsolve equations
    if four_pol:
        eqns = odict([(k, "T_ew*{} + T_ns*{} - T_ew*{} - T_ns*{}"
                       "".format(r_ew[k[0]], r_ns[k[0]], r_ew[k[1]], r_ns[k[1]])) for i, k in enumerate(keys)])
    else:
        eqns = odict([(k, "T_ew_{}*{} + T_ns_{}*{} - T_ew_{}*{} - T_ns_{}*{}"
                       "".format(split_pol(k[2])[0], r_ew[k[0]], split_pol(k[2])[0], r_ns[k[0]],
                                 split_pol(k[2])[1], r_ew[k[1]], split_pol(k[2])[1], r_ns[k[1]]))
                      for i, k in enumerate(keys)])

    # set design matrix entries
    ls_design_matrix = odict(list(map(lambda a: ("r_ew_{}".format(a), antpos[a][0]), ants)))
    ls_design_matrix.update(odict(list(map(lambda a: ("r_ns_{}".format(a), antpos[a][1]), ants))))

    # setup linsolve data dictionary
    ls_data = odict([(eqns[k], ydata[k]) for i, k in enumerate(keys)])
    ls_wgts = odict([(eqns[k], ywgts[k]) for i, k in enumerate(keys)])

    # setup linsolve and run
    sol = linsolve.LinearSolver(ls_data, wgts=ls_wgts, **ls_design_matrix)
    echo("...running linsolve", verbose=verbose)
    fit = sol.solve()
    echo("...finished linsolve", verbose=verbose)

    if not return_gains:
        return fit
    else:
        freqs = np.arange(list(data.values())[0].shape[1]) * df
        return {ant: np.exp(np.einsum('i,ijk,k->jk', antpos[ant[0]][:2], 
                                      [fit['T_ew_{}'.format(ant[1])], fit['T_ns_{}'.format(ant[1])]], 
                                      freqs) * 2j * np.pi) for ant in gain_ants}


def dft_phase_slope_solver(xs, ys, data, flags=None):
    '''Solve for spatial phase slopes across an array by looking for the peak in the DFT.
    This is analogous to the method in utils.fft_dly(), except its in 2D and does not 
    assume a regular grid for xs and ys.

    Arguments:
        xs: 1D array of x positions (e.g. of antennas or baselines)
        ys: 1D array of y positions (must be same length as xs)
        data: ndarray of complex numbers to fit with a phase slope. The first dimension must match 
            xs and ys, but subsequent dimensions will be preserved and solved independently. 
            Any np.nan in data is interpreted as a flag.
        flags: optional array of flags of data not to include in the phase slope solver.

    Returns:
        slope_x, slope_y: phase slopes in units of 1/[xs] where the best fit phase slope plane
            is np.exp(2.0j * np.pi * (xs * slope_x + ys * slope_y)). Both have the same shape 
            the data after collapsing along the first dimension.
    '''

    # use the minimum and maximum difference between positions to define the search range and sampling in Fourier space
    deltas = [((xi - xj)**2 + (yi - yj)**2)**.5 for i, (xi, yi) in enumerate(zip(xs, ys)) 
              for (xj, yj) in zip(xs[i + 1:], ys[i + 1:])]
    search_slice = slice(-1.0 / np.min(deltas), 1.0 / np.min(deltas), 1.0 / np.max(deltas))

    # define cost function
    def dft_abs(k, x, y, z):
        return -np.abs(np.dot(z, np.exp(-2.0j * np.pi * (x * k[0] + y * k[1]))))

    # set up flags, treating nans as flags
    if flags is None:
        flags = np.zeros_like(data, dtype=bool)
    flags = flags | np.isnan(data)

    # loop over data, minimizing the cost function
    dflat = data.reshape((len(xs), -1))
    fflat = flags.reshape((len(xs), -1))
    slope_x = np.zeros_like(dflat[0, :].real)
    slope_y = np.zeros_like(dflat[0, :].real)
    for i in range(dflat.shape[1]):
        if not np.all(np.isnan(dflat[:, i])):
            dft_peak = brute(dft_abs, (search_slice, search_slice), 
                             (xs[~fflat[:, i]], ys[~fflat[:, i]], 
                              dflat[:, i][~fflat[:, i]]), finish=minimize)
            slope_x[i] = dft_peak[0]
            slope_y[i] = dft_peak[1]
    return slope_x.reshape(data.shape[1:]), slope_y.reshape(data.shape[1:])


def global_phase_slope_logcal(model, data, antpos, solver='linfit', wgts=None, refant=None, 
                              verbose=True, tol=1.0, edge_cut=0, return_gains=False, gain_ants=[]):
    """
    Solve for a frequency-independent spatial phase slope using the equation

    median_over_freq(angle(V_ij,xy^data / V_ij,xy^model)) = dot(Phi_x, r_i) - dot(Phi_y, r_j)

    Parameters:
    -----------
    model : visibility data of refence model, type=DataContainer
            keys are antenna-pair + polarization tuples, Ex. (1, 2, 'nn').
            values are complex ndarray visibilities.
            these must 2D arrays, with [0] axis indexing time
            and [1] axis indexing frequency.

    data : visibility data of measurements, type=DataContainer
           keys are antenna pair + pol tuples (must match model), values are
           complex ndarray visibilities matching shape of model

    antpos : type=dictionary, antpos dictionary. antenna num as key, position vector as value.

    solver : 'linfit' uses linsolve to fit phase slope across the array,
             'dft' uses a spatial Fourier transform to find a phase slope 

    wgts : weights of data, type=DataContainer, [default=None]
           keys are antenna pair + pol tuples (must match model), values are real floats
           matching shape of model and data. These are only used to find delays from
           itegrations that are unflagged for at least two frequency bins. In this case,
           the delays are assumed to have equal weight, otherwise the delays take zero weight.

    refant : antenna number integer to use as a reference,
        The antenna position coordaintes are centered at the reference, such that its phase
        is identically zero across all frequencies. If None, use the first key in data as refant.

    verbose : print output, type=boolean, [default=False]

    tol : type=float, baseline match tolerance in units of baseline vectors (e.g. meters)

    edge_cut : int, number of channels to exclude at each band edge in phase slope solver

    return_gains : boolean. If True, convert result into a dictionary of gain waterfalls.

    gain_ants : list of ant-pol tuples for return_gains dictionary

    Output:
    -------
    if not return_gains:
        fit : dictionary containing frequency-indpendent phase slope, e.g. Phi_ns_x
              for each position component and polarization [radians / meter].
    else:
        gains : dictionary with gain_ants as keys and gain waterfall arrays as values
    """
    # check solver and edgecut
    assert solver in PHASE_SLOPE_SOLVERS, "Unrecognized solver {}".format(solver)
    echo("...configuring global_phase_slope_logcal for the {} algorithm".format(solver), verbose=verbose)
    assert 2 * edge_cut < list(data.values())[0].shape[1] - 1, "edge_cut cannot be >= Nfreqs/2 - 1"

    # get keys from model and data dictionaries
    keys = sorted(set(model.keys()) & set(data.keys()))
    ants = np.unique(list(antpos.keys()))

    # make weights if None and make flags
    if wgts is None:
        wgts = odict()
        for i, k in enumerate(keys):
            wgts[k] = np.ones_like(data[k], dtype=np.float)
    flags = DataContainer({k: ~wgts[k].astype(np.bool) for k in wgts})

    # center antenna positions about the reference antenna
    if refant is None:
        refant = keys[0][0]
    assert refant in ants, "reference antenna {} not found in antenna list".format(refant)
    antpos = odict(list(map(lambda k: (k, antpos[k] - antpos[refant]), antpos.keys())))

    # average data over baselines
    _reds = redcal.get_pos_reds(antpos, bl_error_tol=tol)
    ap = data.antpairs()
    reds = []
    for _red in _reds:
        red = [bl for bl in _red if bl in ap]
        if len(red) > 0:
            reds.append(red)
    avg_data, avg_flags, _ = utils.red_average(data, reds=reds, flags=flags, inplace=False)
    red_keys = list(avg_data.keys())
    avg_wgts = DataContainer({k: (~avg_flags[k]).astype(np.float) for k in avg_flags})
    avg_model, _, _ = utils.red_average(model, reds=reds, flags=flags, inplace=False)

    ls_data, ls_wgts, bls, pols = {}, {}, {}, {}
    for rk in red_keys:
        # build equation string
        eqn_str = '{}*Phi_ew_{} + {}*Phi_ns_{} - {}*Phi_ew_{} - {}*Phi_ns_{}'
        eqn_str = eqn_str.format(antpos[rk[0]][0], split_pol(rk[2])[0], antpos[rk[0]][1], split_pol(rk[2])[0],
                                 antpos[rk[1]][0], split_pol(rk[2])[1], antpos[rk[1]][1], split_pol(rk[2])[1])
        bls[eqn_str] = antpos[rk[0]] - antpos[rk[1]]
        pols[eqn_str] = rk[2]

        # calculate median of unflagged angle(data/model)
        # ls_weights are sum of non-binary weights
        dm_ratio = avg_data[rk] / avg_model[rk]
        dm_ratio /= np.abs(dm_ratio)  # This gives all channels roughly equal weight, moderating the effect of RFI (as in firstcal)
        binary_flgs = np.isclose(avg_wgts[rk], 0.0) | np.isinf(dm_ratio) | np.isnan(dm_ratio)
        avg_wgts[rk][binary_flgs] = 0.0
        dm_ratio[binary_flgs] *= np.nan
        if solver == 'linfit':  # we want to fit the angles
            ls_data[eqn_str] = np.nanmedian(np.angle(dm_ratio[:, edge_cut:(dm_ratio.shape[1] - edge_cut)]), axis=1, keepdims=True)
        elif solver == 'dft':  # we want the full complex number
            ls_data[eqn_str] = np.nanmedian(dm_ratio[:, edge_cut:(dm_ratio.shape[1] - edge_cut)], axis=1, keepdims=True)
        ls_wgts[eqn_str] = np.sum(avg_wgts[rk][:, edge_cut:(dm_ratio.shape[1] - edge_cut)], axis=1, keepdims=True)

        # set unobserved data to 0 with 0 weight
        ls_wgts[eqn_str][np.isnan(ls_data[eqn_str])] = 0
        ls_data[eqn_str][np.isnan(ls_data[eqn_str])] = 0

    if solver == 'linfit':  # build linear system for phase slopes and solve with linsolve
        # setup linsolve and run
        solver = linsolve.LinearSolver(ls_data, wgts=ls_wgts)
        echo("...running linsolve", verbose=verbose)
        fit = solver.solve()
        echo("...finished linsolve", verbose=verbose)

    elif solver == 'dft':  # look for a peak angle space by 2D DFTing across baselines
        if not np.all([split_pol(pol)[0] == split_pol(pol)[1] for pol in data.pols()]):
            raise NotImplementedError('DFT solving of global phase not implemented for abscal with cross-polarizations.')
        fit = {}
        for pol in data.pols():
            keys = [k for k in bls.keys() if pols[k] == pol]
            blx = np.array([bls[k][0] for k in keys])
            bly = np.array([bls[k][1] for k in keys])
            data_array = np.array([ls_data[k] / (ls_wgts[k] > 0) for k in keys])  # is np.nan if all flagged
            with np.errstate(divide='ignore'):  # is np.nan if all flagged
                data_array = np.array([ls_data[k] / (ls_wgts[k] > 0) for k in keys])
            slope_x, slope_y = dft_phase_slope_solver(blx, bly, data_array)
            fit['Phi_ew_{}'.format(split_pol(pol)[0])] = slope_x * 2.0 * np.pi  # 2pi matches custom_phs_slope_gain
            fit['Phi_ns_{}'.format(split_pol(pol)[0])] = slope_y * 2.0 * np.pi

    if not return_gains:
        return fit
    else:
        return {ant: np.exp(np.einsum('i,ijk,k->jk', antpos[ant[0]][:2], 
                                      [fit['Phi_ew_{}'.format(ant[1])], fit['Phi_ns_{}'.format(ant[1])]],
                                      np.ones(list(data.values())[0].shape[1])) * 1j) for ant in gain_ants}


def merge_gains(gains, merge_shared=True):
    """
    Merge a list of gain (or flag) dictionaries.

    If gains has boolean ndarray keys, interpret as flags
    and merge with a logical OR.

    Parameters:
    -----------
    gains : type=list or tuple, series of gain dictionaries with (ant, pol) keys
            and complex ndarrays as values (or boolean ndarrays if flags)
    merge_shared : type=bool, If True merge only shared keys, eliminating the others.
        Otherwise, merge all keys.

    Output:
    -------
    merged_gains : type=dictionary, merged gain (or flag) dictionary with same key-value
                   structure as input dict.
    """
    # get shared keys
    if merge_shared:
        keys = sorted(set(reduce(operator.and_, [set(g.keys()) for g in gains])))
    else:
        keys = sorted(set(reduce(operator.add, [list(g.keys()) for g in gains])))

    # form merged_gains dict
    merged_gains = odict()

    # determine if gains or flags from first entry in gains
    fedflags = False
    if gains[0][list(gains[0].keys())[0]].dtype == np.bool_:
        fedflags = True

    # iterate over keys
    for i, k in enumerate(keys):
        if fedflags:
            merged_gains[k] = reduce(operator.add, [g.get(k, True) for g in gains])
        else:
            merged_gains[k] = reduce(operator.mul, [g.get(k, 1.0) for g in gains])

    return merged_gains


def data_key_to_array_axis(data, key_index, array_index=-1, avg_dict=None):
    """
    move an index of data.keys() into the data axes

    Parameters:
    -----------
    data : type=DataContainer, complex visibility data with
        antenna-pair + pol tuples for keys, in DataContainer dictionary format.

    key_index : integer, index of keys to consolidate into data arrays

    array_index : integer, which axes of data arrays to append to

    avg_dict : DataContainer, a dictionary with same keys as data
        that will have its data arrays averaged along key_index

    Result:
    -------
    new_data : DataContainer, complex visibility data
        with key_index of keys moved into the data arrays

    new_avg_dict : copy of avg_dict. Only returned if avg_dict is not None.

    popped_keys : unique list of keys moved into data array axis
    """
    # instantiate new data object
    new_data = odict()
    new_avg = odict()

    # get keys
    keys = list(data.keys())

    # sort keys across key_index
    key_sort = np.argsort(np.array(keys, dtype=np.object)[:, key_index])
    keys = list(map(lambda i: keys[i], key_sort))
    popped_keys = np.unique(np.array(keys, dtype=np.object)[:, key_index])

    # get new keys
    new_keys = list(map(lambda k: k[:key_index] + k[key_index + 1:], keys))
    new_unique_keys = []

    # iterate over new_keys
    for i, nk in enumerate(new_keys):
        # check for unique keys
        if nk in new_unique_keys:
            continue
        new_unique_keys.append(nk)

        # get all instances of redundant keys
        ravel = list(map(lambda k: k == nk, new_keys))

        # iterate over redundant keys and consolidate into new arrays
        arr = []
        avg_arr = []
        for j, b in enumerate(ravel):
            if b:
                arr.append(data[keys[j]])
                if avg_dict is not None:
                    avg_arr.append(avg_dict[keys[j]])

        # assign to new_data
        new_data[nk] = np.moveaxis(arr, 0, array_index)
        if avg_dict is not None:
            new_avg[nk] = np.nanmean(avg_arr, axis=0)

    if avg_dict is not None:
        return new_data, new_avg, popped_keys
    else:
        return new_data, popped_keys


def array_axis_to_data_key(data, array_index, array_keys, key_index=-1, copy_dict=None):
    """
    move an axes of data arrays in data out of arrays
    and into a unique key index in data.keys()

    Parameters:
    -----------
    data : DataContainer, complex visibility data with
        antenna-pair (+ pol + other) tuples for keys

    array_index : integer, which axes of data arrays
        to extract from arrays and move into keys

    array_keys : list, list of new key from array elements. must have length
        equal to length of data_array along axis array_index

    key_index : integer, index within the new set of keys to insert array_keys

    copy_dict : DataContainer, a dictionary with same keys as data
        that will have its data arrays copied along array_keys

    Output:
    -------
    new_data : DataContainer, complex visibility data
        with array_index of data arrays extracted and moved
        into a unique set of keys

    new_copy : DataContainer, copy of copy_dict
        with array_index of data arrays copied to unique keys
    """
    # instantiate new object
    new_data = odict()
    new_copy = odict()

    # get keys
    keys = sorted(data.keys())
    new_keys = []

    # iterate over keys
    for i, k in enumerate(keys):
        # iterate overy new array keys
        for j, ak in enumerate(array_keys):
            new_key = list(k)
            if key_index == -1:
                new_key.insert(len(new_key), ak)
            else:
                new_key.insert(key_index, ak)
            new_key = tuple(new_key)
            new_data[new_key] = np.take(data[k], j, axis=array_index)
            if copy_dict is not None:
                new_copy[new_key] = copy.copy(copy_dict[k])

    if copy_dict is not None:
        return new_data, new_copy
    else:
        return new_data


def wiener(data, window=(5, 11), noise=None, medfilt=True, medfilt_kernel=(3, 9), array=False):
    """
    wiener filter complex visibility data. this might be used in constructing
    model reference. See scipy.signal.wiener for details on method.

    Parameters:
    -----------
    data : type=DataContainer, ADataContainer dictionary holding complex visibility data
           unelss array is True

    window : type=tuple, wiener-filter window along each axis of data

    noise : type=float, estimate of noise. if None will estimate itself

    medfilt : type=bool, if True, median filter data before wiener filtering

    medfilt_kernel : type=tuple, median filter kernel along each axis of data

    array : type=boolean, if True, feeding a single ndarray, rather than a dictionary

    Output: (new_data)
    -------
    new_data type=DataContainer, DataContainer dictionary holding new visibility data
    """
    # check if data is an array
    if array:
        data = {'arr': data}

    new_data = odict()
    for i, k in enumerate(list(data.keys())):
        real = np.real(data[k])
        imag = np.imag(data[k])
        if medfilt:
            real = signal.medfilt(real, kernel_size=medfilt_kernel)
            imag = signal.medfilt(imag, kernel_size=medfilt_kernel)

        new_data[k] = signal.wiener(real, mysize=window, noise=noise) + \
            1j * signal.wiener(imag, mysize=window, noise=noise)

    if array:
        return new_data['arr']
    else:
        return DataContainer(new_data)


def interp2d_vis(model, model_lsts, model_freqs, data_lsts, data_freqs, flags=None,
                 kind='cubic', flag_extrapolate=True, medfilt_flagged=True, medfilt_window=(3, 7),
                 fill_value=None):
    """
    Interpolate complex visibility model onto the time & frequency basis of
    a data visibility. See below for notes on flag propagation if flags is provided.

    Parameters:
    -----------
    model : type=DataContainer, holds complex visibility for model
        keys are antenna-pair + pol tuples, values are 2d complex visibility
        with shape (Ntimes, Nfreqs).

    model_lsts : 1D array of the model time axis, dtype=float, shape=(Ntimes,)

    model_freqs : 1D array of the model freq axis, dtype=float, shape=(Nfreqs,)

    data_lsts : 1D array of the data time axis, dtype=float, shape=(Ntimes,)

    data_freqs : 1D array of the data freq axis, dtype=float, shape=(Nfreqs,)

    flags : type=DataContainer, dictionary containing model flags. Can also contain model wgts
            as floats and will convert to booleans appropriately.

    kind : type=str, kind of interpolation, options=['linear', 'cubic', 'quintic']

    medfilt_flagged : type=bool, if True, before interpolation, replace flagged pixels with output from
                      a median filter centered on each flagged pixel.

    medfilt_window : type=tuple, extent of window for median filter across the (time, freq) axes.
                     Even numbers are rounded down to odd number.

    flag_extrapolate : type=bool, flag extrapolated data_lsts if True.

    fill_value : type=float, if fill_value is None, extrapolated points are extrapolated
                 else they are filled with fill_value.

    Output: (new_model, new_flags)
    -------
    new_model : interpolated model, type=DataContainer
    new_flags : flags associated with interpolated model, type=DataContainer

    Notes:
    ------
    If the data has flagged pixels, it is recommended to turn medfilt_flagged to True. This runs a median
    filter on the flagged pixels and replaces their values with the results, but they remain flagged.
    This happens *before* interpolation. This means that interpolation near flagged pixels
    aren't significantly biased by their presence.

    In general, if flags are fed, flags are propagated if a flagged pixel is a nearest neighbor
    of an interpolated pixel.
    """
    # make flags
    new_model = odict()
    new_flags = odict()

    # get nearest neighbor points
    freq_nn = np.array(list(map(lambda x: np.argmin(np.abs(model_freqs - x)), data_freqs)))
    time_nn = np.array(list(map(lambda x: np.argmin(np.abs(model_lsts - x)), data_lsts)))
    freq_nn, time_nn = np.meshgrid(freq_nn, time_nn)

    # get model indices meshgrid
    mod_F, mod_L = np.meshgrid(np.arange(len(model_freqs)), np.arange(len(model_lsts)))

    # raise warning on flags
    if flags is not None and medfilt_flagged is False:
        print("Warning: flags are fed, but medfilt_flagged=False. \n"
              "This may cause weird behavior of interpolated points near flagged data.")

    # ensure flags are booleans
    if flags is not None:
        if np.issubdtype(flags[list(flags.keys())[0]].dtype, np.floating):
            flags = DataContainer(odict(list(map(lambda k: (k, ~flags[k].astype(np.bool)), flags.keys()))))

    # loop over keys
    for i, k in enumerate(list(model.keys())):
        # get model array
        m = model[k]

        # get real and imag separately
        real = np.real(m)
        imag = np.imag(m)

        # median filter flagged data if desired
        if medfilt_flagged and flags is not None:
            # get extent of window along freq and time
            f_ext = int((medfilt_window[1] - 1) / 2.)
            t_ext = int((medfilt_window[0] - 1) / 2.)

            # set flagged data to nan
            real[flags[k]] *= np.nan
            imag[flags[k]] *= np.nan

            # get flagged indices
            f_indices = mod_F[flags[k]]
            l_indices = mod_L[flags[k]]

            # construct fill arrays
            real_fill = np.empty(len(f_indices), np.float)
            imag_fill = np.empty(len(f_indices), np.float)

            # iterate over flagged data and replace w/ medfilt
            for j, (find, tind) in enumerate(zip(f_indices, l_indices)):
                tlow, thi = tind - t_ext, tind + t_ext + 1
                flow, fhi = find - f_ext, find + f_ext + 1
                ll = 0
                while True:
                    # iterate until window has non-flagged data in it
                    # with a max of 10 iterations
                    if tlow < 0:
                        tlow = 0
                    if flow < 0:
                        flow = 0
                    r_med = np.nanmedian(real[tlow:thi, flow:fhi])
                    i_med = np.nanmedian(imag[tlow:thi, flow:fhi])
                    tlow -= 2
                    thi += 2
                    flow -= 2
                    fhi += 2
                    ll += 1
                    if not (np.isnan(r_med) or np.isnan(i_med)):
                        break
                    if ll > 10:
                        break
                real_fill[j] = r_med
                imag_fill[j] = i_med

            # fill real and imag
            real[l_indices, f_indices] = real_fill
            imag[l_indices, f_indices] = imag_fill

            # flag residual nans
            resid_nans = np.isnan(real) + np.isnan(imag)
            flags[k] += resid_nans

            # replace residual nans
            real[resid_nans] = 0.0
            imag[resid_nans] = 0.0

        # propagate flags to nearest neighbor
        if flags is not None:
            f = flags[k][time_nn, freq_nn]
            # check f is boolean type
            if np.issubdtype(f.dtype, np.floating):
                f = ~(f.astype(np.bool))
        else:
            f = np.zeros_like(real, bool)

        # interpolate
        interp_real = interpolate.interp2d(model_freqs, model_lsts, real, kind=kind, copy=False, bounds_error=False, fill_value=fill_value)(data_freqs, data_lsts)
        interp_imag = interpolate.interp2d(model_freqs, model_lsts, imag, kind=kind, copy=False, bounds_error=False, fill_value=fill_value)(data_freqs, data_lsts)

        # flag extrapolation if desired
        if flag_extrapolate:
            time_extrap = np.where((data_lsts > model_lsts.max() + 1e-6) | (data_lsts < model_lsts.min() - 1e-6))
            freq_extrap = np.where((data_freqs > model_freqs.max() + 1e-6) | (data_freqs < model_freqs.min() - 1e-6))
            f[time_extrap, :] = True
            f[:, freq_extrap] = True

        # rejoin
        new_model[k] = interp_real + 1j * interp_imag
        new_flags[k] = f

    return DataContainer(new_model), DataContainer(new_flags)


def rephase_vis(model, model_lsts, data_lsts, bls, freqs, inplace=False, flags=None, max_dlst=0.005, latitude=-30.72152):
    """
    Rephase model visibility data onto LST grid of data_lsts.

    Parameters:
    -----------
    model : type=DataContainer, holds complex visibility for model
        keys are antenna-pair + pol tuples, values are 2d complex visibility
        with shape (Ntimes, Nfreqs)

    model_lsts : 1D array of the LST grid in model [radians], dtype=float, shape=(Ntimes,)

    data_lsts : 1D array of the LST grid in data [radians], dtype=float, shape=(Ntimes,)

    bls : type=dictionary, ant-pair keys that holds baseline position vector in ENU frame in meters

    freqs : type=float ndarray, holds frequency channels of model in Hz.

    inplace : type=bool, if True edit data in memory, else make a copy and return

    flags : type=DataContainer, holds model flags

    max_dlst : type=bool, maximum dlst [radians] to allow for rephasing, otherwise flag data.

    latitude : type=float, latitude of array in degrees North

    Return: (new_model, new_flags)
    -------
    new_model : DataContainer with rephased model
    new_flags : DataContainer with new flags
    """
    # unravel LST array if necessary
    data_lsts[data_lsts < data_lsts[0]] += 2 * np.pi

    # get nearest neighbor model points
    lst_nn = np.array(list(map(lambda x: np.argmin(np.abs(model_lsts - x)), data_lsts)))

    # get dlst array
    dlst = data_lsts - model_lsts[lst_nn]

    # flag dlst above threshold
    flag_lst = np.zeros_like(dlst, np.bool)
    flag_lst[np.abs(dlst) > max_dlst] = True

    # make new_model and new_flags
    if inplace:
        new_model = model
    else:
        new_model = odict()
    if inplace and flags is not None:
        new_flags = flags
    else:
        new_flags = odict()

    for k in model.keys():
        m = model[k][lst_nn, :]
        new_model[k] = m
        if flags is None:
            new_flags[k] = np.zeros_like(m, np.bool)
        else:
            new_flags[k] = flags[k][lst_nn, :]
        new_flags[k][flag_lst, :] = True

    # rephase
    if inplace:
        utils.lst_rephase(new_model, bls, freqs, dlst, lat=latitude, inplace=True)
        return new_model, new_flags
    else:
        new_model = utils.lst_rephase(new_model, bls, freqs, dlst, lat=latitude, inplace=False)
        return DataContainer(new_model), DataContainer(new_flags)


def fill_dict_nans(data, wgts=None, nan_fill=None, inf_fill=None, array=False):
    """
    take a dictionary and re-fill nan and inf ndarray values.

    Parameters:
    -----------
    data : type=DataContainer, visibility dictionary in AbsCal dictionary format

    wgts : type=DataContainer, weights dictionary matching shape of data to also fill

    nan_fill : if not None, fill nans with nan_fill

    inf_fill : if not None, fill infs with inf_fill

    array : type=boolean, if True, data is a single ndarray to perform operation on
    """
    if array:
        if nan_fill is not None:
            nan_select = np.isnan(data)
            data[nan_select] = nan_fill
            if wgts is not None:
                wgts[nan_select] = 0.0
        if inf_fill is not None:
            inf_select = np.isinf(data)
            data[inf_select] = inf_fill
            if wgts is not None:
                wgts[inf_select] = 0.0

    else:
        for i, k in enumerate(data.keys()):
            if nan_fill is not None:
                # replace nan
                nan_select = np.isnan(data[k])
                data[k][nan_select] = nan_fill
                if wgts is not None:
                    wgts[k][nan_select] = 0.0

            if inf_fill is not None:
                # replace infs
                inf_select = np.isinf(data[k])
                data[k][inf_select] = inf_fill
                if wgts is not None:
                    wgts[k][inf_select] = 0.0


def flatten(l):
    """ flatten a nested list """
    return [item for sublist in l for item in sublist]


class Baseline(object):
    """
    Baseline object for making antenna-independent, unique baseline labels
    for baselines up to 1km in length to an absolute precison of 10 cm.
    Only __eq__ operator is overloaded.
    """

    def __init__(self, bl, tol=2.0):
        """
        bl : list containing [dx, dy, dz] float separation in meters
        tol : tolerance for baseline length comparison in meters
        """
        self.label = "{:06.1f}:{:06.1f}:{:06.1f}".format(float(bl[0]), float(bl[1]), float(bl[2]))
        self.bl = np.array(bl, dtype=np.float)
        self.tol = tol

    def __repr__(self):
        return self.label

    @property
    def unit(self):
        return self.bl / np.linalg.norm(self.bl)

    @property
    def len(self):
        return np.linalg.norm(self.bl)

    def __eq__(self, B2):
        tol = np.max([self.tol, B2.tol])
        # check same length
        if np.isclose(self.len, B2.len, atol=tol):
            # check x, y, z
            equiv = bool(reduce(operator.mul, list(map(lambda x: np.isclose(*x, atol=tol), zip(self.bl, B2.bl)))))
            dot = np.dot(self.unit, B2.unit)
            if equiv:
                return True
            # check conjugation
            elif np.isclose(np.arccos(dot), np.pi, atol=tol / self.len) or (dot < -1.0):
                return 'conjugated'
            # else return False
            else:
                return False
        else:
            return False


def match_red_baselines(model, model_antpos, data, data_antpos, tol=1.0, verbose=True):
    """
    Match unique model baseline keys to unique data baseline keys based on positional redundancy.

    Ideally, both model and data contain only unique baselines, in which case there is a
    one-to-one mapping. If model contains extra redundant baselines, these are not propagated
    to new_model. If data contains extra redundant baselines, the lowest ant1-ant2 pair is chosen
    as the baseline key to insert into model.

    Parameters:
    -----------
    model : type=DataContainer, model dictionary holding complex visibilities
            must conform to DataContainer dictionary format.

    model_antpos : type=dictionary, dictionary holding antennas positions for model dictionary
            keys are antenna integers, values are ndarrays of position vectors in meters

    data : type=DataContainer, data dictionary holding complex visibilities.
            must conform to DataContainer dictionary format.

    data_antpos : type=dictionary, dictionary holding antennas positions for data dictionary
                same format as model_antpos

    tol : type=float, baseline match tolerance in units of baseline vectors (e.g. meters)

    Output: (data)
    -------
    new_model : type=DataContainer, dictionary holding complex visibilities from model that
        had matching baselines to data
    """

    # create baseline keys for model
    model_keys = list(model.keys())
    model_bls = np.array(list(map(lambda k: Baseline(model_antpos[k[1]] - model_antpos[k[0]], tol=tol), model_keys)))

    # create baseline keys for data
    data_keys = list(data.keys())
    data_bls = np.array(list(map(lambda k: Baseline(data_antpos[k[1]] - data_antpos[k[0]], tol=tol), data_keys)))

    # iterate over data baselines
    new_model = odict()
    for i, bl in enumerate(model_bls):
        # compre bl to all model_bls
        comparison = np.array(list(map(lambda mbl: bl == mbl, data_bls)), np.str)

        # get matches
        matches = np.where((comparison == 'True') | (comparison == 'conjugated'))[0]

        # check for matches
        if len(matches) == 0:
            echo("found zero matches in data for model {}".format(model_keys[i]), verbose=verbose)
            continue
        else:
            if len(matches) > 1:
                echo("found more than 1 match in data to model {}: {}".format(model_keys[i], list(map(lambda j: data_keys[j], matches))), verbose=verbose)
            # assign to new_data
            if comparison[matches[0]] == 'True':
                new_model[data_keys[matches[0]]] = model[model_keys[i]]
            elif comparison[matches[0]] == 'conjugated':
                new_model[data_keys[matches[0]]] = np.conj(model[model_keys[i]])

    return DataContainer(new_model)


def avg_data_across_red_bls(data, antpos, wgts=None, broadcast_wgts=True, tol=1.0,
                            mirror_red_data=False, reds=None):
    """
    Given complex visibility data spanning one or more redundant
    baseline groups, average redundant visibilities and return

    Parameters:
    -----------
    data : type=DataContainer, data dictionary holding complex visibilities.
        must conform to AbsCal dictionary format.

    antpos : type=dictionary, antenna position dictionary

    wgts : type=DataContainer, data weights as float

    broadcast_wgts : type=boolean, if True, take geometric mean of input weights as output weights,
        else use mean. If True, this has the effect of broadcasting a single flag from any particular
        baseline to all baselines in a baseline group.

    tol : type=float, redundant baseline tolerance threshold

    mirror_red_data : type=boolean, if True, mirror average visibility across red bls

    reds : list of list of redundant baselines with polarization strings.
           If None, reds is produced from antpos.

    Output: (red_data, red_wgts, red_keys)
    -------
    """
    warnings.warn("Warning: This function will be deprecated in the next hera_cal release.")

    # get data keys
    keys = list(data.keys())

    # get data, wgts and ants
    pols = np.unique(list(map(lambda k: k[2], data.keys())))
    ants = np.unique(np.concatenate(keys))
    if wgts is None:
        wgts = DataContainer(odict(list(map(lambda k: (k, np.ones_like(data[k]).astype(np.float)), data.keys()))))

    # get redundant baselines if not provided
    if reds is None:
        reds = redcal.get_reds(antpos, bl_error_tol=tol, pols=pols)

    # strip reds of keys not in data
    stripped_reds = []
    for i, bl_group in enumerate(reds):
        group = []
        for k in bl_group:
            if k in data:
                group.append(k)
        if len(group) > 0:
            stripped_reds.append(group)

    # make red_data dictionary
    red_data = odict()
    red_wgts = odict()

    # iterate over reds
    for i, bl_group in enumerate(stripped_reds):
        # average redundant baseline group
        d = np.nansum(list(map(lambda k: data[k] * wgts[k], bl_group)), axis=0)
        d /= np.nansum(list(map(lambda k: wgts[k], bl_group)), axis=0)

        # get wgts
        if broadcast_wgts:
            w = np.array(reduce(operator.mul, list(map(lambda k: wgts[k], bl_group))), np.float) ** (1. / len(bl_group))
        else:
            w = np.array(reduce(operator.add, list(map(lambda k: wgts[k], bl_group))), np.float) / len(bl_group)

        # iterate over bl_group
        for j, key in enumerate(sorted(bl_group)):
            # assign to red_data and wgts
            red_data[key] = d
            red_wgts[key] = w

            # break if no mirror
            if mirror_red_data is False:
                break

    # get red_data keys
    red_keys = list(red_data.keys())

    return DataContainer(red_data), DataContainer(red_wgts), red_keys


def mirror_data_to_red_bls(data, antpos, tol=2.0, weights=False):
    """
    Given unique baseline data (like omnical model visibilities),
    copy the data over to all other baselines in the same redundant group.
    If weights==True, treat data as a wgts dictionary and multiply values
    by their redundant baseline weighting.

    Parameters:
    -----------
    data : data DataContainer in hera_cal.DataContainer form

    antpos : type=dictionary, antenna positions dictionary
                keys are antenna integers, values are ndarray baseline vectors.

    tol : type=float, redundant baseline distance tolerance in units of baseline vectors

    weights : type=bool, if True, treat data as a wgts dictionary and multiply by redundant weighting.

    Output: (red_data)
    -------
    red_data : type=DataContainer, data dictionary in AbsCal form, with unique baseline data
                distributed to redundant baseline groups.
    if weights == True:
        red_data is a real-valued wgts dictionary with redundant baseline weighting muliplied in.
    """
    # get data keys
    keys = list(data.keys())

    # get polarizations in data
    pols = data.pols()

    # get redundant baselines
    reds = redcal.get_reds(antpos, bl_error_tol=tol, pols=pols)

    # make red_data dictionary
    red_data = odict()

    # iterate over data keys
    for i, k in enumerate(keys):

        # find which bl_group this key belongs to
        match = np.array(list(map(lambda r: k in r, reds)))
        conj_match = np.array(list(map(lambda r: reverse_bl(k) in r, reds)))

        # if no match, just copy data over to red_data
        if True not in match and True not in conj_match:
            red_data[k] = copy.copy(data[k])

        else:
            # iterate over matches
            for j, (m, cm) in enumerate(zip(match, conj_match)):
                if weights:
                    # if weight dictionary, add repeated baselines
                    if m:
                        if k not in red_data:
                            red_data[k] = copy.copy(data[k])
                            red_data[k][red_data[k].astype(np.bool)] = red_data[k][red_data[k].astype(np.bool)] + len(reds[j]) - 1
                        else:
                            red_data[k][red_data[k].astype(np.bool)] = red_data[k][red_data[k].astype(np.bool)] + len(reds[j])
                    elif cm:
                        if k not in red_data:
                            red_data[k] = copy.copy(data[k])
                            red_data[k][red_data[k].astype(np.bool)] = red_data[k][red_data[k].astype(np.bool)] + len(reds[j]) - 1
                        else:
                            red_data[k][red_data[k].astype(np.bool)] = red_data[k][red_data[k].astype(np.bool)] + len(reds[j])
                else:
                    # if match, insert all bls in bl_group into red_data
                    if m:
                        for bl in reds[j]:
                            red_data[bl] = copy.copy(data[k])
                    elif cm:
                        for bl in reds[j]:
                            red_data[bl] = np.conj(data[k])

    # re-sort, square if weights to match linsolve
    if weights:
        for i, k in enumerate(red_data):
            red_data[k][red_data[k].astype(np.bool)] = red_data[k][red_data[k].astype(np.bool)]**(2.0)
    else:
        red_data = odict([(k, red_data[k]) for k in sorted(red_data)])

    return DataContainer(red_data)


def match_times(datafile, modelfiles, filetype='uvh5', atol=1e-5):
    """
    Match start and end LST of datafile to modelfiles. Each file in modelfiles needs
    to have the same integration time.

    Args:
        datafile : type=str, path to data file
        modelfiles : type=list of str, list of filepaths to model files ordered according to file start time
        filetype : str, options=['uvh5', 'miriad']

    Returns:
        matched_modelfiles : type=list, list of modelfiles that overlap w/ datafile in LST
    """
    # get lst arrays
    data_dlst, data_dtime, data_lsts, data_times = io.get_file_times(datafile, filetype=filetype)
    model_dlsts, model_dtimes, model_lsts, model_times = io.get_file_times(modelfiles, filetype=filetype)

    # shift model files relative to first file & first index if needed
    for ml in model_lsts:
        if ml[0] < model_lsts[0][0]:
            ml += 2 * np.pi

    # get model start and stop, buffering by dlst / 2
    model_starts = np.asarray([ml[0] - md / 2.0 for ml, md in zip(model_lsts, model_dlsts)])
    model_ends = np.asarray([ml[-1] + md / 2.0 for ml, md in zip(model_lsts, model_dlsts)])

    # shift data relative to model if needed
    if data_lsts[-1] < model_starts[0]:
        data_lsts += 2 * np.pi

    # select model files
    match = np.asarray(modelfiles)[(model_starts < data_lsts[-1] + atol)
                                   & (model_ends > data_lsts[0] - atol)]

    return match


def cut_bls(datacontainer, bls=None, min_bl_cut=None, max_bl_cut=None, inplace=False):
    """
    Cut visibility data based on min and max baseline length.

    Parameters
    ----------
    datacontainer : DataContainer object to perform baseline cut on

    bls : dictionary, holding baseline position vectors.
        keys are antenna-pair tuples and values are baseline vectors in meters.
        If bls is None, will look for antpos attr in datacontainer.

    min_bl_cut : float, minimum baseline separation [meters] to keep in data

    max_bl_cut : float, maximum baseline separation [meters] to keep in data

    inplace : bool, if True edit data in input object, else make a copy.

    Output
    ------
    datacontainer : DataContainer object with bl cut enacted
    """
    if not inplace:
        datacontainer = copy.deepcopy(datacontainer)
    if min_bl_cut is None:
        min_bl_cut = 0.0
    if max_bl_cut is None:
        max_bl_cut = 1e10
    if bls is None:
        # look for antpos in dc
        if not hasattr(datacontainer, 'antpos'):
            raise ValueError("If bls is not fed, datacontainer must have antpos attribute.")
        bls = odict()
        ap = datacontainer.antpos
        for bl in datacontainer.keys():
            if bl[0] not in ap or bl[1] not in ap:
                continue
            bls[bl] = ap[bl[1]] - ap[bl[0]]
    for k in list(datacontainer.keys()):
        bl_len = np.linalg.norm(bls[k])
        if k not in bls:
            continue
        if bl_len > max_bl_cut or bl_len < min_bl_cut:
            del datacontainer[k]

    assert len(datacontainer) > 0, "no baselines were kept after baseline cut..."

    return datacontainer


class AbsCal(object):
    """
    AbsCal object used to for phasing and scaling visibility data to an absolute reference model.
    A few different calibration methods exist. These include:

    1) per-antenna amplitude logarithmic calibration solves the equation:
            ln[abs(V_ij^data / V_ij^model)] = eta_i + eta_j

    2) per-antenna phase logarithmic calibration solves the equation:
           angle(V_ij^data / V_ij^model) = phi_i - phi_j

    3) delay linear calibration solves the equation:
           delay(V_ij^data / V_ij^model) = delay(g_i) - delay(g_j)
                                         = tau_i - tau_j
       where tau is the delay that can be turned
       into a complex gain via: g = exp(i * 2pi * tau * freqs).

    4) delay slope linear calibration solves the equation:
            delay(V_ij^data / V_ij^model) = dot(T_dly, B_ij)
        where T_dly is a delay slope in [ns / meter]
        and B_ij is the baseline vector between ant i and j.

    5) frequency-independent phase slope calibration
        median_over_freq(angle(V_ij^data / V_ij^model)) = dot(Phi, B_ji)
        where Phi is a phase slope in [radians / meter]
        and B_ij is the baseline vector between ant i and j.

    6) Average amplitude linear calibration solves the equation:
            log|V_ij^data / V_ij^model| = log|g_avg_i| + log|g_avg_j|

    7) Tip-Tilt phase logarithmic calibration solves the equation
            angle(V_ij^data /  V_ij^model) = psi + dot(TT_Phi, B_ij)
        where psi is an overall gain phase scalar,
        TT_Phi is the gain phase slope vector [radians / meter]
        and B_ij is the baseline vector between antenna i and j.

    Methods (1), (2) and (3) can be thought of as general bandpass solvers, whereas
    methods (4), (5), (6), and (7) are methods that would be used for data that has already
    been redundantly calibrated.

    Be warned that the linearizations of the phase solvers suffer from phase wrapping
    pathologies, meaning that a delay calibration should generally precede a
    phs_logcal or a TT_phs_logcal bandpass routine.
    """
    def __init__(self, model, data, refant=None, wgts=None, antpos=None, freqs=None,
                 min_bl_cut=None, max_bl_cut=None, bl_taper_fwhm=None, verbose=True,
                 filetype='miriad', input_cal=None):
        """
        AbsCal object used to for phasing and scaling visibility data to an absolute reference model.

        The format of model, data and wgts is in a dictionary format, with the convention that
        keys contain antennas-pairs + polarization, Ex. (1, 2, 'nn'), and values contain 2D complex
        ndarrays with [0] axis indexing time and [1] axis frequency.

        Parameters:
        -----------
        model : Visibility data of refence model, type=dictionary or DataContainer
                keys are antenna-pair + polarization tuples, Ex. (1, 2, 'nn').
                values are complex ndarray visibilities.
                these must be 2D arrays, with [0] axis indexing time
                and [1] axis indexing frequency.

                Optionally, model can be a path to a pyuvdata-supported file, a
                pyuvdata.UVData object or hera_cal.HERAData object,
                or a list of either.

        data :  Visibility data, type=dictionary or DataContainer
                keys are antenna-pair + polarization tuples, Ex. (1, 2, 'nn').
                values are complex ndarray visibilities.
                these must be 2D arrays, with [0] axis indexing time
                and [1] axis indexing frequency.

                Optionally, data can be a path to a pyuvdata-supported file, a
                pyuvdata.UVData object or hera_cal.HERAData object,
                or a list of either. In this case, antpos, freqs
                and wgts are overwritten from arrays in data. 

        refant : antenna number integer for reference antenna
            The refence antenna is used in the phase solvers, where an absolute phase is applied to all
            antennas such that the refant's phase is set to identically zero.

        wgts : weights of the data, type=dictionary or DataContainer, [default=None]
               keys are antenna pair + pol tuples (must match model), values are real floats
               matching shape of model and data

        antpos : type=dictionary, dict of antenna position vectors in ENU (topo) frame in meters.
                 origin of coordinates does not matter, but preferably are centered in the array.
                 keys are antenna integers and values are ndarray position vectors,
                 containing [East, North, Up] coordinates.
                 Can be generated from a pyuvdata.UVData instance via
                 ----
                 #!/usr/bin/env python
                 uvd = pyuvdata.UVData()
                 uvd.read_miriad(<filename>)
                 antenna_pos, ants = uvd.get_ENU_antpos()
                 antpos = dict(zip(ants, antenna_pos))
                 ----
                 This is needed only for Tip Tilt, phase slope, and delay slope calibration.

        freqs : ndarray of frequency array, type=ndarray
                1d array containing visibility frequencies in Hz.
                Needed for delay calibration.

        min_bl_cut : float, eliminate all visibilities with baseline separation lengths
            smaller than min_bl_cut. This is assumed to be in ENU coordinates with units of meters.

        max_bl_cut : float, eliminate all visibilities with baseline separation lengths
            larger than max_bl_cut. This is assumed to be in ENU coordinates with units of meters.

        bl_taper_fwhm : float, impose a gaussian taper on the data weights as a function of
            bl separation length, with a specified fwhm [meters]

        filetype : str, if data and/or model are fed as strings, this is their filetype

        input_cal : filepath to calfits, UVCal or HERACal object with gain solutions to
            apply to data on-the-fly via hera_cal.apply_cal.calibrate_in_place
        """
        # set pols to None
        pols = None

        # load model if necessary
        if isinstance(model, list) or isinstance(model, np.ndarray) or isinstance(model, str) or issubclass(model.__class__, UVData):
            (model, model_flags, model_antpos, model_ants, model_freqs, model_lsts,
             model_times, model_pols) = io.load_vis(model, pop_autos=True, return_meta=True, filetype=filetype)

        # load data if necessary
        if isinstance(data, list) or isinstance(data, np.ndarray) or isinstance(data, str) or issubclass(data.__class__, UVData):
            (data, flags, data_antpos, data_ants, data_freqs, data_lsts,
             data_times, data_pols) = io.load_vis(data, pop_autos=True, return_meta=True, filetype=filetype)
            pols = data_pols
            freqs = data_freqs
            antpos = data_antpos

        # apply calibration
        if input_cal is not None:
            if 'flags' not in locals():
                flags = None
            uvc = io.to_HERACal(input_cal)
            gains, cal_flags, quals, totquals = uvc.read()
            apply_cal.calibrate_in_place(data, gains, data_flags=flags, cal_flags=cal_flags, gain_convention=uvc.gain_convention)

        # get shared keys and pols
        self.keys = sorted(set(model.keys()) & set(data.keys()))
        assert len(self.keys) > 0, "no shared keys exist between model and data"
        if pols is None:
            pols = np.unique(list(map(lambda k: k[2], self.keys)))
        self.pols = pols
        self.Npols = len(self.pols)
        self.gain_pols = np.unique(list(map(lambda p: list(split_pol(p)), self.pols)))
        self.Ngain_pols = len(self.gain_pols)        

        # append attributes
        self.model = DataContainer(dict([(k, model[k]) for k in self.keys]))
        self.data = DataContainer(dict([(k, data[k]) for k in self.keys]))

        # setup frequencies
        self.freqs = freqs
        if self.freqs is None:
            self.Nfreqs = None
        else:
            self.Nfreqs = len(self.freqs)

        # setup weights
        if wgts is None:
            # use data flags if present
            if 'flags' in locals() and flags is not None:
                wgts = DataContainer(dict([(k, (~flags[k]).astype(np.float)) for k in self.keys]))
            else:
                wgts = DataContainer(dict([(k, np.ones_like(data[k], dtype=np.float)) for k in self.keys]))
            if 'model_flags' in locals():
                for k in self.keys:
                    wgts[k] *= (~model_flags[k]).astype(np.float)
        self.wgts = wgts

        # setup ants
        self.ants = np.unique(np.concatenate(list(map(lambda k: k[:2], self.keys))))
        self.Nants = len(self.ants)
        if refant is None:
            refant = self.keys[0][0]
            print("using {} for reference antenna".format(refant))
        else:
            assert refant in self.ants, "refant {} not found in self.ants".format(refant)
        self.refant = refant

        # setup antenna positions
        self._set_antpos(antpos)

        # setup gain solution keys
        self._gain_keys = [[(a, p) for a in self.ants] for p in self.gain_pols]

        # perform baseline cut
        if min_bl_cut is not None or max_bl_cut is not None:
            assert self.antpos is not None, "can't request a bl_cut if antpos is not fed"

            _model = cut_bls(self.model, self.bls, min_bl_cut, max_bl_cut)
            _data = cut_bls(self.data, self.bls, min_bl_cut, max_bl_cut)
            _wgts = cut_bls(self.wgts, self.bls, min_bl_cut, max_bl_cut)

            # re-init
            self.__init__(_model, _data, refant=self.refant, wgts=_wgts, antpos=self.antpos, freqs=self.freqs, verbose=verbose)

        # enact a baseline weighting taper
        if bl_taper_fwhm is not None:
            assert self.antpos is not None, "can't request a baseline taper if antpos is not fed"

            # make gaussian taper func
            def taper(ratio):
                return np.exp(-0.5 * ratio**2)

            # iterate over baselines
            for k in self.wgts.keys():
                self.wgts[k] *= taper(np.linalg.norm(self.bls[k]) / bl_taper_fwhm)

    def _set_antpos(self, antpos):
        '''Helper function for replacing self.antpos, self.bls, and self.antpos_arr without affecting tapering or baseline cuts.
        Useful for replacing true antenna positions with idealized ones derived from the redundancies.'''
        self.antpos = antpos
        self.antpos_arr = None
        self.bls = None
        if self.antpos is not None:
            # center antpos about reference antenna
            self.antpos = odict([(k, antpos[k] - antpos[self.refant]) for k in self.ants])
            self.bls = odict([(x, self.antpos[x[0]] - self.antpos[x[1]]) for x in self.keys])
            self.antpos_arr = np.array(list(map(lambda x: self.antpos[x], self.ants)))
            self.antpos_arr -= np.median(self.antpos_arr, axis=0)

    def amp_logcal(self, verbose=True):
        """
        Call abscal_funcs.amp_logcal() method. see its docstring for more details.

        Parameters:
        -----------
        verbose : type=boolean, if True print feedback to stdout

        Result:
        -------
        per-antenna amplitude and per-antenna amp gains
        can be accessed via the getter functions
            self.ant_eta
            self.ant_eta_arr
            self.ant_eta_gain
            self.ant_eta_gain_arr
        """
        # set data quantities
        model = self.model
        data = self.data
        wgts = copy.copy(self.wgts)

        # run linsolve
        fit = amp_logcal(model, data, wgts=wgts, verbose=verbose)

        # form result array
        self._ant_eta = odict(list(map(lambda k: (k, copy.copy(fit["eta_{}_{}".format(k[0], k[1])])), flatten(self._gain_keys))))
        self._ant_eta_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._ant_eta[k], pk)), self._gain_keys)), 0, -1)

    def phs_logcal(self, avg=False, verbose=True):
        """
        call abscal_funcs.phs_logcal() method. see its docstring for more details.

        Parameters:
        -----------
        avg : type=boolean, if True, average solution across time and frequency

        verbose : type=boolean, if True print feedback to stdout

        Result:
        -------
        per-antenna phase and per-antenna phase gains
        can be accessed via the methods
            self.ant_phi
            self.ant_phi_arr
            self.ant_phi_gain
            self.ant_phi_gain_arr
        """
        # assign data
        model = self.model
        data = self.data
        wgts = copy.deepcopy(self.wgts)

        # run linsolve
        fit = phs_logcal(model, data, wgts=wgts, refant=self.refant, verbose=verbose)

        # form result array
        self._ant_phi = odict(list(map(lambda k: (k, copy.copy(fit["phi_{}_{}".format(k[0], k[1])])), flatten(self._gain_keys))))
        self._ant_phi_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._ant_phi[k], pk)), self._gain_keys)), 0, -1)

        # take time and freq average
        if avg:
            self._ant_phi = odict(list(map(lambda k: (k, np.ones_like(self._ant_phi[k])
                                                      * np.angle(np.median(np.real(np.exp(1j * self._ant_phi[k])))
                                                                 + 1j * np.median(np.imag(np.exp(1j * self._ant_phi[k]))))), flatten(self._gain_keys))))
            self._ant_phi_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._ant_phi[k], pk)), self._gain_keys)), 0, -1)

    def delay_lincal(self, medfilt=True, kernel=(1, 11), verbose=True, time_avg=False, edge_cut=0):
        """
        Solve for per-antenna delay according to the equation
        by calling abscal_funcs.delay_lincal method.
        See abscal_funcs.delay_lincal for details.

        Parameters:
        -----------
        medfilt : boolean, if True median filter data before fft

        kernel : size of median filter across (time, freq) axes, type=(int, int)

        time_avg : boolean, if True, average resultant antenna delays across time

        edge_cut : int, number of channels to exclude at each band edge in FFT window

        Result:
        -------
        per-antenna delays, per-antenna delay gains, per-antenna phase + phase gains
        can be accessed via the methods
            self.ant_dly
            self.ant_dly_gain
            self.ant_dly_arr
            self.ant_dly_gain_arr
            self.ant_dly_phi
            self.ant_dly_phi_gain
            self.ant_dly_phi_arr
            self.ant_dly_phi_gain_arr
        """
        # check for freq data
        if self.freqs is None:
            raise AttributeError("cannot delay_lincal without self.freqs array")

        # assign data
        model = self.model
        data = self.data
        wgts = self.wgts

        # get freq channel width
        df = np.median(np.diff(self.freqs))

        # run delay_lincal
        fit = delay_lincal(model, data, wgts=wgts, refant=self.refant, medfilt=medfilt, df=df, 
                           f0=self.freqs[0], kernel=kernel, verbose=verbose, edge_cut=edge_cut)

        # time average
        if time_avg:
            k = flatten(self._gain_keys)[0]
            Ntimes = fit["tau_{}_{}".format(k[0], k[1])].shape[0]
            for i, k in enumerate(flatten(self._gain_keys)):
                tau_key = "tau_{}_{}".format(k[0], k[1])
                tau_avg = np.moveaxis(np.median(fit[tau_key], axis=0)[np.newaxis], 0, 0)
                fit[tau_key] = np.repeat(tau_avg, Ntimes, axis=0)
                phi_key = "phi_{}_{}".format(k[0], k[1])
                gain = np.exp(1j * fit[phi_key])
                real_avg = np.median(np.real(gain), axis=0)
                imag_avg = np.median(np.imag(gain), axis=0)
                phi_avg = np.moveaxis(np.angle(real_avg + 1j * imag_avg)[np.newaxis], 0, 0)
                fit[phi_key] = np.repeat(phi_avg, Ntimes, axis=0)

        # form result
        self._ant_dly = odict(list(map(lambda k: (k, copy.copy(fit["tau_{}_{}".format(k[0], k[1])])), flatten(self._gain_keys))))
        self._ant_dly_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._ant_dly[k], pk)), self._gain_keys)), 0, -1)

        self._ant_dly_phi = odict(list(map(lambda k: (k, copy.copy(fit["phi_{}_{}".format(k[0], k[1])])), flatten(self._gain_keys))))
        self._ant_dly_phi_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._ant_dly_phi[k], pk)), self._gain_keys)), 0, -1)

    def delay_slope_lincal(self, medfilt=True, kernel=(1, 15), verbose=True, time_avg=False,
                           four_pol=False, edge_cut=0):
        """
        Solve for an array-wide delay slope (a subset of the omnical degeneracies) by calling
        abscal_funcs.delay_slope_lincal method. See abscal_funcs.delay_slope_lincal for details.

        Parameters:
        -----------
        medfilt : boolean, if True median filter data before fft

        kernel : size of median filter across (time, freq) axes, type=(int, int)

        verbose : type=boolean, if True print feedback to stdout

        time_avg : boolean, if True, average resultant delay slope across time

        four_pol : boolean, if True, form a joint polarization solution

        edge_cut : int, number of channels to exclude at each band edge in FFT window

        Result:
        -------
        delays slopes, per-antenna delay gains, per-antenna phase + phase gains
        can be accessed via the methods
            self.dly_slope
            self.dly_slope_gain
            self.dly_slope_arr
            self.dly_slope_gain_arr
        """
        # check for freq data
        if self.freqs is None:
            raise AttributeError("cannot delay_slope_lincal without self.freqs array")

        # assign data
        model = self.model
        data = self.data
        wgts = self.wgts
        antpos = self.antpos

        # get freq channel width
        df = np.median(np.diff(self.freqs))

        # run delay_slope_lincal
        fit = delay_slope_lincal(model, data, antpos, wgts=wgts, refant=self.refant, medfilt=medfilt, df=df,
                                 kernel=kernel, verbose=verbose, four_pol=four_pol, edge_cut=edge_cut)

        # separate pols if four_pol
        if four_pol:
            for i, gp in enumerate(self.gain_pols):
                fit['T_ew_{}'.format(gp)] = fit["T_ew"]
                fit['T_ns_{}'.format(gp)] = fit["T_ns"]
                fit.pop('T_ew')
                fit.pop('T_ns')

        # time average
        if time_avg:
            k = flatten(self._gain_keys)[0]
            Ntimes = fit["T_ew_{}".format(k[1])].shape[0]
            for i, k in enumerate(flatten(self._gain_keys)):
                ew_key = "T_ew_{}".format(k[1])
                ns_key = "T_ns_{}".format(k[1])
                ew_avg = np.moveaxis(np.median(fit[ew_key], axis=0)[np.newaxis], 0, 0)
                ns_avg = np.moveaxis(np.median(fit[ns_key], axis=0)[np.newaxis], 0, 0)
                fit[ew_key] = np.repeat(ew_avg, Ntimes, axis=0)
                fit[ns_key] = np.repeat(ns_avg, Ntimes, axis=0)

        # form result
        self._dly_slope = odict(list(map(lambda k: (k, copy.copy(np.array([fit["T_ew_{}".format(k[1])], fit["T_ns_{}".format(k[1])]]))), flatten(self._gain_keys))))
        self._dly_slope_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: np.array([self._dly_slope[k][0], self._dly_slope[k][1]]), pk)), self._gain_keys)), 0, -1)

    def global_phase_slope_logcal(self, solver='linfit', tol=1.0, edge_cut=0, verbose=True):
        """
        Solve for a frequency-independent spatial phase slope (a subset of the omnical degeneracies) by calling
        abscal_funcs.global_phase_slope_logcal method. See abscal_funcs.global_phase_slope_logcal for details.

        Parameters:
        -----------
        solver : 'linfit' uses linsolve to fit phase slope across the array,
                 'dft' uses a spatial Fourier transform to find a phase slope 

        tol : type=float, baseline match tolerance in units of baseline vectors (e.g. meters)

        edge_cut : int, number of channels to exclude at each band edge in phase slope solver

        verbose : type=boolean, if True print feedback to stdout

        Result:
        -------
        per-antenna delays, per-antenna delay gains, per-antenna phase + phase gains
        can be accessed via the methods
            self.phs_slope
            self.phs_slope_gain
            self.phs_slope_arr
            self.phs_slope_gain_arr
        """

        # assign data
        model = self.model
        data = self.data
        wgts = self.wgts
        antpos = self.antpos

        # run global_phase_slope_logcal
        fit = global_phase_slope_logcal(model, data, antpos, solver=solver, wgts=wgts,
                                        refant=self.refant, verbose=verbose, tol=tol, edge_cut=edge_cut)

        # form result
        self._phs_slope = odict(list(map(lambda k: (k, copy.copy(np.array([fit["Phi_ew_{}".format(k[1])], fit["Phi_ns_{}".format(k[1])]]))), flatten(self._gain_keys))))
        self._phs_slope_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: np.array([self._phs_slope[k][0], self._phs_slope[k][1]]), pk)), self._gain_keys)), 0, -1)

    def abs_amp_logcal(self, verbose=True):
        """
        call abscal_funcs.abs_amp_logcal() method. see its docstring for more details.

        Parameters:
        -----------
        verbose : type=boolean, if True print feedback to stdout

        Result:
        -------
        Absolute amplitude scalar can be accessed via methods
            self.abs_eta
            self.abs_eta_gain
            self.abs_eta_arr
            self.abs_eta_gain_arr
        """
        # set data quantities
        model = self.model
        data = self.data
        wgts = self.wgts

        # run abs_amp_logcal
        fit = abs_amp_logcal(model, data, wgts=wgts, verbose=verbose)

        # form result
        self._abs_eta = odict(list(map(lambda k: (k, copy.copy(fit["eta_{}".format(k[1])])), flatten(self._gain_keys))))
        self._abs_eta_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._abs_eta[k], pk)), self._gain_keys)), 0, -1)

    def TT_phs_logcal(self, verbose=True, zero_psi=True, four_pol=False):
        """
        call abscal_funcs.TT_phs_logcal() method. see its docstring for more details.

        Parameters:
        -----------
        zero_psi : type=boolean, set overall gain phase (psi) to identically zero in linsolve equations.
            This is separate than the reference antenna's absolute phase being set to zero, as it can account
            for absolute phase offsets between polarizations.

        four_pol : type=boolean, even if multiple polarizations are present in data, make free
                    variables polarization un-aware: i.e. one solution across all polarizations.
                    This is the same assumption as 4-polarization calibration in omnical.

        verbose : type=boolean, if True print feedback to stdout

        Result:
        -------
        Tip-Tilt phase slope and overall phase fit can be accessed via methods
            self.abs_psi
            self.abs_psi_gain
            self.TT_Phi
            self.TT_Phi_gain
            self.abs_psi_arr
            self.abs_psi_gain_arr
            self.TT_Phi_arr
            self.TT_Phi_gain_arr
        """
        # set data quantities
        model = self.model
        data = self.data
        wgts = self.wgts
        antpos = self.antpos

        # run TT_phs_logcal
        fit = TT_phs_logcal(model, data, antpos, wgts=wgts, refant=self.refant, verbose=verbose, zero_psi=zero_psi, four_pol=four_pol)

        # manipulate if four_pol
        if four_pol:
            for i, gp in enumerate(self.gain_pols):
                fit['Phi_ew_{}'.format(gp)] = fit["Phi_ew"]
                fit['Phi_ns_{}'.format(gp)] = fit["Phi_ns"]
                fit.pop('Phi_ew')
                fit.pop('Phi_ns')

        # form result
        self._abs_psi = odict(list(map(lambda k: (k, copy.copy(fit["psi_{}".format(k[1])])), flatten(self._gain_keys))))
        self._abs_psi_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._abs_psi[k], pk)), self._gain_keys)), 0, -1)

        self._TT_Phi = odict(list(map(lambda k: (k, copy.copy(np.array([fit["Phi_ew_{}".format(k[1])], fit["Phi_ns_{}".format(k[1])]]))), flatten(self._gain_keys))))
        self._TT_Phi_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: np.array([self._TT_Phi[k][0], self._TT_Phi[k][1]]), pk)), self._gain_keys)), 0, -1)

    # amp_logcal results
    @property
    def ant_eta(self):
        """ return _ant_eta dict, containing per-antenna amplitude solution """
        if hasattr(self, '_ant_eta'):
            return copy.deepcopy(self._ant_eta)
        else:
            return None

    @property
    def ant_eta_gain(self):
        """ form complex gain from _ant_eta dict """
        if hasattr(self, '_ant_eta'):
            ant_eta = self.ant_eta
            return odict(list(map(lambda k: (k, np.exp(ant_eta[k]).astype(np.complex)), flatten(self._gain_keys))))
        else:
            return None

    @property
    def ant_eta_arr(self):
        """ return _ant_eta in ndarray format """
        if hasattr(self, '_ant_eta_arr'):
            return copy.copy(self._ant_eta_arr)
        else:
            return None

    @property
    def ant_eta_gain_arr(self):
        """ return _ant_eta_gain in ndarray format """
        if hasattr(self, '_ant_eta_arr'):
            return np.exp(self.ant_eta_arr).astype(np.complex)
        else:
            return None

    # phs_logcal results
    @property
    def ant_phi(self):
        """ return _ant_phi dict, containing per-antenna phase solution """
        if hasattr(self, '_ant_phi'):
            return copy.deepcopy(self._ant_phi)
        else:
            return None

    @property
    def ant_phi_gain(self):
        """ form complex gain from _ant_phi dict """
        if hasattr(self, '_ant_phi'):
            ant_phi = self.ant_phi
            return odict(list(map(lambda k: (k, np.exp(1j * ant_phi[k])), flatten(self._gain_keys))))
        else:
            return None

    @property
    def ant_phi_arr(self):
        """ return _ant_phi in ndarray format """
        if hasattr(self, '_ant_phi_arr'):
            return copy.copy(self._ant_phi_arr)
        else:
            return None

    @property
    def ant_phi_gain_arr(self):
        """ return _ant_phi_gain in ndarray format """
        if hasattr(self, '_ant_phi_arr'):
            return np.exp(1j * self.ant_phi_arr)
        else:
            return None

    # delay_lincal results
    @property
    def ant_dly(self):
        """ return _ant_dly dict, containing per-antenna delay solution """
        if hasattr(self, '_ant_dly'):
            return copy.deepcopy(self._ant_dly)
        else:
            return None

    @property
    def ant_dly_gain(self):
        """ form complex gain from _ant_dly dict """
        if hasattr(self, '_ant_dly'):
            ant_dly = self.ant_dly
            return odict(list(map(lambda k: (k, np.exp(2j * np.pi * self.freqs.reshape(1, -1) * ant_dly[k])), flatten(self._gain_keys))))
        else:
            return None

    @property
    def ant_dly_arr(self):
        """ return _ant_dly in ndarray format """
        if hasattr(self, '_ant_dly_arr'):
            return copy.copy(self._ant_dly_arr)
        else:
            return None

    @property
    def ant_dly_gain_arr(self):
        """ return ant_dly_gain in ndarray format """
        if hasattr(self, '_ant_dly_arr'):
            return np.exp(2j * np.pi * self.freqs.reshape(-1, 1) * self.ant_dly_arr)
        else:
            return None

    @property
    def ant_dly_phi(self):
        """ return _ant_dly_phi dict, containing a single phase solution per antenna """
        if hasattr(self, '_ant_dly_phi'):
            return copy.deepcopy(self._ant_dly_phi)
        else:
            return None

    @property
    def ant_dly_phi_gain(self):
        """ form complex gain from _ant_dly_phi dict """
        if hasattr(self, '_ant_dly_phi'):
            ant_dly_phi = self.ant_dly_phi
            return odict(list(map(lambda k: (k, np.exp(1j * np.repeat(ant_dly_phi[k], self.Nfreqs, 1))), flatten(self._gain_keys))))
        else:
            return None

    @property
    def ant_dly_phi_arr(self):
        """ return _ant_dly_phi in ndarray format """
        if hasattr(self, '_ant_dly_phi_arr'):
            return copy.copy(self._ant_dly_phi_arr)
        else:
            return None

    @property
    def ant_dly_phi_gain_arr(self):
        """ return _ant_dly_phi_gain in ndarray format """
        if hasattr(self, '_ant_dly_phi_arr'):
            return np.exp(1j * np.repeat(self.ant_dly_phi_arr, self.Nfreqs, 2))
        else:
            return None

    # delay_slope_lincal results
    @property
    def dly_slope(self):
        """ return _dly_slope dict, containing the delay slope across the array """
        if hasattr(self, '_dly_slope'):
            return copy.deepcopy(self._dly_slope)
        else:
            return None

    @property
    def dly_slope_gain(self):
        """ form a per-antenna complex gain from _dly_slope dict and the antpos dictionary attached to the class"""
        if hasattr(self, '_dly_slope'):
            # get dly_slope dictionary
            dly_slope = self.dly_slope
            # turn delay slope into per-antenna complex gains, while iterating over self._gain_keys
            # einsum sums over antenna position
            return odict(list(map(lambda k: (k, np.exp(2j * np.pi * self.freqs.reshape(1, -1) * np.einsum("i...,i->...", dly_slope[k], self.antpos[k[0]][:2]))),
                                  flatten(self._gain_keys))))
        else:
            return None

    def custom_dly_slope_gain(self, gain_keys, antpos):
        """
        return dly_slope_gain with custom gain keys and antenna positions

        gain_keys : type=list, list of unique (ant, pol). Ex. [(0, 'Jee'), (1, 'Jee'), (0, 'Jnn'), (1, 'Jnn')]
        antpos : type=dictionary, contains antenna position vectors. keys are ant integer, values are ant position vectors
        """
        if hasattr(self, '_dly_slope'):
            # form dict of delay slopes for each polarization in self._gain_keys
            # b/c they are identical for all antennas of the same polarization
            dly_slope_dict = {ants[0][1]: self.dly_slope[ants[0]] for ants in self._gain_keys}

            # turn delay slope into per-antenna complex gains, while iterating over input gain_keys
            dly_slope_gain = odict()
            for gk in gain_keys:
                # einsum sums over antenna position
                dly_slope_gain[gk] = np.exp(2j * np.pi * self.freqs.reshape(1, -1) * np.einsum("i...,i->...", dly_slope_dict[gk[1]], antpos[gk[0]][:2]))
            return dly_slope_gain
        else:
            return None

    @property
    def dly_slope_arr(self):
        """ return _dly_slope_arr array """
        if hasattr(self, '_dly_slope_arr'):
            return copy.copy(self._dly_slope_arr)
        else:
            return None

    @property
    def dly_slope_gain_arr(self):
        """ form complex gain from _dly_slope_arr array """
        if hasattr(self, '_dly_slope_arr'):
            # einsum sums over antenna position
            return np.exp(2j * np.pi * self.freqs.reshape(-1, 1) * np.einsum("hi...,hi->h...", self._dly_slope_arr, self.antpos_arr[:, :2]))
        else:
            return None

    @property
    def dly_slope_ant_dly_arr(self):
        """ form antenna delays from _dly_slope_arr array """
        if hasattr(self, '_dly_slope_arr'):
            # einsum sums over antenna position
            return np.einsum("hi...,hi->h...", self._dly_slope_arr, self.antpos_arr[:, :2])
        else:
            return None

    # global_phase_slope_logcal results
    @property
    def phs_slope(self):
        """ return _phs_slope dict, containing the frequency-indpendent phase slope across the array """
        if hasattr(self, '_phs_slope'):
            return copy.deepcopy(self._phs_slope)
        else:
            return None

    @property
    def phs_slope_gain(self):
        """ form a per-antenna complex gain from _phs_slope dict and the antpos dictionary attached to the class"""
        if hasattr(self, '_phs_slope'):
            # get phs_slope dictionary
            phs_slope = self.phs_slope
            # turn phs slope into per-antenna complex gains, while iterating over self._gain_keys
            # einsum sums over antenna position
            return odict(list(map(lambda k: (k, np.exp(1.0j * np.ones_like(self.freqs).reshape(1, -1) * np.einsum("i...,i->...", phs_slope[k], self.antpos[k[0]][:2]))),
                                  flatten(self._gain_keys))))
        else:
            return None

    def custom_phs_slope_gain(self, gain_keys, antpos):
        """
        return phs_slope_gain with custom gain keys and antenna positions

        gain_keys : type=list, list of unique (ant, pol). Ex. [(0, 'Jee'), (1, 'Jee'), (0, 'Jnn'), (1, 'Jnn')]
        antpos : type=dictionary, contains antenna position vectors. keys are ant integer, values are ant position vectors
        """
        if hasattr(self, '_phs_slope'):
            # form dict of phs slopes for each polarization in self._gain_keys
            # b/c they are identical for all antennas of the same polarization
            phs_slope_dict = {ants[0][1]: self.phs_slope[ants[0]] for ants in self._gain_keys}

            # turn phs slope into per-antenna complex gains, while iterating over input gain_keys
            phs_slope_gain = odict()
            for gk in gain_keys:
                # einsum sums over antenna position
                phs_slope_gain[gk] = np.exp(1.0j * np.ones_like(self.freqs).reshape(1, -1) * np.einsum("i...,i->...", phs_slope_dict[gk[1]], antpos[gk[0]][:2]))
            return phs_slope_gain

        else:
            return None

    @property
    def phs_slope_arr(self):
        """ return _phs_slope_arr array """
        if hasattr(self, '_phs_slope_arr'):
            return copy.copy(self._phs_slope_arr)
        else:
            return None

    @property
    def phs_slope_gain_arr(self):
        """ form complex gain from _phs_slope_arr array """
        if hasattr(self, '_phs_slope_arr'):
            # einsum sums over antenna position
            return np.exp(1.0j * np.ones_like(self.freqs).reshape(-1, 1) * np.einsum("hi...,hi->h...", self._phs_slope_arr, self.antpos_arr[:, :2]))
        else:
            return None

    @property
    def phs_slope_ant_phs_arr(self):
        """ form antenna delays from _phs_slope_arr array """
        if hasattr(self, '_phs_slope_arr'):
            # einsum sums over antenna position
            return np.einsum("hi...,hi->h...", self._phs_slope_arr, self.antpos_arr[:, :2])
        else:
            return None

    # abs_amp_logcal results
    @property
    def abs_eta(self):
        """return _abs_eta dict"""
        if hasattr(self, '_abs_eta'):
            return copy.deepcopy(self._abs_eta)
        else:
            return None

    @property
    def abs_eta_gain(self):
        """form complex gain from _abs_eta dict"""
        if hasattr(self, '_abs_eta'):
            abs_eta = self.abs_eta
            return odict(list(map(lambda k: (k, np.exp(abs_eta[k]).astype(np.complex)), flatten(self._gain_keys))))
        else:
            return None

    def custom_abs_eta_gain(self, gain_keys):
        """
        return abs_eta_gain with custom gain keys

        gain_keys : type=list, list of unique (ant, pol). Ex. [(0, 'Jee'), (1, 'Jee'), (0, 'Jnn'), (1, 'Jnn')]
        """
        if hasattr(self, '_abs_eta'):
            # form dict of abs eta for each polarization in self._gain_keys
            # b/c they are identical for all antennas of the same polarization
            abs_eta_dict = {ants[0][1]: self.abs_eta[ants[0]] for ants in self._gain_keys}

            # turn abs eta into per-antenna complex gains, while iterating over input gain_keys
            abs_eta_gain = odict()
            for gk in gain_keys:
                abs_eta_gain[gk] = np.exp(abs_eta_dict[gk[1]]).astype(np.complex)
            return abs_eta_gain

        else:
            return None

    @property
    def abs_eta_arr(self):
        """return _abs_eta_arr array"""
        if hasattr(self, '_abs_eta_arr'):
            return copy.copy(self._abs_eta_arr)
        else:
            return None

    @property
    def abs_eta_gain_arr(self):
        """form complex gain from _abs_eta_arr array"""
        if hasattr(self, '_abs_eta_arr'):
            return np.exp(self._abs_eta_arr).astype(np.complex)
        else:
            return None

    # TT_phs_logcal results
    @property
    def abs_psi(self):
        """return _abs_psi dict"""
        if hasattr(self, '_abs_psi'):
            return copy.deepcopy(self._abs_psi)
        else:
            return None

    @property
    def abs_psi_gain(self):
        """ form complex gain from _abs_psi array """
        if hasattr(self, '_abs_psi'):
            abs_psi = self.abs_psi
            return odict(list(map(lambda k: (k, np.exp(1j * abs_psi[k])), flatten(self._gain_keys))))
        else:
            return None

    def custom_abs_psi_gain(self, gain_keys):
        """
        return abs_psi_gain with custom gain keys

        gain_keys : type=list, list of unique (ant, pol). Ex. [(0, 'Jee'), (1, 'Jee'), (0, 'Jnn'), (1, 'Jnn')]
        """
        if hasattr(self, '_abs_psi'):
            # form dict of abs psi for each polarization in self._gain_keys
            # b/c they are identical for all antennas of the same polarization
            abs_psi_dict = {ants[0][1]: self.abs_psi[ants[0]] for ants in self._gain_keys}

            # turn abs psi into per-antenna complex gains, while iterating over input gain_keys
            abs_psi_gain = odict()
            for gk in gain_keys:
                abs_psi_gain[gk] = np.exp(1j * abs_psi_dict[gk[1]])
            return abs_psi_gain
        else:
            return None

    @property
    def abs_psi_arr(self):
        """return _abs_psi_arr array"""
        if hasattr(self, '_abs_psi_arr'):
            return copy.copy(self._abs_psi_arr)
        else:
            return None

    @property
    def abs_psi_gain_arr(self):
        """ form complex gain from _abs_psi_arr array """
        if hasattr(self, '_abs_psi_arr'):
            return np.exp(1j * self._abs_psi_arr)
        else:
            return None

    @property
    def TT_Phi(self):
        """return _TT_Phi array"""
        if hasattr(self, '_TT_Phi'):
            return copy.deepcopy(self._TT_Phi)
        else:
            return None

    @property
    def TT_Phi_gain(self):
        """ form complex gain from _TT_Phi array """
        if hasattr(self, '_TT_Phi'):
            TT_Phi = self.TT_Phi
            # einsum sums over antenna position
            return odict(list(map(lambda k: (k, np.exp(1j * np.einsum("i...,i->...", TT_Phi[k], self.antpos[k[0]][:2]))), flatten(self._gain_keys))))
        else:
            return None

    def custom_TT_Phi_gain(self, gain_keys, antpos):
        """
        return TT_Phi_gain with custom gain keys and antenna positions

        gain_keys : type=list, list of unique (ant, pol). Ex. [(0, 'Jee'), (1, 'Jee'), (0, 'Jnn'), (1, 'Jnn')]
        antpos : type=dictionary, contains antenna position vectors. keys are ant integer, values are ant positions
        """
        if hasattr(self, '_TT_Phi'):
            # form dict of TT_Phi for each polarization in self._gain_keys
            # b/c they are identical for all antennas of the same polarization
            TT_Phi_dict = {ants[0][1]: self.TT_Phi[ants[0]] for ants in self._gain_keys}

            # turn TT_Phi into per-antenna complex gains, while iterating over input gain_keys
            TT_Phi_gain = odict()
            for gk in gain_keys:
                # einsum sums over antenna position
                TT_Phi_gain[gk] = np.exp(1j * np.einsum("i...,i->...", TT_Phi_dict[gk[1]], antpos[gk[0]][:2]))
            return TT_Phi_gain
        else:
            return None

    @property
    def TT_Phi_arr(self):
        """return _TT_Phi_arr array"""
        if hasattr(self, '_TT_Phi_arr'):
            return copy.copy(self._TT_Phi_arr)
        else:
            return None

    @property
    def TT_Phi_gain_arr(self):
        """ form complex gain from _TT_Phi_arr array """
        if hasattr(self, '_TT_Phi_arr'):
            # einsum sums over antenna position
            return np.exp(1j * np.einsum("hi...,hi->h...", self._TT_Phi_arr, self.antpos_arr[:, :2]))
        else:
            return None


def get_all_times_and_lsts(hd, solar_horizon=90.0, unwrap=True):
    '''Extract all times and lsts from a HERAData object

    Arguments:
        hd: HERAData object intialized with one ore more uvh5 file's metadata
        solar_horizon: Solar altitude threshold [degrees]. Times are not returned when the Sun is above this altitude.
        unwrap: increase all LSTs smaller than the first one by 2pi to avoid phase wrapping

    Returns:
        all_times: list of times in JD in the file or files
        all_lsts: LSTs (in radians) corresponding to all_times
    '''
    all_times = hd.times
    all_lsts = hd.lsts
    if len(hd.filepaths) > 1:  # in this case, it's a dictionary
        all_times = np.array([time for f in hd.filepaths for time in all_times[f]])
        all_lsts = np.array([lst for f in hd.filepaths for lst in all_lsts[f]])[np.argsort(all_times)]
    if unwrap:  # avoid phase wraps 
        all_lsts[all_lsts < all_lsts[0]] += 2 * np.pi
        
    # remove times when sun was too high
    if solar_horizon < 90.0:
        lat, lon, alt = hd.telescope_location_lat_lon_alt_degrees
        solar_alts = utils.get_sun_alt(all_times, latitude=lat, longitude=lon)
        solar_flagged = solar_alts > solar_horizon
        return all_times[~solar_flagged], all_lsts[~solar_flagged]
    else:  # skip this step for speed
        return all_times, all_lsts


def get_d2m_time_map(data_times, data_lsts, model_times, model_lsts, unwrap=True):
    '''Generate a dictionary that maps data times to model times via shared LSTs.

    Arguments:
        data_times: list of times in the data (in JD)
        data_lsts: list of corresponding LSTs (in radians)
        model_times: list of times in the mdoel (in JD)
        model_lsts: list of corresponing LSTs (in radians)
        unwrap: increase all LSTs smaller than the first one by 2pi to avoid phase wrapping

    Returns:
        d2m_time_map: dictionary uniqely mapping times in the data to times in the model 
            that are closest in LST. Each model time maps to at most one data time and 
            each model time maps to at most one data time. Data times without corresponding
            model times map to None.
    '''
    if unwrap:  # avoid phase wraps
        data_lsts[data_lsts < data_lsts[0]] += 2 * np.pi
        model_lsts[model_lsts < model_lsts[0]] += 2 * np.pi

    # first produce a map of indices using the LSTs
    m2d_ind_map = {}  
    for dind, dlst in enumerate(data_lsts):
        nearest_mind = np.argmin(np.abs(model_lsts - dlst))
        if nearest_mind in m2d_ind_map:
            if np.abs(model_lsts[nearest_mind] < data_lsts[m2d_ind_map[nearest_mind]]):
                m2d_ind_map[nearest_mind] = dind
        else:
            m2d_ind_map[nearest_mind] = dind

    # now use those indicies to produce a map of times
    d2m_time_map = {time: None for time in data_times}
    for mind, dind in m2d_ind_map.items():
        d2m_time_map[data_times[dind]] = model_times[mind]
    return d2m_time_map


def match_baselines(data_bls, model_bls, data_antpos, model_antpos=None, pols=[], data_is_redsol=False, 
                    model_is_redundant=False, tol=1.0, min_bl_cut=None, max_bl_cut=None, verbose=False):
    '''Figure out which baselines to use in the data and the model for abscal and their correspondence.

    Arguments:
        data_bls: list of baselines in data file in the form (0, 1, 'ee')
        model_bls: list of baselines in model files in the form (0, 1, 'ee')
        data_antpos: dictionary mapping antenna number to ENU position in meters for antennas in the data
        model_antpos: same as data_antpos, but for the model. If None, assumed to match data_antpos
        pols: list of polarizations to use. If empty, will use all polarizations in the data or model.
        data_is_redsol: if True, the data file only contains one visibility per unique baseline
        model_is_redundant: if True, the model file only contains one visibility per unique baseline
        tol: float distance for baseline match tolerance in units of baseline vectors (e.g. meters)
        min_bl_cut : float, eliminate all visibilities with baseline separation lengths
            smaller than min_bl_cut. This is assumed to be in ENU coordinates with units of meters.
        max_bl_cut : float, eliminate all visibilities with baseline separation lengths
            larger than max_bl_cut. This is assumed to be in ENU coordinates with units of meters.

    Returns:
        data_bl_to_load: list of baseline tuples in the form (0, 1, 'ee') to load from the data file
        model_bl_to_load: list of baseline tuples in the form (0, 1, 'ee') to load from the model file(s)
        data_to_model_bl_map: dictionary mapping data baselines to the corresponding model baseline
    '''
    if data_is_redsol and not model_is_redundant:
        raise NotImplementedError('If the data is just unique baselines, the model must also be just unique baselines.')
    if model_antpos is None:
        model_antpos = copy.deepcopy(data_antpos)
    
    # Perform cut on baseline length and polarization
    if len(pols) == 0:
        pols = list(set([bl[2] for bl_list in [data_bls, model_bls] for bl in bl_list]))
    
    def _cut_bl_and_pol(bls, antpos):
        bls_to_load = []
        for bl in bls:
            if bl[2] in pols:
                ant1, ant2 = split_bl(bl)
                bl_length = np.linalg.norm(antpos[ant2[0]] - antpos[ant1[0]])
                if (min_bl_cut is None) or (bl_length >= min_bl_cut):
                    if (max_bl_cut is None) or (bl_length <= max_bl_cut):
                        bls_to_load.append(bl)
        return bls_to_load
    data_bl_to_load = _cut_bl_and_pol(data_bls, data_antpos)
    model_bl_to_load = _cut_bl_and_pol(model_bls, model_antpos)

    # If we're working with full data sets, only pick out matching keys (or ones that work reversably)
    if not data_is_redsol and not model_is_redundant:
        data_bl_to_load = [bl for bl in data_bl_to_load if (bl in model_bl_to_load) or (reverse_bl(bl) in model_bl_to_load)]
        model_bl_to_load = [bl for bl in model_bl_to_load if (bl in data_bl_to_load) or (reverse_bl(bl) in data_bl_to_load)]
        data_to_model_bl_map = {bl: bl for bl in data_bl_to_load if bl in model_bl_to_load}
        data_to_model_bl_map.update({bl: reverse_bl(bl) for bl in data_bl_to_load if reverse_bl(bl) in model_bl_to_load})

    # Either the model is just unique baselines, or both the data and the model are just unique baselines
    else:
        # build reds using both sets of antpos to find matching baselines
        ant_offset = np.max(list(data_antpos.keys())) + 1  # increase all antenna indices by this amount
        joint_antpos = {**data_antpos, **{ant + ant_offset: pos for ant, pos in model_antpos.items()}}
        joint_reds = redcal.get_reds(joint_antpos, pols=pols, bl_error_tol=tol)

        # filter out baselines not in data or model or between data and model
        joint_reds = [[bl for bl in red if not ((bl[0] < ant_offset) ^ (bl[1] < ant_offset))] for red in joint_reds]
        joint_reds = [[bl for bl in red if (bl in data_bl_to_load) or (reverse_bl(bl) in data_bl_to_load)
                       or ((bl[0] - ant_offset, bl[1] - ant_offset, bl[2]) in model_bl_to_load)
                       or reverse_bl((bl[0] - ant_offset, bl[1] - ant_offset, bl[2])) in model_bl_to_load] for red in joint_reds]
        joint_reds = [red for red in joint_reds if len(red) > 0]

        # map baselines in data to unique baselines in model
        data_to_model_bl_map = {}
        for red in joint_reds:
            data_bl_candidates = [bl for bl in red if bl[0] < ant_offset]
            model_bl_candidates = [(bl[0] - ant_offset, bl[1] - ant_offset, bl[2]) for bl in red if bl[0] >= ant_offset]
            assert len(model_bl_candidates) <= 1, ('model_is_redundant is True, but the following model baselines are '
                                                  'redundant and in the model file: {}'.format(model_bl_candidates))
            if len(model_bl_candidates) == 1:
                for bl in red:
                    if bl[0] < ant_offset:
                        data_to_model_bl_map[bl] = model_bl_candidates[0]
            assert ((len(data_bl_candidates) <= 1)
                    or (not data_is_redsol)), ('data_is_redsol is True, but the following data baselines are redundant in the ',
                                               'data file: {}'.format(data_bl_candidates))
        # only load baselines in map
        data_bl_to_load = [bl for bl in data_bl_to_load if bl in data_to_model_bl_map.keys()
                           or reverse_bl(bl) in data_to_model_bl_map.keys()]
        model_bl_to_load = [bl for bl in model_bl_to_load if (bl in data_to_model_bl_map.values()) 
                            or (reverse_bl(bl) in data_to_model_bl_map.values())]

    echo("Selected {} data baselines and {} model baselines to load.".format(len(data_bl_to_load), len(model_bl_to_load)), verbose=verbose)
    return data_bl_to_load, model_bl_to_load, data_to_model_bl_map


def build_data_wgts(data_flags, data_nsamples, model_flags, autocorrs, times_by_bl=None, df=None,
                    data_is_redsol=False, gain_flags=None, tol=1.0, antpos=None):
    '''Build linear weights for data in abscal (or calculating chisq) defined as
    wgts = (noise variance * nsamples)^-1 * (0 if data or model is flagged).
    
    Arguments:
        data_flags: DataContainer containing flags on data to be abscaled
        data_nsamples: DataContainer containing the number of samples in each data point
        model_flags: DataContainer with model flags. Assumed to have all the same keys as the data_flags.
        autocorrs: DataContainer with autocorrelation visibilities
        times_by_bl: dictionary mapping antenna pairs like (0,1) to float Julian Date. Optional if
            inferable from data_flags and all times have length > 1. 
        df: If None, inferred from data_flags.freqs
        data_is_redsol: If True, data_file only contains unique visibilities for each baseline group.
            In this case, gain_flags and tol are required and antpos is required if not derivable 
            from data_flags. In this case, the noise variance is inferred from autocorrelations from
            all baselines in the represented unique baseline group.
        gain_flags: Used to exclude ants from the noise variance calculation from the autocorrelations
            Ignored if data_is_redsol is False.
        tol: float distance for baseline match tolerance in units of baseline vectors (e.g. meters).
            Ignored if data_is_redsol is False.
        antpos: dictionary mapping antenna number to ENU position in meters for antennas in the data.
            Ignored if data_is_redsol is False. If left as None, can be inferred from data_flags.antpos.
    Returns:
        wgts: Datacontainer mapping data_flags baseline to weights
    '''
    # infer times and df if necessary
    if times_by_bl is None:
        times_by_bl = data_flags.times_by_bl
    if df is None:
        df = np.median(np.ediff1d(data_flags.freqs))
    
    # if data_is_redsol, get reds, using data_flags.antpos if antpos is unspecified
    if data_is_redsol:
        if antpos is None:
            antpos = data_flags.antpos
        reds = redcal.get_reds(antpos, bl_error_tol=tol, pols=data_flags.pols())
        ex_ants = [ant for ant, flags in gain_flags.items() if np.all(flags)]
        reds = redcal.filter_reds(reds, ex_ants=ex_ants)

    # build weights dict using (noise variance * nsamples)^-1 * (0 if data or model is flagged)
    wgts = {}
    for bl in data_flags:
        dt = (np.median(np.ediff1d(times_by_bl[bl[:2]])) * 86400.)
        wgts[bl] = (data_nsamples[bl] * (~data_flags[bl]) * (~model_flags[bl])).astype(np.float)
        if not np.all(wgts[bl] == 0.0):
            # use autocorrelations to produce weights
            if not data_is_redsol:
                noise_var = predict_noise_variance_from_autos(bl, autocorrs, dt=dt, df=df)
            # use autocorrelations from all unflagged antennas in unique baseline to produce weights
            else:
                try:  # get redundant group that includes this baseline
                    red_here = [red for red in reds if bl in red][0]
                except IndexError:  # this baseline has no unflagged redundancies
                    wgts[bl] *= 0.0
                else:
                    noise_vars = [predict_noise_variance_from_autos(bl, autocorrs, dt=dt, df=df) for bl in red_here]
                    # estimate noise variance per baseline, assuming inverse variance weighting
                    noise_var = np.sum(np.array(noise_vars)**-1, axis=0)**-1 * len(noise_vars)
            wgts[bl] *= noise_var**-1

        # wgts[bl] = (noise_var * data_nsamples[bl])**-1 * (~data_flags[bl]) * (~model_flags[bl])
        wgts[bl][~np.isfinite(wgts[bl])] = 0.0

    return DataContainer(wgts)


def post_redcal_abscal(model, data, data_wgts, rc_flags, edge_cut=0, tol=1.0, kernel=(1, 15), 
                       gain_convention='divide', phs_max_iter=100, phs_conv_crit=1e-6, verbose=True):
    '''Performs Abscal for data that has already been redundantly calibrated.

    Arguments:
        model: DataContainer containing externally calibrated visibilities, LST-matched to the data.
            The model keys must match the data keys.
        data: DataContainer containing redundantly but not absolutely calibrated visibilities. This gets modified.
        data_wgts: DataContainer containing same keys as data, determines their relative weight in the abscal
            linear equation solvers.
        rc_flags: dictionary mapping keys like (1, 'Jnn') to flag waterfalls from redundant calibration. 
        edge_cut : integer number of channels to exclude at each band edge in delay and global phase solvers
        tol: float distance for baseline match tolerance in units of baseline vectors (e.g. meters)
        kernel: tuple of integers, size of medfilt kernel used in the first step of delay slope calibration.
        gain_convention: either 'divide' if raw data is calibrated by dividing it by the gains
            otherwise, 'multiply'.
        phs_max_iter: maximum number of iterations of phase_slope_cal or TT_phs_cal allowed
        phs_conv_crit: convergence criterion for updates to iterative phase calibration that compares
            the updates to all 1.0s.

    Returns:
        abscal_delta_gains: gain dictionary mapping keys like (1, 'Jnn') to waterfalls containing 
            the updates to the gains between redcal and abscal. Uses keys from rc_flags
    '''

    # setup: initialize ants, get idealized antenna positions
    ants = list(rc_flags.keys())
    idealized_antpos = redcal.reds_to_antpos(redcal.get_reds(data.antpos, bl_error_tol=tol), tol=IDEALIZED_BL_TOL)
    
    # If the array is not redundant (i.e. extra degeneracies), lop off extra dimensions and warn user
    if np.max([len(pos) for pos in idealized_antpos.values()]) > 2:  
        suspected_off_grid = [ant for ant, pos in idealized_antpos.items() if np.any(np.abs(pos[2:]) > IDEALIZED_BL_TOL)]
        not_flagged = [ant for ant in suspected_off_grid if not np.all([np.all(f) for a, f in rc_flags.items() if ant in a])]
        warnings.warn(('WARNING: The following antennas appear not to be redundant with the main array:\n         {}\n'
                       '         Of them, {} is not flagged.\n').format(suspected_off_grid, not_flagged))
        idealized_antpos = {ant: pos[:2] for ant, pos in idealized_antpos.items()}

    # Abscal Step 1: Per-Channel Absolute Amplitude Calibration
    gains_here = abs_amp_logcal(model, data, wgts=data_wgts, verbose=verbose, return_gains=True, gain_ants=ants)
    abscal_delta_gains = {ant: gains_here[ant] for ant in ants}
    apply_cal.calibrate_in_place(data, gains_here, gain_convention=gain_convention)

    # Abscal Step 2: Global Delay Slope Calibration
    df = np.median(np.diff(data.freqs))
    for time_avg in [True, False]:
        gains_here = delay_slope_lincal(model, data, idealized_antpos, wgts=data_wgts, df=df, medfilt=True, kernel=kernel,
                                        verbose=verbose, edge_cut=edge_cut, return_gains=True, gain_ants=ants)
        if time_avg:
            gains_here = {ant: np.ones_like(gain) * np.median(gain, axis=0, keepdims=True) for ant, gain in gains_here.items()}
        abscal_delta_gains = {ant: abscal_delta_gains[ant] * gains_here[ant] for ant in ants}
        apply_cal.calibrate_in_place(data, gains_here, gain_convention=gain_convention)

    # Abscal Step 3: Global Phase Slope Calibration (first using dft, then using linfit)
    gains_here = global_phase_slope_logcal(model, data, idealized_antpos, solver='dft', wgts=data_wgts, verbose=verbose, 
                                           tol=IDEALIZED_BL_TOL, edge_cut=edge_cut, return_gains=True, gain_ants=ants)
    abscal_delta_gains = {ant: abscal_delta_gains[ant] * gains_here[ant] for ant in ants}
    apply_cal.calibrate_in_place(data, gains_here, gain_convention=gain_convention)
    for i in range(phs_max_iter):
        gains_here = global_phase_slope_logcal(model, data, idealized_antpos, solver='linfit', wgts=data_wgts, verbose=verbose,
                                               tol=IDEALIZED_BL_TOL, edge_cut=edge_cut, return_gains=True, gain_ants=ants)
        abscal_delta_gains = {ant: abscal_delta_gains[ant] * gains_here[ant] for ant in ants}
        apply_cal.calibrate_in_place(data, gains_here, gain_convention=gain_convention)
        crit = np.median(np.linalg.norm([gains_here[k] - 1.0 for k in gains_here.keys()], axis=(0, 1)))
        echo("global_phase_slope_logcal convergence criterion: " + str(crit), verbose=verbose)
        if crit < phs_conv_crit:
            break

    # Abscal Step 4: Per-Channel Tip-Tilt Phase Calibration
    for i in range(phs_max_iter):
        gains_here = TT_phs_logcal(model, data, idealized_antpos, wgts=data_wgts, verbose=verbose, return_gains=True, gain_ants=ants)
        abscal_delta_gains = {ant: abscal_delta_gains[ant] * gains_here[ant] for ant in ants}
        apply_cal.calibrate_in_place(data, gains_here, gain_convention=gain_convention)
        crit = np.median(np.linalg.norm([gains_here[k] - 1.0 for k in gains_here.keys()], axis=(0, 1)))
        echo("TT_phs_logcal convergence criterion: " + str(crit), verbose=verbose)
        if crit < phs_conv_crit:
            break

    return abscal_delta_gains


def post_redcal_abscal_run(data_file, redcal_file, model_files, raw_auto_file=None, data_is_redsol=False, model_is_redundant=False, output_file=None,
                           nInt_to_load=None, data_solar_horizon=90, model_solar_horizon=90, min_bl_cut=1.0, max_bl_cut=None, edge_cut=0,
                           tol=1.0, phs_max_iter=100, phs_conv_crit=1e-6, refant=None, clobber=True, add_to_history='', verbose=True):
    '''Perform abscal on entire data files, picking relevant model_files from a list and doing partial data loading.
    Does not work on data (or models) with baseline-dependant averaging.
    
    Arguments:
        data_file: string path to raw uvh5 visibility file or omnical_visibility solution 
            (in the later case, one must also set data_is_redsol to True).
        redcal_file: string path to redcal calfits file. This forms the basis of the resultant abscal calfits file.
            If data_is_redsol is False, this will also be used to calibrate the data_file and raw_auto_file
        model_files: list of string paths to externally calibrated data or a reference simulation. 
            Strings must be sortable to produce a chronological list in LST (wrapping over 2*pi is OK).
        raw_auto_file: path to data file that contains raw autocorrelations for all antennas in redcal_file. 
            These are used for weighting and calculating chi^2. If data_is_redsol, this must be provided. 
            If this is None and data_file will be used.
        data_is_redsol: If True, data_file only contains unique visibilities for each baseline group. This means it has been 
            redundantly calibrated by the gains in redcal_file already. If this is True, model_is_redundant must also be True
            and raw_auto_file must be provided. If both this and model_is_redundant are False, then only exact baseline
            matches are used in absolute calibration.
        model_is_redundant: If True, then model_files only containe unique visibilities. In this case, data and model
            antenna numbering do not need to agree, as redundant baselines will be found automatically.
        output_file: string path to output abscal calfits file. If None, will be redcal_file.replace('.omni.', '.abs.')
        nInt_to_load: number of integrations to load and calibrate simultaneously. Default None loads all integrations.
        data_solar_horizon: Solar altitude threshold [degrees]. When the sun is too high in the data, flag the integration.
        model_solar_horizon: Solar altitude threshold [degrees]. When the sun is too high in the model, flag the integration.
        min_bl_cut: minimum baseline separation [meters] to keep in data when calibrating. None or 0 means no mininum,
            which will include autocorrelations in the absolute calibration. Usually this is not desired, so the default is 1.0.
        max_bl_cut: maximum baseline separation [meters] to keep in data when calibrating. None (default) means no maximum.
        edge_cut: integer number of channels to exclude at each band edge in delay and global phase solvers
        tol: baseline match tolerance in units of baseline vectors (e.g. meters)
        phs_max_iter: integer maximum number of iterations of phase_slope_cal or TT_phs_cal allowed
        phs_conv_crit: convergence criterion for updates to iterative phase calibration that compares them to all 1.0s.
        refant: tuple of the form (0, 'Jnn') indicating the antenna defined to have 0 phase. If None, refant will be automatically chosen.
        clobber: if True, overwrites existing abscal calfits file at the output path
        add_to_history: string to add to history of output abscal file

    Returns:
        hc: HERACal object which was written to disk. Matches the input redcal_file with an updated history.
            This HERACal object has been updated with the following properties accessible on hc.build_calcontainers():
                * gains: abscal gains for times that could be calibrated, redcal gains otherwise (but flagged)
                * flags: redcal flags, with additional flagging if the data is flagged (see flag_utils.synthesize_ant_flags) or if 
                    if the model is completely flagged for a given freq/channel when reduced to a single flagging waterfall
                * quals: abscal chi^2 per antenna based on calibrated data minus model (Normalized by noise/nObs, but not with proper DoF)
                * total_qual: abscal chi^2 based on calibrated data minus model (Normalized by noise/nObs, but not with proper DoF)
    '''
    # Raise error if output calfile already exists and clobber is False
    if output_file is None:
        output_file = redcal_file.replace('.omni.', '.abs.')
    if os.path.exists(output_file) and not clobber:
        raise IOError("{} exists, not overwriting.".format(output_file))

    # Make raw_auto_file the data_file if None when appropriate, otherwise raise an error
    if raw_auto_file is None:
        if not data_is_redsol:
            raw_auto_file = data_file
        else:
            raise ValueError('If the data is a redundant visibility solution, raw_auto_file must be specified.')

    # Load redcal calibration
    hc = io.HERACal(redcal_file)
    rc_gains, rc_flags, rc_quals, rc_tot_qual = hc.read()
    auto_bls = [join_bl(ant, ant) for ant in rc_gains]

    # Initialize full-size, totally-flagged abscal gain/flag/etc. dictionaries
    abscal_gains = copy.deepcopy(rc_gains)
    abscal_flags = {ant: np.ones_like(rf) for ant, rf in rc_flags.items()}
    abscal_chisq_per_ant = {ant: np.zeros_like(rq) for ant, rq in rc_quals.items()}  # this stays zero, as it's not particularly meaningful
    abscal_chisq = {pol: np.zeros_like(rtq) for pol, rtq in rc_tot_qual.items()}

    # match times to narrow down model_files
    matched_model_files = sorted(set(match_times(data_file, model_files, filetype='uvh5')))
    if len(matched_model_files) == 0:
        echo("No model files overlap with data files in LST. Result will be fully flagged.", verbose=verbose)
    else:
        echo("The following model files overlap with data files in LST:\n" + "\n".join(matched_model_files), verbose=verbose)
        hd = io.HERAData(data_file)
        hdm = io.HERAData(matched_model_files)
        assert hdm.x_orientation == hd.x_orientation, 'Data x_orientation, {}, does not match model x_orientation, {}'.format(hd.x_orientation, hdm.x_orientation)
        assert hc.x_orientation == hd.x_orientation, 'Data x_orientation, {}, does not match redcal x_orientation, {}'.format(hd.x_orientation, hc.x_orientation)
        pol_load_list = [pol for pol in hd.pols if split_pol(pol)[0] == split_pol(pol)[1]]

        # get model bls and antpos to use later in baseline matching
        model_bls = hdm.bls
        model_antpos = hdm.antpos
        if len(matched_model_files) > 1:  # in this case, it's a dictionary
            model_bls = list(set([bl for bls in list(hdm.bls.values()) for bl in bls]))
            model_antpos = {ant: pos for antpos in hdm.antpos.values() for ant, pos in antpos.items()}

        # match integrations in model to integrations in data
        all_data_times, all_data_lsts = get_all_times_and_lsts(hd, solar_horizon=data_solar_horizon, unwrap=True)
        all_model_times, all_model_lsts = get_all_times_and_lsts(hdm, solar_horizon=model_solar_horizon, unwrap=True)
        d2m_time_map = get_d2m_time_map(all_data_times, all_data_lsts, all_model_times, all_model_lsts)
        
        # group matched time indices for partial I/O
        matched_tinds = [tind for tind, time in enumerate(hd.times) if time in d2m_time_map and d2m_time_map[time] is not None]
        if len(matched_tinds) > 0:
            tind_groups = np.array([matched_tinds])  # just load a single group
            if nInt_to_load is not None:  # split up the integrations to load nInt_to_load at a time
                tind_groups = np.split(matched_tinds, np.arange(nInt_to_load, len(matched_tinds), nInt_to_load))

            # loop over polarizations
            for pol in pol_load_list:
                echo('\n\nNow calibrating ' + pol + '-polarization...', verbose=verbose)

                # figure out whic 
                (data_bl_to_load,
                 model_bl_to_load,
                 data_to_model_bl_map) = match_baselines(hd.bls, model_bls, hd.antpos, model_antpos=model_antpos, pols=[pol],
                                                         data_is_redsol=data_is_redsol, model_is_redundant=model_is_redundant,
                                                         tol=tol, min_bl_cut=min_bl_cut, max_bl_cut=max_bl_cut, verbose=verbose)
                if (len(data_bl_to_load) == 0) or (len(model_bl_to_load) == 0):
                    echo("No baselines in the data match baselines in the model. Results for this polarization will be fully flagged.", verbose=verbose)
                else:
                    # loop over groups of time indices
                    for tinds in tind_groups:
                        echo('\n    Now calibrating times ' + str(hd.times[tinds[0]])
                             + ' through ' + str(hd.times[tinds[-1]]) + '...', verbose=verbose)
                        
                        # load data and apply calibration (unless data_is_redsol, so it's already redcal'ed)
                        data, flags, nsamples = hd.read(times=hd.times[tinds], bls=data_bl_to_load)
                        data_ants = set([ant for bl in data.keys() for ant in split_bl(bl)])
                        rc_gains_subset = {k: rc_gains[k][tinds, :] for k in data_ants}
                        rc_flags_subset = {k: rc_flags[k][tinds, :] for k in data_ants}
                        if not data_is_redsol:
                            calibrate_in_place(data, rc_gains_subset, data_flags=flags, 
                                               cal_flags=rc_flags_subset, gain_convention=hc.gain_convention)

                        if not np.all(list(flags.values())):
                            # load model and rephase
                            model_times_to_load = [d2m_time_map[time] for time in hd.times[tinds]]
                            model, model_flags, _ = io.partial_time_io(hdm, model_times_to_load, bls=model_bl_to_load)
                            model_blvecs = {bl: model.antpos[bl[0]] - model.antpos[bl[1]] for bl in model.keys()}
                            utils.lst_rephase(model, model_blvecs, model.freqs, data.lsts - model.lsts,
                                              lat=hdm.telescope_location_lat_lon_alt_degrees[0], inplace=True)

                            # Flag frequencies and times in the data that are entirely flagged in the model
                            model_flag_waterfall = np.all([f for f in model_flags.values()], axis=0)
                            for k in flags.keys():
                                flags[k] += model_flag_waterfall

                            # get the relative wgts for each piece of data
                            hd_autos = io.HERAData(raw_auto_file)
                            autocorrs, _, _ = hd_autos.read(times=hd.times[tinds], bls=auto_bls)
                            calibrate_in_place(autocorrs, rc_gains_subset, gain_convention=hc.gain_convention)

                            # use data_to_model_bl_map to rekey model. Does not copy to save memory.
                            model = DataContainer({bl: model[data_to_model_bl_map[bl]] for bl in data})
                            model_flags = DataContainer({bl: model_flags[data_to_model_bl_map[bl]] for bl in data})

                            # build data weights based on inverse noise variance and nsamples and flags
                            data_wgts = build_data_wgts(flags, nsamples, model_flags, autocorrs, times_by_bl=hd.times_by_bl, 
                                                        df=np.median(np.ediff1d(data.freqs)))

                            # run absolute calibration to get the gain updates
                            delta_gains = post_redcal_abscal(model, data, data_wgts, rc_flags_subset, edge_cut=edge_cut, tol=tol,
                                                             gain_convention=hc.gain_convention, phs_max_iter=phs_max_iter, 
                                                             phs_conv_crit=phs_conv_crit, verbose=verbose)

                            # abscal autos, rebuild weights, and generate abscal Chi^2
                            calibrate_in_place(autocorrs, delta_gains, gain_convention=hc.gain_convention)
                            chisq_wgts = build_data_wgts(flags, nsamples, model_flags, autocorrs, times_by_bl=hd.times_by_bl, 
                                                         df=np.median(np.ediff1d(data.freqs)))
                            total_qual, nObs, quals, nObs_per_ant = utils.chisq(data, model, chisq_wgts,
                                                                                gain_flags=rc_flags_subset, split_by_antpol=True)
                        
                            # update results
                            for ant in data_ants:
                                # new gains are the product of redcal gains and delta gains from abscal
                                abscal_gains[ant][tinds, :] = rc_gains_subset[ant] * delta_gains[ant]
                                # new flags are the OR of redcal flags and times/freqs totally flagged in the model
                                abscal_flags[ant][tinds, :] = rc_flags_subset[ant] + model_flag_waterfall
                            for antpol in total_qual.keys():
                                abscal_chisq[antpol][tinds, :] = total_qual[antpol] / nObs[antpol]  # Note, not normalized for DoF
                            
        # impose a single reference antenna on the final antenna solution
        if refant is None:
            refant = pick_reference_antenna(abscal_gains, abscal_flags, hc.freqs, per_pol=True)
        rephase_to_refant(abscal_gains, refant, flags=abscal_flags)

    # Save results to disk
    hc.update(gains=abscal_gains, flags=abscal_flags, quals=abscal_chisq_per_ant, total_qual=abscal_chisq)
    hc.quality_array[np.isnan(hc.quality_array)] = 0
    hc.total_quality_array[np.isnan(hc.total_quality_array)] = 0
    hc.history += version.history_string(add_to_history)
    hc.write_calfits(output_file, clobber=clobber)
    return hc


def post_redcal_abscal_argparser():
    ''' Argparser for commandline operation of hera_cal.abscal.post_redcal_abscal_run() '''
    a = argparse.ArgumentParser(description="Command-line drive script for post-redcal absolute calibration using hera_cal.abscal module")
    a.add_argument("data_file", type=str, help="string path to raw uvh5 visibility file or omnical_visibility solution")
    a.add_argument("redcal_file", type=str, help="string path to calfits file that serves as the starting point of abscal")
    a.add_argument("model_files", type=str, nargs='+', help="list of string paths to externally calibrated data or reference solution. Strings \
                                                             must be sortable to produce a chronological list in LST (wrapping over 2*pi is OK)")
    a.add_argument("--raw_auto_file", default=None, type=str, help="path to data file that contains raw autocorrelations for all antennas in redcal_file. \
                                                                  If not provided, data_file is used instead. Required if data_is_redsol is True.")
    a.add_argument("--data_is_redsol", default=False, action="store_true", help="If True, data_file only contains unique, redcal'ed visibilities.")
    a.add_argument("--model_is_redundant", default=False, action="store_true", help="If True, then model_files only containe unique visibilities.")
    a.add_argument("--output_file", default=None, type=str, help="string path to output abscal calfits file. If None, will be redcal_file.replace('.omni.', '.abs.'")
    a.add_argument("--nInt_to_load", default=None, type=int, help="number of integrations to load and calibrate simultaneously. Default None loads all integrations.")
    a.add_argument("--data_solar_horizon", default=90.0, type=float, help="Solar altitude threshold [degrees]. When the sun is too high in the data, flag the integration.")
    a.add_argument("--model_solar_horizon", default=90.0, type=float, help="Solar altitude threshold [degrees]. When the sun is too high in the model, flag the integration.")
    a.add_argument("--min_bl_cut", default=1.0, type=float, help="minimum baseline separation [meters] to keep in data when calibrating. None or 0 means no mininum, which will \
                                                                  include autocorrelations in the absolute calibration. Usually this is not desired, so the default is 1.0.")
    a.add_argument("--max_bl_cut", default=None, type=float, help="maximum baseline separation [meters] to keep in data when calibrating. None (default) means no maximum.")
    a.add_argument("--edge_cut", default=0, type=int, help="integer number of channels to exclude at each band edge in delay and global phase solvers")
    a.add_argument("--tol", default=1.0, type=float, help="baseline match tolerance in units of baseline vectors (e.g. meters)")
    a.add_argument("--phs_max_iter", default=100, type=int, help="integer maximum number of iterations of phase_slope_cal or TT_phs_cal allowed")
    a.add_argument("--phs_conv_crit", default=1e-6, type=float, help="convergence criterion for updates to iterative phase calibration that compares them to all 1.0s.")
    a.add_argument("--clobber", default=False, action="store_true", help="overwrites existing abscal calfits file at the output path")
    a.add_argument("--verbose", default=False, action="store_true", help="print calibration progress updates")
    args = a.parse_args()
    return args
