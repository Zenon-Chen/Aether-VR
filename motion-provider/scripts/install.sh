#!/usr/bin/env sh
set -eu
python -m pip install --upgrade pip
python -m pip install '.[server]'
printf '%s\n' 'Next: install vendor PyTorch from https://pytorch.org/get-started/locally/ and DART dependencies:' 'python -m pip install -r <DART_ROOT>/requirements_dart.txt' 'Windows AMD/ROCm is unsupported by this provider.'
