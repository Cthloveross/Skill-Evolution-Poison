#!/usr/bin/env python3
"""Verify reused DyMal payloads and seal the active SearchQA run index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import experiment


def verify_payloads() -> dict[str, Any]:
    receipts: dict[str, Any] = {}
    for attack in experiment.ATTACKS:
        payload = experiment.payload_path(attack)
        manifest = payload.with_name("attack_manifest.json")
        source = payload.with_name("source_payload.md")
        for path in (payload, manifest, source):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"missing regular reused attack artifact: {path}")
        digest = experiment.sha256_file(payload)
        if digest != experiment.FROZEN_PAYLOAD_SHA256[attack]:
            raise ValueError(f"reused payload hash differs: {attack}")
        value = experiment.read_json(manifest)
        if not isinstance(value, dict) or value.get("attack") != attack:
            raise ValueError(f"invalid reused attack manifest: {manifest}")
        if value.get("payload_sha256") != digest:
            raise ValueError(f"attack manifest is not bound to payload: {attack}")
        receipts[attack] = {
            "payload_path": str(payload.resolve()),
            "payload_sha256": digest,
            "payload_bytes": payload.stat().st_size,
            "source_payload_path": str(source.resolve()),
            "source_payload_sha256": experiment.sha256_file(source),
            "source_manifest_path": str(manifest.resolve()),
            "source_manifest_sha256": experiment.sha256_file(manifest),
            "reuse_source_experiment": "self-evolution-half-full-8gpu",
        }
    return receipts


def verify_initials() -> dict[str, Any]:
    receipts: dict[str, Any] = {}
    for benchmark in experiment.BENCHMARKS:
        official = experiment.official_initial_path(benchmark)
        official_payload = official.read_bytes()
        if experiment.sha256_bytes(official_payload) != experiment.OFFICIAL_INITIAL_SHA256[benchmark]:
            raise ValueError(f"official initial drift: {benchmark}")
        for condition in experiment.CONDITIONS:
            path = experiment.initial_path(benchmark, condition)
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"missing regular reused initial: {path}")
            payload = path.read_bytes()
            expected = (
                official_payload
                if condition == "clean"
                else experiment.payload_path(condition).read_bytes() + official_payload
            )
            if payload != expected:
                raise ValueError(
                    f"initial is not exact payload+official bytes: {benchmark}/{condition}"
                )
            digest = experiment.sha256_bytes(payload)
            if digest != experiment.MATERIALIZED_INITIAL_SHA256[benchmark][condition]:
                raise ValueError(f"reused initial hash differs: {benchmark}/{condition}")
            receipt = {
                "schema_version": 2,
                "experiment_id": experiment.EXPERIMENT_ID,
                "benchmark": benchmark,
                "condition": "clean" if condition == "clean" else "attacked",
                "attack": None if condition == "clean" else condition,
                "path": str(path.resolve()),
                "sha256": digest,
                "bytes": len(payload),
                "official_initial_path": str(official.resolve()),
                "official_initial_sha256": experiment.sha256_bytes(official_payload),
                "payload_path": (
                    None
                    if condition == "clean"
                    else str(experiment.payload_path(condition).resolve())
                ),
                "operation": (
                    "official_initial_bytes"
                    if condition == "clean"
                    else "reused_payload_bytes + official_initial_bytes"
                ),
            }
            experiment.atomic_write_json(path.with_name("injection_receipt.json"), receipt)
            receipts[f"{benchmark}/{condition}"] = receipt
    return receipts


def seal() -> dict[str, Any]:
    payloads = verify_payloads()
    initials = verify_initials()
    hashes: dict[str, str] = {}
    for benchmark in experiment.BENCHMARKS:
        manifest = experiment.split_manifest_path(benchmark)
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError(f"data must be materialized first: {manifest}")
        hashes[benchmark] = experiment.sha256_file(manifest)
    index = experiment.build_run_plan(hashes)
    experiment.validate_run_index(index, verify_files=True)
    experiment.atomic_write_json(experiment.RUN_INDEX, index)
    experiment.atomic_write_json(
        experiment.EXPERIMENT_DIR / "records" / "initial-skill-index.json",
        {
            **index,
            "contract": (
                "exact official initial.md or exact reused DyMal payload bytes prepended "
                "to official initial.md"
            ),
            "payload_receipts": payloads,
            "initial_receipts": initials,
        },
    )
    return index


def main() -> int:
    result = seal()
    print(json.dumps({"status": "ready", "runs": len(result["runs"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
