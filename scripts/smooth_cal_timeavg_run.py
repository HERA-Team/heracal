#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2019 the HERA Project
# Licensed under the MIT License

"""Command-line drive script for hera_cal.smooth_cal.
That performs per-time gain fitting over a subband
and broadcasts the average to all times.
This script also supports flag factorization.
1) loads in the calibration solutions and (optionally) the associated flagging npzs for (usually) a whole day
2) performs a 2D time and frequency smoothing of the calibration solutions using aipy.deconv.clean (default scales 1800 s and 10 MHz)
4) writes the smoothed calibrations solutions to disk.
See help for a more detailed explanation of the parameters.
"""

from hera_cal.smooth_cal import CalibrationSmoother, smooth_cal_argparser
import sys

a = smooth_cal_argparser(mode='dpss_freqfilter_timeaverage')
if a.flag_file_list == ['none']:
    a.flag_file_list = None
if a.run_if_first is None or sorted(a.calfits_list)[0] == a.run_if_first:
    cs = CalibrationSmoother(a.calfits_list, flag_file_list=a.flag_file_list, flag_filetype=a.flag_filetype,
                             antflag_thresh=a.antflag_thresh, time_blacklists=a.time_blacklists,
                             lst_blacklists=a.lst_blacklists, freq_blacklists=a.freq_blacklists,
                             chan_blacklists=a.chan_blacklists, pick_refant=a.pick_refant, freq_threshold=a.freq_threshold,
                             time_threshold=a.time_threshold, ant_threshold=a.ant_threshold, verbose=a.verbose,
                             factorize_flags=a.factorize_flags, a_priori_flags_yaml=a.a_priori_flags_yaml, spw_range=a.spw_range)
    cs.freq_filter(filter_scale=a.freq_scale, mode='dpss_leastsq',
                   broadcast_time_average=True, skip_flagged_edge_freqs=True)
    cs.write_smoothed_cal(output_replace=(a.infile_replace, a.outfile_replace),
                          add_to_history=' '.join(sys.argv), clobber=a.clobber)
else:
    print(sorted(a.calfits_list)[0], 'is not', a.run_if_first, '...skipping.')
