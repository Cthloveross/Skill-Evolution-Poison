#!/usr/bin/env python3
"""Stage 7 v3 adapter around the frozen SkillSandbox pipeline."""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable

from stage7_v3_behavior_verifier import run_behavior_verifier

D3_CONTROL_SCHEMA_VERSION = 1
D3_REPORT_SCHEMA_VERSION = 1
STAGING_ROOT = Path("/output/staging")
VALID_DEFENSES = {"none", "d1", "d3", "d1+d3"}
FROZEN_GENERATION_CONFIG = {
    "temperature": 0.0,
    "seed": 0,
    "max_output_tokens": 4096,
    "num_retries": 1,
}


class D3EnforcementError(RuntimeError):
    """The kernel read-only D3 contract was not established."""


class WorkspacePermissionError(RuntimeError):
    """The copied skill cannot be safely normalized inside staging."""


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise D3EnforcementError(f"invalid {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise D3EnforcementError(f"{label} is not a JSON object")
    return value


def _request_read_only_mount(skill_root: Path) -> dict[str, Any]:
    control_dir = Path(os.environ.get("DYMAL_D3_CONTROL_DIR", ""))
    nonce = os.environ.get("DYMAL_D3_NONCE", "")
    expected_relative = os.environ.get("DYMAL_D3_EXPECTED_RELATIVE", "")
    if not nonce or not expected_relative or not control_dir.is_dir():
        raise D3EnforcementError("D3 mount-controller environment is incomplete")
    staging_root = Path("/output/staging").resolve(strict=True)
    resolved_skill_root = skill_root.resolve(strict=True)
    try:
        relative = resolved_skill_root.relative_to(staging_root).as_posix()
    except ValueError as exc:
        raise D3EnforcementError("D3 skill path escapes staging") from exc
    if relative != expected_relative or skill_root.is_symlink():
        raise D3EnforcementError("D3 skill path differs from the assigned path")
    request = {
        "schema_version": D3_CONTROL_SCHEMA_VERSION,
        "nonce": nonce,
        "relative_path": relative,
        "pid": os.getpid(),
    }
    request_path = control_dir / "request.json"
    if request_path.exists():
        raise D3EnforcementError("D3 mount request already exists")
    _atomic_write_json(request_path, request)
    deadline = time.monotonic() + float(os.environ.get("DYMAL_D3_HANDSHAKE_TIMEOUT_SECONDS", "30"))
    ack_path = control_dir / "ack.json"
    while time.monotonic() < deadline:
        if ack_path.is_file():
            ack = _read_json_object(ack_path, "D3 mount acknowledgement")
            expected = {
                "schema_version": D3_CONTROL_SCHEMA_VERSION,
                "nonce": nonce,
                "relative_path": relative,
                "status": "mounted_read_only",
            }
            if any(ack.get(key) != value for key, value in expected.items()):
                raise D3EnforcementError("D3 mount acknowledgement identity mismatch")
            return ack
        time.sleep(0.025)
    raise D3EnforcementError("D3 mount-controller acknowledgement timed out")


def _probe_oserror(
    action: Callable[[], None], restore: Callable[[], None] | None = None
) -> dict[str, Any]:
    try:
        action()
    except OSError as exc:
        return {
            "passed": exc.errno == errno.EROFS,
            "errno": exc.errno,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if restore is not None:
        try:
            restore()
        except OSError:
            pass
    return {"passed": False, "errno": None, "error": "mutation unexpectedly succeeded"}


def _file_editor_probe(skill_root: Path) -> dict[str, Any]:
    probe_path = skill_root / ".stage7_d3_file_editor_probe"
    try:
        from openhands.tools.file_editor.definition import FileEditorAction
        from openhands.tools.file_editor.impl import FileEditorExecutor

        executor = FileEditorExecutor(workspace_root=str(skill_root))
        observation = executor(
            FileEditorAction(
                command="create",
                path=str(probe_path),
                file_text="D3 violation\n",
            )
        )
        passed = bool(getattr(observation, "is_error", False)) and not probe_path.exists()
        return {
            "passed": passed,
            "is_error": bool(getattr(observation, "is_error", False)),
            "probe_file_absent": not probe_path.exists(),
            "observation_sha256": hashlib.sha256(
                str(observation).encode("utf-8", errors="replace")
            ).hexdigest(),
        }
    except Exception as exc:
        return {
            "passed": False,
            "is_error": None,
            "probe_file_absent": not probe_path.exists(),
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass


def run_d3_negative_probes(skill_root: Path, ack: dict[str, Any]) -> dict[str, Any]:
    target = skill_root / "SKILL.md"
    if target.is_symlink() or not target.is_file():
        candidates = sorted(
            path for path in skill_root.rglob("*") if path.is_file() and not path.is_symlink()
        )
        if not candidates:
            raise D3EnforcementError("D3 skill tree has no regular probe target")
        target = candidates[0]
    original_bytes = target.read_bytes()
    original_mode = stat.S_IMODE(target.stat().st_mode)
    renamed = target.with_name(f".{target.name}.stage7-d3-rename")

    def restore_write() -> None:
        target.write_bytes(original_bytes)

    def append_violation() -> None:
        with target.open("ab") as stream:
            stream.write(b"D3 violation\n")

    def restore_chmod() -> None:
        target.chmod(original_mode)

    def restore_rename() -> None:
        if renamed.exists() and not target.exists():
            renamed.rename(target)

    def restore_unlink() -> None:
        if not target.exists():
            target.write_bytes(original_bytes)
            target.chmod(original_mode)

    checks = {
        "mount_flag_read_only": {
            "passed": bool(os.statvfs(skill_root).f_flag & os.ST_RDONLY),
        },
        "write_denied": _probe_oserror(append_violation, restore_write),
        "rename_denied": _probe_oserror(lambda: target.rename(renamed), restore_rename),
        "unlink_denied": _probe_oserror(target.unlink, restore_unlink),
        "chmod_denied": _probe_oserror(
            lambda: target.chmod(original_mode ^ stat.S_IWUSR), restore_chmod
        ),
        "file_editor_denied": _file_editor_probe(skill_root),
    }
    passed = all(record.get("passed") is True for record in checks.values())
    report = {
        "schema_version": D3_REPORT_SCHEMA_VERSION,
        "enforcement": "linux_bind_remount_read_only",
        "skill_root_relative": os.environ.get("DYMAL_D3_EXPECTED_RELATIVE"),
        "mount_controller": {
            "schema_version": ack.get("schema_version"),
            "status": ack.get("status"),
            "mount_options": ack.get("mount_options"),
        },
        "checks": checks,
        "passed": passed,
    }
    report_path = Path("/output/staging/d3_preflight.json")
    _atomic_write_json(report_path, report)
    if not passed:
        raise D3EnforcementError("one or more D3 mutation-denial probes failed")
    return report


def enforce_d3(skill_root: Path) -> dict[str, Any]:
    ack = _request_read_only_mount(skill_root)
    return run_d3_negative_probes(skill_root, ack)


def normalize_workspace_skill_permissions(skills_home: Path, skill_ids: list[str]) -> None:
    """Make only copied staging skills owner-writable before applying any defense."""
    if STAGING_ROOT.is_symlink():
        raise WorkspacePermissionError("staging root must not be a symlink")
    try:
        staging_root = STAGING_ROOT.resolve(strict=True)
    except OSError as exc:
        raise WorkspacePermissionError("staging root is missing or invalid") from exc

    lexical_staging_root = STAGING_ROOT.absolute()
    for skill_id in skill_ids:
        if Path(skill_id).parts != (skill_id,) or skill_id in {"", ".", ".."}:
            raise WorkspacePermissionError(f"invalid copied skill id: {skill_id!r}")
        skill_root = skills_home / skill_id
        lexical_skill_root = skill_root.absolute()
        try:
            relative_parts = lexical_skill_root.relative_to(lexical_staging_root).parts
            resolved_skill_root = skill_root.resolve(strict=True)
            resolved_skill_root.relative_to(staging_root)
        except (OSError, ValueError) as exc:
            raise WorkspacePermissionError(
                f"copied skill escapes staging: {skill_id}"
            ) from exc

        current = lexical_staging_root
        for part in relative_parts:
            current /= part
            if current.is_symlink():
                raise WorkspacePermissionError(
                    f"copied skill path contains a symlink: {skill_id}"
                )

        entries = [skill_root, *skill_root.rglob("*")]
        for path in entries:
            if path.is_symlink():
                raise WorkspacePermissionError(
                    f"copied skill tree contains a symlink: {skill_id}"
                )
            try:
                mode = path.lstat().st_mode
                path.resolve(strict=True).relative_to(resolved_skill_root)
            except (OSError, ValueError) as exc:
                raise WorkspacePermissionError(
                    f"copied skill tree is invalid: {skill_id}"
                ) from exc
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise WorkspacePermissionError(
                    f"copied skill tree contains a special file: {skill_id}"
                )

        for path in entries:
            mode = path.lstat().st_mode
            path.chmod(0o755 if stat.S_ISDIR(mode) else 0o644)


def _load_original_pipeline() -> Any:
    root = Path(os.environ.get("DYMAL_SKILLSANDBOX_MOUNT", "/opt/skillsandbox"))
    if root.is_symlink() or not (root / "pipeline.py").is_file():
        raise RuntimeError("frozen SkillSandbox evaluator mount is missing")
    sys.path.insert(0, str(root))
    return importlib.import_module("pipeline")


def _write_augmented_result(
    *,
    pipeline: Any,
    result: dict[str, Any],
    trial_root: Path,
) -> None:
    result_path = trial_root / "result.json"
    retryable_path = trial_root / "_retryable_error.json"
    if result.get("retryable") is True and not result_path.exists():
        pipeline.write_json(retryable_path, result)
    else:
        pipeline.write_json(result_path, result)


def install_v3_hooks(pipeline: Any) -> None:
    requested_generation_config = {
        "temperature": float(os.environ.get("DYMAL_TEMPERATURE", "nan")),
        "seed": int(os.environ.get("DYMAL_GENERATION_SEED", "-1")),
        "max_output_tokens": int(os.environ.get("DYMAL_MAX_OUTPUT_TOKENS", "-1")),
        "num_retries": int(os.environ.get("DYMAL_NUM_RETRIES", "-1")),
    }
    if requested_generation_config != FROZEN_GENERATION_CONFIG:
        raise RuntimeError(
            "runner generation settings differ from the frozen protocol: "
            f"{requested_generation_config!r}"
        )
    original_copy = pipeline.copy_skill_corpus
    original_load_sdk = pipeline.load_openhands_sdk
    original_run_trial = pipeline.run_trial
    d3_reports: dict[str, dict[str, Any]] = {}
    generation_observations: list[dict[str, Any]] = []

    def load_openhands_sdk_v3() -> dict[str, Any]:
        sdk = original_load_sdk()
        original_llm = sdk["LLM"]

        def frozen_llm(*args: Any, **kwargs: Any) -> Any:
            kwargs.update(FROZEN_GENERATION_CONFIG)
            llm = original_llm(*args, **kwargs)
            observed = {
                key: getattr(llm, key, None) for key in FROZEN_GENERATION_CONFIG
            }
            generation_observations.append(observed)
            if observed != FROZEN_GENERATION_CONFIG:
                raise RuntimeError(
                    "OpenHands LLM generation settings differ from the frozen protocol: "
                    f"{observed!r}"
                )
            return llm

        sdk["LLM"] = frozen_llm
        return sdk

    def copy_skill_corpus_v3(attack_dir: Path, skills_home: Path, skill_ids: list[str]) -> None:
        defense = os.environ.get("DYMAL_DEFENSE")
        if defense not in VALID_DEFENSES:
            raise WorkspacePermissionError(f"invalid defense for copied skill: {defense!r}")
        original_copy(attack_dir, skills_home, skill_ids)
        normalize_workspace_skill_permissions(skills_home, skill_ids)
        if defense in {"d3", "d1+d3"}:
            if len(skill_ids) != 1:
                raise D3EnforcementError("D3 requires exactly one assigned skill")
            d3_reports[skill_ids[0]] = enforce_d3(skills_home / skill_ids[0])

    def restore_skill_writable_v3(_skills_home: Path, _skill_ids: list[str]) -> None:
        # The mount-controller owns the read-only bind mount and unmounts it only
        # after the capability-dropped runner exits.
        return None

    def run_trial_v3(*args: Any, **kwargs: Any) -> dict[str, Any]:
        generation_observations.clear()
        result = original_run_trial(*args, **kwargs)
        attack = kwargs["attack_name"]
        prompt_record = kwargs["prompt_record"]
        skill_id = prompt_record["skill_id"]
        run_root = Path(kwargs["run_root"])
        trial_root = run_root / attack / skill_id
        skill_root = trial_root / "workspace" / ".agents" / "skills" / skill_id
        behavior = run_behavior_verifier(
            attack=attack,
            skill_id=skill_id,
            skill_dir=skill_root,
            prompt_record=prompt_record,
            output_dir=trial_root,
            bundle_root=Path(os.environ.get("DYMAL_VERIFIER_ROOT", "/opt/verifier_12")),
            timeout_seconds=float(os.environ.get("DYMAL_VERIFIER_TIMEOUT_SECONDS", "30")),
        )
        result["behavior_verifier"] = behavior
        result["generation_config"] = (
            generation_observations[-1] if len(generation_observations) == 1 else None
        )
        defense = kwargs.get("defense", "none")
        if defense in {"d3", "d1+d3"}:
            d3 = d3_reports.pop(skill_id, None)
            if d3 is None:
                d3 = {
                    "schema_version": D3_REPORT_SCHEMA_VERSION,
                    "passed": False,
                    "error": "D3 in-memory preflight report is missing",
                }
            else:
                _atomic_write_json(Path("/output/staging/d3_preflight.json"), d3)
            result["d3_enforcement"] = d3
        gate_errors = []
        if behavior.get("accepted") is not True:
            gate_errors.append("behavior_verifier_fail_closed")
        if result["generation_config"] != FROZEN_GENERATION_CONFIG:
            gate_errors.append("generation_config_not_frozen")
        if defense in {"d3", "d1+d3"} and result["d3_enforcement"].get("passed") is not True:
            gate_errors.append("d3_enforcement_failed")
        if gate_errors:
            result["pre_endpoint_gate_status"] = {
                "status": result.get("status"),
                "error": result.get("error"),
                "error_kind": result.get("error_kind"),
                "retryable": result.get("retryable"),
            }
            result["status"] = "error"
            result["error"] = "Stage7V3EndpointGateError: " + ",".join(gate_errors)
            result["error_kind"] = "stage7_v3_endpoint_gate"
            result["retryable"] = False
        _write_augmented_result(pipeline=pipeline, result=result, trial_root=trial_root)
        return result

    pipeline.copy_skill_corpus = copy_skill_corpus_v3
    pipeline.load_openhands_sdk = load_openhands_sdk_v3
    pipeline.make_skill_readonly = lambda _skills_home, _skill_ids: None
    pipeline.restore_skill_writable = restore_skill_writable_v3
    pipeline.run_trial = run_trial_v3


def main() -> int:
    pipeline = _load_original_pipeline()
    install_v3_hooks(pipeline)
    return int(pipeline.main())


if __name__ == "__main__":
    raise SystemExit(main())
