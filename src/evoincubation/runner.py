from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from evoincubation.canary import generate_block_data
from evoincubation.config import ExperimentConfig
from evoincubation.design import build_design, parse_bool_cell, save_design
from evoincubation.io_utils import (
    exclusive_lock,
    read_csv,
    read_json,
    runtime_provenance,
    stable_hash,
    write_json,
)


def prepare_experiment(config: ExperimentConfig) -> Path:
    resolved_path = config.output_root / "resolved_experiment_config.json"
    config_hash = stable_hash(config.resolved(), 32)
    if resolved_path.exists():
        existing_hash = stable_hash(read_json(resolved_path), 32)
        if existing_hash != config_hash:
            raise RuntimeError(
                f"Experiment root {config.output_root} already contains a different config. "
                "Use a new experiment.output_root."
            )
    rows = build_design(config)
    design_path = save_design(config, rows)
    candidate_by_id = {str(candidate["id"]): candidate for candidate in config.seed_candidates}
    generated: set[str] = set()
    for row in rows:
        if row.block_id in generated:
            continue
        generate_block_data(
            config,
            block_id=row.block_id,
            data_seed=row.data_seed,
            seed_candidate=candidate_by_id[row.seed_id],
        )
        generated.add(row.block_id)
    write_json(resolved_path, config.resolved())
    return design_path


def _coerce_design_row(row: dict[str, str]) -> dict[str, Any]:
    return {
        **row,
        "replicate": int(row["replicate"]),
        "seed_present": parse_bool_cell(row["seed_present"]),
        "evolution_enabled": parse_bool_cell(row["evolution_enabled"]),
        "run_order": int(row["run_order"]),
        "lineage_seed": int(row["lineage_seed"]),
        "data_seed": int(row["data_seed"]),
    }


def load_design_rows(config: ExperimentConfig) -> list[dict[str, Any]]:
    path = config.output_root / "design.csv"
    if not path.exists():
        prepare_experiment(config)
    return [_coerce_design_row(row) for row in read_csv(path)]


def select_lineage(
    config: ExperimentConfig,
    *,
    lineage_id: str | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    rows = load_design_rows(config)
    if lineage_id is not None:
        matches = [row for row in rows if row["lineage_id"] == lineage_id]
        if len(matches) != 1:
            raise ValueError(f"Unknown or duplicate lineage_id: {lineage_id}")
        return matches[0]
    if index is None:
        raise ValueError("Provide either lineage_id or zero-based index")
    if index < 0 or index >= len(rows):
        raise IndexError(f"Lineage index {index} outside [0, {len(rows) - 1}]")
    return rows[index]


def run_lineage(config: ExperimentConfig, row: dict[str, Any], *, force: bool = False) -> Path:
    lineage_dir = config.output_root / "lineages" / str(row["lineage_id"])
    complete_path = lineage_dir / "COMPLETE.json"
    config_hash = stable_hash(config.resolved(), 32)
    if complete_path.exists() and not force:
        complete = read_json(complete_path)
        if complete.get("experiment_config_hash") != config_hash:
            raise RuntimeError(
                f"Completed lineage {row['lineage_id']} belongs to a different config. "
                "Use a new output_root or rerun with --force to preserve a backup."
            )
        return lineage_dir
    if force and lineage_dir.exists():
        backup = lineage_dir.with_name(
            f"{lineage_dir.name}.backup-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )
        shutil.move(str(lineage_dir), str(backup))
    lineage_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lineage_dir / ".lock"
    with exclusive_lock(lock_path):
        started = dt.datetime.now(dt.timezone.utc).isoformat()
        write_json(
            lineage_dir / "allocation.json",
            {
                **row,
                "started_utc": started,
                "runtime": runtime_provenance(),
                "experiment_config_hash": config_hash,
                "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
                "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
            },
        )
        write_json(lineage_dir / "STATUS.json", {"status": "running", "started_utc": started})
        data_root = config.output_root / "data" / str(row["block_id"])
        try:
            if str(row["engine"]) == "mock":
                from evoincubation.mock_engine import run_mock_lineage

                result = run_mock_lineage(config, row, lineage_dir)
            elif str(row["engine"]) == "skillopt":
                from evoincubation.skillopt_bridge import run_skillopt_lineage

                result = run_skillopt_lineage(config, row, data_root, lineage_dir)
            else:
                raise ValueError(f"Unknown engine: {row['engine']}")
        except Exception as exc:
            failure = {
                "status": "failed",
                "ended_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            write_json(lineage_dir / "STATUS.json", failure)
            raise
        ended = dt.datetime.now(dt.timezone.utc).isoformat()
        complete = {
            "status": "complete",
            "started_utc": started,
            "ended_utc": ended,
            "experiment_config_hash": config_hash,
            **result,
        }
        write_json(lineage_dir / "STATUS.json", complete)
        write_json(complete_path, complete)
    return lineage_dir


def run_block(config: ExperimentConfig, block_id: str, *, force: bool = False) -> list[Path]:
    rows = [row for row in load_design_rows(config) if row["block_id"] == block_id]
    if len(rows) != 4:
        raise ValueError(f"Expected four arms in block {block_id}, found {len(rows)}")
    rows.sort(key=lambda row: row["run_order"])
    outputs: list[Path] = []
    failures: list[str] = []
    for row in rows:
        command = [
            sys.executable,
            "-m",
            "evoincubation.cli",
            "run-lineage",
            "--config",
            str(config.path),
            "--lineage-id",
            str(row["lineage_id"]),
        ]
        if force:
            command.append("--force")
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            failures.append(str(row["lineage_id"]))
        outputs.append(config.output_root / "lineages" / str(row["lineage_id"]))
    if failures:
        raise RuntimeError(
            "One or more lineages failed after all randomized arms were attempted: "
            + ", ".join(failures)
        )
    return outputs


def block_ids(config: ExperimentConfig) -> list[str]:
    rows = load_design_rows(config)
    ordered: list[str] = []
    for row in rows:
        if row["block_id"] not in ordered:
            ordered.append(str(row["block_id"]))
    return ordered


def validate_prepared_experiment(config: ExperimentConfig) -> dict[str, Any]:
    rows = load_design_rows(config)
    errors: list[str] = []
    for block in block_ids(config):
        block_rows = [row for row in rows if row["block_id"] == block]
        arms = {(row["seed_present"], row["evolution_enabled"]) for row in block_rows}
        if len(block_rows) != 4 or len(arms) != 4:
            errors.append(f"Block {block} is not a complete 2x2")
        data_root = config.output_root / "data" / block
        manifest_path = data_root / "data_manifest.json"
        if not manifest_path.exists():
            errors.append(f"Missing data manifest: {manifest_path}")
            continue
        manifest = read_json(manifest_path)
        if manifest.get("integrity", {}).get("trigger_in_train") is not False:
            errors.append(f"Block {block} does not assert trigger/train separation")
    report = {
        "ok": not errors,
        "n_lineages": len(rows),
        "n_blocks": len(block_ids(config)),
        "errors": errors,
    }
    write_json(config.output_root / "validation_report.json", report)
    return report
