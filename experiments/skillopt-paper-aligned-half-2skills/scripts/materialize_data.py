#!/usr/bin/env python3
"""Invoke and verify the canonical sealed split adapter."""

from __future__ import annotations

import json
import subprocess

import experiment


ADAPTER = experiment.DATA_ROOT / "materialize_from_frozen_parent.py"
ADAPTER_SHA256 = "fcc5623c7bcb3c2d4d31218685ba1d3a33d2621493ba665b6ca056cc185313c5"
AUDIT = experiment.DATA_ROOT / "materialization_audit.json"


def verify() -> dict:
    if ADAPTER.is_symlink() or experiment.sha256_file(ADAPTER) != ADAPTER_SHA256:
        raise ValueError("canonical data adapter identity differs")
    value = experiment.read_json(AUDIT)
    observed_counts = value.get("counts")
    if (
        value.get("status") != "passed"
        or observed_counts != experiment.MATERIALIZED_COUNTS_BY_BENCHMARK
    ):
        raise ValueError("canonical materialization audit failed")
    for benchmark in experiment.BENCHMARKS:
        if observed_counts.get(benchmark) != experiment.counts_for(benchmark):
            raise ValueError(f"canonical materialization audit differs: {benchmark}")
        manifest = experiment.read_json(experiment.split_manifest_path(benchmark))
        if manifest.get("counts") != experiment.counts_for(benchmark):
            raise ValueError(f"sealed split count differs: {benchmark}")
    return value


def main() -> int:
    subprocess.run([str(experiment.SKILLOPT_PYTHON), str(ADAPTER)], check=True)
    value = verify()
    print(json.dumps({"status": value["status"], "counts": value["counts"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
