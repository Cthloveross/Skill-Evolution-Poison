from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import run_parallel_test_shard as sharding


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _make_run_root(tmp_path: Path) -> Path:
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "best_skill.md").write_text("skill", encoding="utf-8")
    (run_root / "config.json").write_text("{}", encoding="utf-8")
    return run_root


def _make_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, count: int = 3
) -> tuple[Path, list[Path]]:
    run_root = _make_run_root(tmp_path)
    destination = run_root / "test_eval"
    all_ids = [f"id-{index}" for index in range(8)]
    split_manifest = tmp_path / "split" / "split_manifest.json"
    test_items = tmp_path / "split" / "test" / "items.json"
    _write_json(split_manifest, {"ids": all_ids})
    _write_json(test_items, [{"id": item_id} for item_id in all_ids])
    runtime_contract = {"root": "/official", "revision": "rev", "files": []}
    monkeypatch.setattr(sharding, "_runtime_contract", lambda: runtime_contract)
    config_path = run_root / "config.json"
    checkpoint_path = run_root / "best_skill.md"
    common = {
        "run_root": str(run_root.resolve()),
        "canonical_destination": str(destination.resolve()),
        "config": {
            "path": str(config_path),
            "sha256": sharding.sha256_file(config_path),
        },
        "checkpoint": {
            "path": str(checkpoint_path),
            "sha256": sharding.sha256_file(checkpoint_path),
            "size_bytes": 5,
        },
        "split": {
            "split": "valid_unseen",
            "split_dir": str(tmp_path / "split"),
            "split_manifest_path": str(split_manifest),
            "split_manifest_sha256": sharding.sha256_file(split_manifest),
            "test_items_path": str(test_items),
            "test_items_sha256": sharding.sha256_file(test_items),
            "all_item_count": len(all_ids),
            "all_item_ids_sha256": sharding.hash_json(all_ids),
            "all_item_ids": all_ids,
        },
        "evaluation": {
            "environment": "searchqa",
            "target_model": "Qwen3.5-9B",
            "temperature": 0,
        },
        "official_runtime": runtime_contract,
    }
    common["evaluation_sha256"] = sharding.hash_json(common["evaluation"])
    shard_dirs: list[Path] = []
    for index in range(count):
        shard_dir = run_root / "acceleration" / "shards" / f"shard-{index}"
        shard_dir.mkdir(parents=True)
        ids = all_ids[index::count]
        rows = []
        for item_id in ids:
            rows.append({"id": item_id, "hard": 1, "agent_ok": True})
            prediction = shard_dir / "predictions" / item_id
            prediction.mkdir(parents=True)
            (prediction / "conversation.json").write_text("[]", encoding="utf-8")
        results = shard_dir / sharding.RESULTS_NAME
        results.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        binding = copy.deepcopy(common)
        binding["shard"] = {
            "index": index,
            "count": count,
            "item_count": len(ids),
            "item_ids_sha256": sharding.hash_json(ids),
            "item_ids": ids,
        }
        binding["endpoint"] = {
            "url": f"http://127.0.0.1:{19000 + index}/v1",
            "expected_model": "Qwen3.5-9B",
            "observed_model_ids": ["Qwen3.5-9B"],
        }
        manifest = {
            "schema_version": sharding.SCHEMA_VERSION,
            "kind": "skillopt_searchqa_test_shard",
            "status": "completed",
            "binding": binding,
            "result": {
                "sha256": sharding.sha256_file(results),
                "row_count": len(rows),
                "item_ids_sha256": sharding.hash_json(ids),
            },
        }
        sharding.atomic_write_json(shard_dir / sharding.MANIFEST_NAME, manifest)
        shard_dirs.append(shard_dir)
    return run_root, shard_dirs


def test_partition_is_disjoint_and_complete() -> None:
    items = [{"id": str(index)} for index in range(11)]
    shards = [sharding.partition_items(items, index, 3) for index in range(3)]
    flattened = [item["id"] for shard in shards for item in shard]
    assert len(flattened) == len(set(flattened)) == len(items)
    assert set(flattened) == {item["id"] for item in items}
    assert [item["id"] for item in shards[1]] == ["1", "4", "7", "10"]


@pytest.mark.parametrize("index,count", [(-1, 3), (3, 3), (0, 0)])
def test_partition_rejects_invalid_indices(index: int, count: int) -> None:
    with pytest.raises(sharding.AccelerationError):
        sharding.partition_items([{"id": "a"}], index, count)


def test_evaluate_paths_require_best_checkpoint_and_isolated_output(tmp_path: Path) -> None:
    run_root = _make_run_root(tmp_path)
    canonical = run_root / "test_eval"
    valid_output = run_root / "acceleration" / "shards" / "shard-0"
    resolved = sharding.validate_evaluate_paths(
        run_root=run_root,
        checkpoint=run_root / "best_skill.md",
        output_dir=valid_output,
        canonical_destination=canonical,
    )
    assert resolved[2] == valid_output.resolve()

    other = run_root / "skills" / "skill_v0020.md"
    other.parent.mkdir()
    other.write_text("skill", encoding="utf-8")
    with pytest.raises(sharding.AccelerationError, match="best checkpoint"):
        sharding.validate_evaluate_paths(
            run_root=run_root,
            checkpoint=other,
            output_dir=valid_output,
            canonical_destination=canonical,
        )
    with pytest.raises(sharding.AccelerationError, match="below"):
        sharding.validate_evaluate_paths(
            run_root=run_root,
            checkpoint=run_root / "best_skill.md",
            output_dir=run_root / "unsafe-shard",
            canonical_destination=canonical,
        )


def test_merge_accepts_complete_partition_and_preserves_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, shard_dirs = _make_shards(tmp_path, monkeypatch)
    destination = run_root / "test_eval"

    provenance = sharding.merge_shards(
        run_root=run_root, destination=destination, shard_dirs=shard_dirs
    )

    rows = [
        json.loads(line)
        for line in (destination / "results.jsonl").read_text().splitlines()
    ]
    assert [row["id"] for row in rows] == [f"id-{index}" for index in range(8)]
    assert provenance["row_count"] == 8
    assert provenance["prediction_directory_count"] == 8
    assert (destination / sharding.PROVENANCE_NAME).is_file()
    assert sorted(path.name for path in (destination / "predictions").iterdir()) == [
        f"id-{index}" for index in range(8)
    ]


def test_merge_rejects_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, shard_dirs = _make_shards(tmp_path, monkeypatch)
    destination = run_root / "test_eval"
    destination.mkdir()
    with pytest.raises(sharding.AccelerationError, match="already exists"):
        sharding.merge_shards(
            run_root=run_root, destination=destination, shard_dirs=shard_dirs
        )


def test_merge_rejects_missing_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, shard_dirs = _make_shards(tmp_path, monkeypatch)
    results = shard_dirs[0] / sharding.RESULTS_NAME
    lines = results.read_text().splitlines()
    results.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    manifest = sharding.read_json(shard_dirs[0] / sharding.MANIFEST_NAME)
    manifest["result"]["sha256"] = sharding.sha256_file(results)
    manifest["result"]["row_count"] -= 1
    sharding.atomic_write_json(shard_dirs[0] / sharding.MANIFEST_NAME, manifest)
    with pytest.raises(sharding.AccelerationError, match="coverage mismatch"):
        sharding.merge_shards(
            run_root=run_root,
            destination=run_root / "test_eval",
            shard_dirs=shard_dirs,
        )


def test_merge_rejects_duplicate_result_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, shard_dirs = _make_shards(tmp_path, monkeypatch)
    results = shard_dirs[0] / sharding.RESULTS_NAME
    first = results.read_text().splitlines()[0]
    with results.open("a", encoding="utf-8") as stream:
        stream.write(first + "\n")
    manifest = sharding.read_json(shard_dirs[0] / sharding.MANIFEST_NAME)
    manifest["result"]["sha256"] = sharding.sha256_file(results)
    manifest["result"]["row_count"] += 1
    sharding.atomic_write_json(shard_dirs[0] / sharding.MANIFEST_NAME, manifest)
    with pytest.raises(sharding.AccelerationError, match="duplicate result ID"):
        sharding.merge_shards(
            run_root=run_root,
            destination=run_root / "test_eval",
            shard_dirs=shard_dirs,
        )


@pytest.mark.parametrize("field", ["checkpoint", "split"])
def test_merge_rejects_mismatched_frozen_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    run_root, shard_dirs = _make_shards(tmp_path, monkeypatch)
    manifest_path = shard_dirs[1] / sharding.MANIFEST_NAME
    manifest = sharding.read_json(manifest_path)
    if field == "checkpoint":
        manifest["binding"]["checkpoint"]["sha256"] = "f" * 64
    else:
        manifest["binding"]["split"]["test_items_sha256"] = "f" * 64
    sharding.atomic_write_json(manifest_path, manifest)
    with pytest.raises(sharding.AccelerationError, match="contracts differ"):
        sharding.merge_shards(
            run_root=run_root,
            destination=run_root / "test_eval",
            shard_dirs=shard_dirs,
        )


def test_merge_requires_every_shard_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, shard_dirs = _make_shards(tmp_path, monkeypatch)
    with pytest.raises(sharding.AccelerationError, match="exactly shard indices"):
        sharding.merge_shards(
            run_root=run_root,
            destination=run_root / "test_eval",
            shard_dirs=shard_dirs[:-1],
        )


def test_merge_rehashes_frozen_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_root, shard_dirs = _make_shards(tmp_path, monkeypatch)
    (run_root / "best_skill.md").write_text("changed", encoding="utf-8")
    with pytest.raises(sharding.AccelerationError, match="checkpoint changed"):
        sharding.merge_shards(
            run_root=run_root,
            destination=run_root / "test_eval",
            shard_dirs=shard_dirs,
        )
