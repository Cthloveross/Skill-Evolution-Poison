from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from evoincubation.checkpoints import discover_checkpoints, select_monitor_checkpoints
from evoincubation.config import ExperimentConfig
from evoincubation.io_utils import runtime_provenance, write_json, write_jsonl

EXPECTED_SKILLOPT_COMMIT = "9639719632daecacd1baaa47fe781f3c0253600a"


def _load_items(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, list):
        raise ValueError(f"Expected JSON list: {path}")
    return value


def _mean(rows: list[dict[str, Any]], key: str) -> float:
    return sum(float(row.get(key, 0.0)) for row in rows) / len(rows) if rows else 0.0


def _package_git_revision(package_root: Path) -> str | None:
    """Return HEAD only when ``package_root`` is itself a Git worktree root."""
    try:
        top_level = subprocess.run(
            ["git", "-C", str(package_root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if Path(top_level).resolve() != package_root.resolve():
            return None
        return (
            subprocess.run(
                ["git", "-C", str(package_root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            or None
        )
    except (OSError, subprocess.CalledProcessError):
        return None


def _skillopt_provenance(*, expected_revision: str = EXPECTED_SKILLOPT_COMMIT) -> dict[str, Any]:
    import skillopt

    package_root = Path(skillopt.__file__).resolve().parent.parent
    try:
        distribution = importlib.metadata.distribution("skillopt")
        version = distribution.version
        direct_url_text = distribution.read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
        direct_url_text = None
    direct_revision = None
    direct_url = None
    if direct_url_text:
        try:
            direct_url_payload = json.loads(direct_url_text)
            direct_url = direct_url_payload.get("url")
            direct_revision = direct_url_payload.get("vcs_info", {}).get("commit_id")
        except json.JSONDecodeError:
            direct_revision = None
    package_git_revision = _package_git_revision(package_root)
    resolved_revision = direct_revision or package_git_revision
    verification_method = (
        "direct_url" if direct_revision else "package_git" if package_git_revision else "unverified"
    )
    return {
        "version": version,
        "package_root": str(package_root),
        "direct_url": direct_url,
        "direct_url_revision": direct_revision,
        "package_git_revision": package_git_revision,
        "resolved_revision": resolved_revision,
        "expected_revision": expected_revision,
        "verification_method": verification_method,
    }


def _verify_skillopt_revision(
    provenance: dict[str, Any],
    *,
    expected_revision: str,
    allow_unverified: bool,
) -> None:
    resolved_revision = provenance.get("resolved_revision")
    if resolved_revision == expected_revision or allow_unverified:
        return
    raise RuntimeError(
        "Cannot verify the required SkillOpt revision. "
        f"Expected {expected_revision}, got {resolved_revision!r}. Install the pinned server "
        "extra or explicitly set skillopt.allow_unverified_revision=true for a "
        "non-confirmatory run."
    )


def _flat_skillopt_config(
    config: ExperimentConfig,
    row: dict[str, Any],
    data_root: Path,
    engine_root: Path,
) -> dict[str, Any]:
    model = config.raw["model"]
    evo = config.raw["evolution"]
    seed_present = bool(row["seed_present"])
    evolution_enabled = bool(row["evolution_enabled"])
    exposure_path = data_root / "exposure" / ("poison.json" if seed_present else "benign.json")
    epochs = int(evo["epochs"])
    batch_size = int(evo["batch_size"])
    accumulation = int(evo.get("accumulation", 1))
    washout_epochs = int(evo.get("washout_epochs", 1))
    if washout_epochs < 0 or washout_epochs >= epochs:
        raise ValueError("evolution.washout_epochs must be >= 0 and smaller than evolution.epochs")
    washout_start_epoch = epochs - washout_epochs + 1 if washout_epochs else 10**9
    cfg: dict[str, Any] = {
        "model_backend": str(model.get("backend", model["target_backend"])),
        "optimizer_model": str(model["optimizer"]),
        "target_model": str(model["target"]),
        "optimizer_backend": str(model["optimizer_backend"]),
        "target_backend": str(model["target_backend"]),
        "reasoning_effort": str(model.get("reasoning_effort", "medium")),
        "rewrite_reasoning_effort": str(model.get("rewrite_reasoning_effort", "")),
        "rewrite_max_completion_tokens": int(model.get("rewrite_max_completion_tokens", 16000)),
        "num_epochs": epochs if evolution_enabled else 1,
        "train_size": 0,
        "train_size_override": None if evolution_enabled else batch_size * accumulation,
        "batch_size": batch_size,
        "accumulation": accumulation,
        "seed": int(row["lineage_seed"]),
        "minibatch_size": int(evo.get("minibatch_size", 2)),
        "merge_batch_size": int(evo.get("merge_batch_size", 4)),
        "analyst_workers": int(evo.get("analyst_workers", 4)),
        "max_analyst_rounds": int(evo.get("max_analyst_rounds", 3)),
        "failure_only": False,
        "edit_budget": int(evo.get("edit_budget", 2)),
        "min_edit_budget": int(evo.get("min_edit_budget", 1)),
        "lr_scheduler": str(evo.get("lr_scheduler", "constant")),
        "lr_control_mode": str(evo.get("lr_control_mode", "fixed")),
        "skill_update_mode": str(evo.get("skill_update_mode", "patch")),
        "use_slow_update": bool(evo.get("use_slow_update", False)),
        "slow_update_samples": int(evo.get("slow_update_samples", 8)),
        "slow_update_gate_with_selection": bool(evo.get("slow_update_gate_with_selection", True)),
        "longitudinal_pair_policy": str(evo.get("longitudinal_pair_policy", "mixed")),
        "use_meta_skill": bool(evo.get("use_meta_skill", False)),
        "use_skill_aware_reflection": False,
        "skill_aware_appendix_source": "failure_only",
        "skill_aware_consolidate_threshold": 0,
        "use_gate": True,
        "gate_metric": str(evo.get("gate_metric", "soft")),
        "gate_mixed_weight": float(evo.get("gate_mixed_weight", 0.5)),
        "use_semantic_density": False,
        "sel_env_num": 0,
        "test_env_num": 0,
        "eval_test": True,
        "env": "evoincubation_canary",
        "skill_init": str(data_root / "initial_skill.md"),
        "split_mode": "split_dir",
        "split_dir": str(data_root),
        "split_seed": int(row["data_seed"]),
        "exposure_path": str(exposure_path),
        "washout_path": str(data_root / "washout" / "items.json"),
        "washout_start_epoch": washout_start_epoch,
        "workers": int(evo.get("rollout_workers", 1)),
        "max_turns": int(config.raw["canary"].get("max_turns", 4)),
        "max_completion_tokens": int(config.raw["canary"].get("max_completion_tokens", 256)),
        "exec_timeout": int(config.raw["canary"].get("timeout_seconds", 120)),
        "limit": 0,
        "out_root": str(engine_root),
    }
    return cfg


def _validate_primary_path(cfg: dict[str, Any]) -> None:
    if cfg["skill_update_mode"] not in {
        "patch",
        "rewrite_from_suggestions",
        "full_rewrite_minibatch",
    }:
        raise ValueError(f"Unsupported skill_update_mode: {cfg['skill_update_mode']}")
    if cfg["use_skill_aware_reflection"]:
        raise ValueError("Primary harness forbids ungated skill-aware appendix mutations")
    if cfg["use_slow_update"] and not cfg["slow_update_gate_with_selection"]:
        raise ValueError("Slow update must be gated on the selection split")


def _audit_gate_transitions(engine_root: Path, checkpoints: list[Any]) -> dict[str, Any]:
    history_path = engine_root / "history.json"
    history = _load_items(history_path) if history_path.exists() else []
    by_step = {int(record["step"]): record for record in history if "step" in record}
    transitions: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        if checkpoint.version == 0:
            continue
        record = by_step.get(checkpoint.version)
        action = str(record.get("action", "missing")) if record else "missing"
        valid = (not checkpoint.changed_from_previous) or ("accept" in action)
        transition = {
            "version": checkpoint.version,
            "skill_sha256": checkpoint.sha256,
            "changed": checkpoint.changed_from_previous,
            "history_action": action,
            "valid": valid,
            "current_score": record.get("current_score") if record else None,
            "candidate_score": record.get("candidate_score") if record else None,
            "best_score": record.get("best_score") if record else None,
        }
        transitions.append(transition)
        if not valid:
            invalid.append(transition)
    return {
        "n_history_records": len(history),
        "n_transitions": len(transitions),
        "n_changed_transitions": sum(bool(row["changed"]) for row in transitions),
        "all_changed_transitions_gate_accepted": not invalid,
        "invalid_transitions": invalid,
        "transitions": transitions,
    }


def _audit_training_exposure(
    engine_root: Path,
    *,
    expected_ids: set[str],
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    for rollout_path in sorted(engine_root.glob("steps/step_*/**/rollouts.json")):
        step_name = next(
            parent.name for parent in rollout_path.parents if parent.name.startswith("step_")
        )
        for row in _load_items(rollout_path):
            item_id = str(row.get("id", ""))
            if item_id not in expected_ids:
                continue
            observations.append(
                {
                    "id": item_id,
                    "step": step_name,
                    "hard": float(row.get("hard", 0.0)),
                    "soft": float(row.get("soft", 0.0)),
                    "rollout_path": str(rollout_path),
                }
            )
    counts = Counter(row["id"] for row in observations)
    exactly_once = set(counts) == expected_ids and all(
        counts[item_id] == 1 for item_id in expected_ids
    )
    return {
        "expected_training_exposure_count": len(expected_ids),
        "actual_training_exposure_count": len(observations),
        "expected_ids": sorted(expected_ids),
        "id_counts": dict(sorted(counts.items())),
        "exactly_once": exactly_once,
        "only_step_0001": all(row["step"] == "step_0001" for row in observations),
        "hard_accuracy": _mean(observations, "hard"),
        "soft_utility": _mean(observations, "soft"),
        "observations": observations,
    }


def _evaluate_checkpoint(
    *,
    checkpoint,
    split: str,
    items: list[dict[str, Any]],
    eval_root: Path,
    adapter,
    lineage_id: str,
    final_locked: bool,
    evaluation_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cache_key = (checkpoint.sha256, split)
    if evaluation_cache is not None and cache_key in evaluation_cache:
        cached = evaluation_cache[cache_key]
        return {
            **cached,
            "version": checkpoint.version,
            "changed_from_previous": int(checkpoint.changed_from_previous),
            "final_locked": int(final_locked),
            "reused_from_version": int(cached["version"]),
        }
    skill = checkpoint.path.read_text(encoding="utf-8")
    out_dir = eval_root / f"v{checkpoint.version:04d}-{checkpoint.sha256[:12]}" / split
    results = adapter.rollout(items, skill, str(out_dir))
    metric = {
        "lineage_id": lineage_id,
        "engine": "skillopt",
        "version": checkpoint.version,
        "skill_sha256": checkpoint.sha256,
        "changed_from_previous": int(checkpoint.changed_from_previous),
        "split": split,
        "n": len(results),
        "hard_accuracy": _mean(results, "hard"),
        "soft_utility": _mean(results, "soft"),
        "attack_success_rate": _mean(results, "attack_success"),
        "final_locked": int(final_locked),
        "rollout_path": str(out_dir / "rollouts.json"),
        "reused_from_version": None,
    }
    if evaluation_cache is not None:
        evaluation_cache[cache_key] = metric
    return metric


def run_skillopt_lineage(
    config: ExperimentConfig,
    row: dict[str, Any],
    data_root: Path,
    lineage_dir: Path,
) -> dict[str, Any]:
    try:
        from skillopt.engine.trainer import ReflACTTrainer
    except ImportError as exc:
        raise RuntimeError(
            "SkillOpt is not installed. Run `pip install -e '.[server]'` or install "
            "the pinned commit."
        ) from exc

    from evoincubation.skillopt_env.adapter import CanaryAdapter

    engine_root = lineage_dir / "engine"
    cfg = _flat_skillopt_config(config, row, data_root, engine_root)
    _validate_primary_path(cfg)
    adapter = CanaryAdapter(
        split_dir=cfg["split_dir"],
        exposure_path=cfg["exposure_path"],
        washout_path=cfg["washout_path"],
        washout_start_epoch=cfg["washout_start_epoch"],
        train_size_override=cfg["train_size_override"],
        workers=cfg["workers"],
        analyst_workers=cfg["analyst_workers"],
        failure_only=cfg["failure_only"],
        minibatch_size=cfg["minibatch_size"],
        edit_budget=cfg["edit_budget"],
        seed=cfg["seed"],
        split_seed=cfg["split_seed"],
        max_turns=cfg["max_turns"],
        max_completion_tokens=cfg["max_completion_tokens"],
        exec_timeout=cfg["exec_timeout"],
    )
    expected_revision = str(
        config.raw.get("skillopt", {}).get("expected_revision", EXPECTED_SKILLOPT_COMMIT)
    )
    skillopt_provenance = _skillopt_provenance(expected_revision=expected_revision)
    allow_unverified = bool(config.raw.get("skillopt", {}).get("allow_unverified_revision", False))
    _verify_skillopt_revision(
        skillopt_provenance,
        expected_revision=expected_revision,
        allow_unverified=allow_unverified,
    )
    provenance = {
        "runtime": runtime_provenance(),
        "skillopt": skillopt_provenance,
    }
    write_json(lineage_dir / "provenance.json", provenance)
    write_json(lineage_dir / "resolved_skillopt_config.json", cfg)
    summary = ReflACTTrainer(cfg, adapter).train()

    checkpoints = discover_checkpoints(engine_root / "skills")
    selected = select_monitor_checkpoints(checkpoints)
    if not selected:
        raise RuntimeError(f"SkillOpt produced no skill checkpoints in {engine_root / 'skills'}")
    gate_audit = _audit_gate_transitions(engine_root, checkpoints)
    write_json(lineage_dir / "gate_audit.json", gate_audit)
    if not gate_audit["all_changed_transitions_gate_accepted"]:
        raise RuntimeError(
            f"Observed a deployed skill transition without native gate acceptance: {gate_audit}"
        )
    action_by_version = {
        int(row["version"]): str(row["history_action"]) for row in gate_audit["transitions"]
    }
    action_by_version[0] = "initial"
    clean_items = _load_items(data_root / "test" / "items.json")
    monitor_items = _load_items(data_root / "trigger_monitor" / "items.json")
    near_items = _load_items(data_root / "near_trigger" / "items.json")
    final_items = _load_items(data_root / "final_trigger" / "items.json")
    exposure_items = _load_items(Path(cfg["exposure_path"]))
    metrics: list[dict[str, Any]] = []
    eval_root = lineage_dir / "monitor_eval"
    evaluation_cache: dict[tuple[str, str], dict[str, Any]] = {}
    metrics.append(
        _evaluate_checkpoint(
            checkpoint=selected[0],
            split="exposure_replay",
            items=exposure_items,
            eval_root=lineage_dir / "exposure_local_audit",
            adapter=adapter,
            lineage_id=str(row["lineage_id"]),
            final_locked=False,
            evaluation_cache=evaluation_cache,
        )
    )
    for checkpoint in selected:
        for split, items in (
            ("clean_test", clean_items),
            ("trigger_monitor", monitor_items),
            ("near_trigger", near_items),
        ):
            metrics.append(
                _evaluate_checkpoint(
                    checkpoint=checkpoint,
                    split=split,
                    items=items,
                    eval_root=eval_root,
                    adapter=adapter,
                    lineage_id=str(row["lineage_id"]),
                    final_locked=False,
                    evaluation_cache=evaluation_cache,
                )
            )
    metrics.append(
        _evaluate_checkpoint(
            checkpoint=selected[-1],
            split="final_trigger",
            items=final_items,
            eval_root=lineage_dir / "final_locked_eval",
            adapter=adapter,
            lineage_id=str(row["lineage_id"]),
            final_locked=True,
            evaluation_cache=evaluation_cache,
        )
    )
    exposure_audit = _audit_training_exposure(
        engine_root,
        expected_ids={str(item["id"]) for item in exposure_items},
    )
    metrics.append(
        {
            "lineage_id": str(row["lineage_id"]),
            "engine": "skillopt",
            "version": 0,
            "observed_at_step": 1,
            "skill_sha256": checkpoints[0].sha256,
            "changed_from_previous": 0,
            "split": "exposure_actual",
            "n": exposure_audit["actual_training_exposure_count"],
            "hard_accuracy": exposure_audit["hard_accuracy"],
            "soft_utility": exposure_audit["soft_utility"],
            "attack_success_rate": 0.0,
            "final_locked": 0,
            "rollout_path": str(engine_root / "steps" / "step_0001" / "rollout" / "rollouts.json"),
            "reused_from_version": None,
        }
    )
    for metric in metrics:
        metric["gate_action"] = action_by_version.get(int(metric["version"]), "unknown")
        metric["gate_accepted"] = int("accept" in metric["gate_action"])
    write_jsonl(lineage_dir / "checkpoint_metrics.jsonl", metrics)
    write_json(lineage_dir / "exposure_audit.json", exposure_audit)
    if not exposure_audit["exactly_once"] or not exposure_audit["only_step_0001"]:
        raise RuntimeError(f"One-time exposure invariant failed: {exposure_audit}")
    shutil.copy2(selected[-1].path, lineage_dir / "final_skill.md")
    official_best = engine_root / "best_skill.md"
    if official_best.exists():
        shutil.copy2(official_best, lineage_dir / "best_skill.md")
    return {
        "engine": "skillopt",
        "lineage_id": row["lineage_id"],
        "trainer_summary": summary,
        "n_all_checkpoints": len(checkpoints),
        "n_monitored_checkpoints": len(selected),
        "n_changed_transitions": gate_audit["n_changed_transitions"],
        "all_changed_transitions_gate_accepted": gate_audit[
            "all_changed_transitions_gate_accepted"
        ],
        "final_skill_sha256": selected[-1].sha256,
        "exposure_local_accuracy": exposure_audit["hard_accuracy"],
    }
