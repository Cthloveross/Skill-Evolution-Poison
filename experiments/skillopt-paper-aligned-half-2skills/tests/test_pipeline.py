from __future__ import annotations

import argparse

import pytest

import experiment
import run_asr_probe
import run_matrix
import run_pipeline
import serve_endpoint


def option_values(command: list[str], option: str) -> list[str]:
    return [command[index + 1] for index, value in enumerate(command) if value == option]


def test_pipeline_dry_run_has_new_roles_and_no_no_skill() -> None:
    args = run_pipeline.parse_args(["--dry-run"])
    plan = run_pipeline.dry_run(experiment.load_run_index(), args)
    assert len(plan["servers"]) == 6
    assert [row["role"] for row in plan["servers"]].count("optimizer") == 2
    assert [row["role"] for row in plan["servers"]].count("target") == 4
    assert plan["trajectory_count"] == 4
    assert plan["asr_probe_count"] == 9
    assert "no-skill" not in str(plan).lower()
    trajectory = plan["stages"]["trajectories"]
    assert option_values(trajectory, "--cell-timeout-seconds") == ["172800"]
    assert option_values(trajectory, "--target-endpoint") == [
        spec.endpoint for spec in experiment.TARGET_ENDPOINT_SPECS
    ]
    assert option_values(plan["stages"]["asr"], "--endpoint") == [
        spec.endpoint for spec in experiment.TARGET_ENDPOINT_SPECS
    ]


def test_condition_routing_assigns_one_search_per_target() -> None:
    endpoints = [spec.endpoint for spec in experiment.TARGET_ENDPOINT_SPECS]
    assignments: dict[str, list[str]] = {endpoint: [] for endpoint in endpoints}
    for run in experiment.load_run_index()["runs"]:
        endpoint = run_matrix.target_endpoint_for_run(run, endpoints)
        assignments[endpoint].append(run["benchmark"])
    assert all(benchmarks == ["searchqa"] for benchmarks in assignments.values())


def test_skillopt_command_routes_roles_separately() -> None:
    run = experiment.load_run_index()["runs"][0]
    target = experiment.TARGET_ENDPOINT_SPECS[0].endpoint
    optimizer = experiment.optimizer_endpoint_for_target(
        experiment.TARGET_ENDPOINT_SPECS[0].port
    )
    command = run_matrix.build_command(run, target, optimizer)
    assert f"model.target_qwen_chat_base_url={target}" in command
    assert f"model.optimizer_qwen_chat_base_url={optimizer}" in command
    assert target != optimizer


def test_runtime_state_is_scoped_to_current_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    runtime_state = tmp_path / "runtime-state.json"
    pipeline_state = tmp_path / "pipeline-state.json"
    experiment.atomic_write_json(
        pipeline_state,
        {"started_at": "2026-08-27T04:32:56+00:00"},
    )
    experiment.atomic_write_json(
        runtime_state,
        {
            "schema_version": 1,
            "created_at": "old",
            "runs": {"stale": {"status": "failed"}},
        },
    )
    monkeypatch.setattr(run_matrix, "RUNTIME_STATE", runtime_state)
    monkeypatch.setattr(run_matrix, "PIPELINE_STATE", pipeline_state)

    state = run_matrix.load_state()

    assert state["schema_version"] == 2
    assert state["pipeline_started_at"] == "2026-08-27T04:32:56+00:00"
    assert state["runs"] == {}


def test_asr_rejects_optimizer_ports_and_uses_thinking_target_command() -> None:
    with pytest.raises(run_asr_probe.ProbeError):
        run_asr_probe.normalize_endpoint(experiment.OPTIMIZER_ENDPOINT_SPECS[0].endpoint)
    command = run_asr_probe.expected_server_command(
        experiment.TARGET_ENDPOINT_SPECS[0].port
    )
    assert '{"enable_thinking":true}' in command
    assert experiment.TARGET_MODEL_ALIAS in command


def test_asr_accepts_new_target_launch_and_qualification_schemas() -> None:
    spec = experiment.TARGET_ENDPOINT_SPECS[0]
    launch = serve_endpoint.launch_contract(spec=spec)
    assert run_asr_probe._validate_endpoint_launch_snapshot(
        launch, spec.endpoint
    ) == spec.gpu_ids[0]
    qualification = {
        "schema_version": 3,
        "status": "passed",
        "scope": "role_bound_65k_thinking_qualification",
        "role": "target",
        "endpoint": spec.endpoint,
        "gpu_ids": list(spec.gpu_ids),
        "model_identity": experiment.target_model_identity_contract(),
        "serving_contract": experiment.TARGET_SERVING_CONTRACT,
        "long_request": {
            "answer": "CTX65_OK",
            "local_prompt_tokens": 40_000,
            "requested_output_tokens": 2_048,
            "enable_thinking": True,
        },
    }
    run_asr_probe._validate_qualification_snapshot(qualification)
