$ErrorActionPreference = 'Stop'
python -m pip install --upgrade pip
python -m pip install '.[server]'
Write-Host 'Next: install vendor PyTorch from https://pytorch.org/get-started/locally/ and DART dependencies:'
Write-Host 'python -m pip install -r <DART_ROOT>\requirements_dart.txt'
Write-Host 'Windows AMD/ROCm is unsupported by this provider.'
