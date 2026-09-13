#!/bin/bash
#SBATCH --job-name=vigil-eval
#SBATCH --partition=gpu
#SBATCH --gres=gpu:v100-sxm2:1
#SBATCH --mem=32G
#SBATCH --time=01:30:00
#SBATCH --output=vigil-eval-%j.log
set -euo pipefail
cd ~/vigil-streaming-anomaly-platform
module load anaconda3/2024.06
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate ~/vigilrun
export HF_HUB_OFFLINE=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
python evaluate_planner.py --adapter artifacts/planner-qlora --report-json artifacts/planner-eval.json
echo "===== EVAL DONE ====="
