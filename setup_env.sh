#!/usr/bin/env bash
# Creates the shared conda env "claude-env".
#   macOS -> environment.yml        (PyTorch with MPS)
#   Linux -> environment-linux.yml  (PyTorch with CUDA)
#   bash setup_env.sh            create (or update if it exists)
#   bash setup_env.sh --recreate delete and rebuild from scratch
set -euo pipefail
cd "$(dirname "$0")"
ENV=claude-env
FILE=environment.yml
[[ "$(uname)" == "Linux" ]] && FILE=environment-linux.yml
if ! command -v conda >/dev/null 2>&1; then
  echo "conda not found. Install Miniforge first:"
  if [[ "$(uname)" == "Linux" ]]; then
    echo '  curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"'
    echo '  bash Miniforge3-$(uname)-$(uname -m).sh -b && ~/miniforge3/bin/conda init bash   (then log out and back in)'
  else
    echo "  brew install --cask miniforge && conda init zsh   (then open a new Terminal)"
  fi
  exit 1
fi
echo "Using $FILE"
exists=$(conda env list | awk '{print $1}' | grep -qx "$ENV" && echo yes || echo no)
if [[ "${1:-}" == "--recreate" && $exists == yes ]]; then
  echo "Removing existing env $ENV ..."
  conda env remove -n "$ENV" -y
  exists=no
fi
if [[ $exists == yes ]]; then
  echo "Updating existing env $ENV ..."
  conda env update -n "$ENV" -f "$FILE" --prune
else
  echo "Creating env $ENV ..."
  conda env create -n "$ENV" -f "$FILE"
fi
echo "Checking the env:"
conda run -n "$ENV" python -c "
import numpy, scipy, matplotlib, tqdm, gymnasium, torch
x = torch.randn(256, 256) @ torch.randn(256, 256)   # exercises OpenMP/BLAS
print('torch', torch.__version__, '| numpy', numpy.__version__)
print('CUDA available:', torch.cuda.is_available(), '| GPUs:', torch.cuda.device_count(), '| MPS available:', torch.backends.mps.is_available())
for i in range(torch.cuda.device_count()):
    print(f'  cuda:{i}', torch.cuda.get_device_name(i))
    (torch.randn(1024, 1024, device=f'cuda:{i}') @ torch.randn(1024, 1024, device=f'cuda:{i}')).sum().item()
"
echo "Done. lab.py will run every agent inside this env automatically."
