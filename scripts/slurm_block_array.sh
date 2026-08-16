#!/usr/bin/env bash
# Submit only after running `evoincubation prepare` and `evoincubation validate` once.
# Example:
#   CONFIG_PATH="$PWD/configs/pilot_skillopt.yaml" sbatch --array=0-1 scripts/slurm_block_array.sh

#SBATCH --job-name=evoincubation
#SBATCH --output=slurm-%A_%a.out
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G

set -euo pipefail

: "${CONFIG_PATH:?Set CONFIG_PATH to an absolute experiment YAML path}"
: "${SLURM_ARRAY_TASK_ID:?This script must run as a Slurm array job}"

python -m evoincubation.cli run-block \
  --config "${CONFIG_PATH}" \
  --index "${SLURM_ARRAY_TASK_ID}"
