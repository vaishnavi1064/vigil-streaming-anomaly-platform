#!/bin/bash
#SBATCH --job-name=vigil-b3
#SBATCH --partition=gpu
#SBATCH --gres=gpu:v100-sxm2:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=08:00:00
#SBATCH --output=vigil-b3-%j.log
set -euo pipefail
cd ~/vigil-streaming-anomaly-platform
module load anaconda3/2024.06
source "$(conda info --base)/etc/profile.d/conda.sh"
set +u; conda activate ~/vigilrun; set -u
cat > constraints.txt << 'CONS'
torch==2.5.1
transformers==4.46.3
peft==0.13.2
trl==0.12.0
bitsandbytes==0.45.0
CONS
[ -d artifacts/planner-dataset ] || python build_planner_dataset.py
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
python train_planner.py
python evaluate_planner.py --adapter artifacts/planner-qlora --report-json artifacts/planner-eval.json
echo "===== DONE ===== result: artifacts/planner-eval.json"
