#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2019 The HERA Collaboration
# Licensed under the MIT License

from __future__ import print_function, division, absolute_import
import os
import sys
import numpy as np
import argparse
from pyuvdata import UVData
from pyuvdata.uvh5 import _hera_corr_dtype

# create an argument parser and parse args
ap = argparse.ArgumentParser(description="Extract only connected antennas from full correlator data")
ap.add_argument("input_file", type=str, help="Full path to target file")
ap.add_argument("output_file", type=str, help="Full path to output file")
ap.add_argument("--overwrite", action="store_true", default=False,
                help="Optional flag to overwrite output file if it exists")
args = ap.parse_args()

# read in file metadata
uvd = UVData()
fn_in = args.input_file
fn_out = args.output_file
if os.path.exists(fn_out) and not args.overwrite:
    print("skipping {}...".format(fn_in))
    sys.exit(0)
print("scanning {}...".format(fn_in))
uvd.read_uvh5(fn_in, read_data=False, run_check=False)

# figure out which antennas have valid data and perform select-on-read
ant_nums = np.unique(uvd.ant_1_array)
inds = np.where(ant_nums < 350)
data_ants = ant_nums[inds]
print("reading {}...".format(fn_in))
uvd.read_uvh5(fn_in, antenna_nums=data_ants)

# fix up the metadata to reflect the antennas in the dataset
uvd.Nants_telescope = len(data_ants)
uvd.antenna_names = [uvd.antenna_names[ind] for ind in data_ants]
uvd.antenna_numbers = uvd.antenna_numbers[data_ants]
uvd.antenna_positions = uvd.antenna_positions[data_ants, :]
uvd.antenna_diameters = uvd.antenna_diameters[data_ants]
print("writing {}...".format(fn_out))
uvd.write_uvh5(fn_out, data_write_dtype=_hera_corr_dtype, flags_compression='lzf',
               nsample_compression='lzf', clobber=True)
