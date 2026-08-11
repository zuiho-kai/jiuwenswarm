#!/usr/bin/env bash
set -euo pipefail

service_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
site_packages="$($service_dir/.venv/bin/python -c 'import site; print(site.getsitepackages()[0])')"

export CUDA_VISIBLE_DEVICES="${ASR_CUDA_VISIBLE_DEVICES:-3}"
export LD_LIBRARY_PATH="$site_packages/nvidia/cublas/lib:$site_packages/nvidia/cudnn/lib:${LD_LIBRARY_PATH:-}"
export ASR_MODEL_ID="${ASR_MODEL_ID:-mobiuslabsgmbh/faster-whisper-large-v3-turbo}"
export ASR_API_KEY="${ASR_API_KEY:-EMPTY}"
export ASR_DEVICE="${ASR_DEVICE:-cuda}"
export ASR_COMPUTE_TYPE="${ASR_COMPUTE_TYPE:-float16}"

cd "$service_dir"
exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port "${ASR_PORT:-8101}"
