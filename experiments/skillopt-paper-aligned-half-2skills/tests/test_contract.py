from __future__ import annotations

import importlib.metadata

import yaml

import experiment
import qualify_endpoint
import serve_endpoint


def test_frozen_training_and_reporting_scope() -> None:
    assert experiment.COUNTS_BY_BENCHMARK == {
        "searchqa": {"train": 200, "val": 100, "test": 700},
    }
    assert experiment.BATCH_SIZE_BY_BENCHMARK == {"searchqa": 40}
    assert experiment.EPOCHS == 4
    assert experiment.STEPS_PER_EPOCH == {"searchqa": 5}
    assert experiment.TOTAL_STEPS == {"searchqa": 20}
    index = experiment.load_run_index()
    assert index["total_trajectories"] == 4
    assert index["no_skill_evaluation"] is False
    assert {row["condition"] for row in index["runs"]} == {"clean", "attacked"}


def test_configs_lock_corrected_protocol() -> None:
    for benchmark in experiment.BENCHMARKS:
        config = yaml.safe_load(experiment.config_path(benchmark).read_text())
        assert config["model"]["optimizer"] == experiment.OPTIMIZER_MODEL_ALIAS
        assert config["model"]["target"] == experiment.TARGET_MODEL_ALIAS
        assert config["model"]["reasoning_effort"] == "medium"
        assert config["model"]["qwen_chat_enable_thinking"] is True
        assert config["model"]["optimizer_qwen_chat_enable_thinking"] is True
        assert config["model"]["target_qwen_chat_enable_thinking"] is True
        assert config["model"]["optimizer_qwen_chat_max_tokens"] == 16_384
        assert config["model"]["target_qwen_chat_max_tokens"] == 16_384
        assert config["env"]["max_completion_tokens"] == 16_384
        assert config["optimizer"]["slow_update_gate_with_selection"] is True
        assert config["train"]["batch_size"] == 40
        assert config["train"]["num_epochs"] == 4
        assert config["env"]["workers"] == experiment.ROLLOUT_WORKERS == 4
        assert config["gradient"]["analyst_workers"] == experiment.ANALYST_WORKERS == 1


def test_six_endpoint_topology_uses_exactly_eight_gpus() -> None:
    assert len(experiment.OPTIMIZER_ENDPOINT_SPECS) == 2
    assert len(experiment.TARGET_ENDPOINT_SPECS) == 4
    assert sorted(gpu for spec in experiment.ALL_ENDPOINT_SPECS for gpu in spec.gpu_ids) == list(range(8))
    assert experiment.TARGET_TO_OPTIMIZER_PORT == {
        19351: 19380,
        19352: 19380,
        19353: 19381,
        19354: 19381,
    }
    for spec in experiment.ALL_ENDPOINT_SPECS:
        expected_context = 131_072 if spec.role == "optimizer" else 65_536
        assert spec.serving_contract["max_model_len"] == expected_context
        assert spec.serving_contract["enable_thinking"] is True
        receipt = serve_endpoint.launch_contract(spec=spec)
        assert receipt["serving_contract"] == spec.serving_contract
        template_kwargs = receipt["command"][
            receipt["command"].index("--default-chat-template-kwargs") + 1
        ]
        if spec.role == "optimizer":
            assert template_kwargs == '{"enable_thinking":true,"reasoning_effort":"medium"}'
        else:
            assert template_kwargs == '{"enable_thinking":true}'
    for spec in experiment.TARGET_ENDPOINT_SPECS:
        assert spec.serving_contract["max_num_seqs"] == experiment.ROLLOUT_WORKERS


def test_json_repair_runtime_is_exactly_pinned() -> None:
    assert importlib.metadata.version("json-repair") == "0.63.4"


def test_endpoint_qualification_covers_each_role_contract() -> None:
    target = qualify_endpoint.qualification_profile("target")
    optimizer = qualify_endpoint.qualification_profile("optimizer")
    assert target == {
        "prompt_tokens": 40_000,
        "max_tokens": 2_048,
        "expected_answer": "CTX65_OK",
        "scope": "role_bound_65k_thinking_qualification",
    }
    assert optimizer == {
        "prompt_tokens": 96_000,
        "max_tokens": 16_384,
        "expected_answer": "CTX131_OK",
        "scope": "role_bound_131k_thinking_qualification",
    }
    assert int(optimizer["prompt_tokens"]) + int(optimizer["max_tokens"]) < 131_072
    assert int(optimizer["prompt_tokens"]) > 49_385


def test_launch_contract_binds_patched_skillopt_runtime() -> None:
    run = experiment.load_run_index()["runs"][0]
    contract = experiment.stable_launch_contract(run)
    assert contract["schema_version"] == 4
    assert contract["skillopt_runtime"] == experiment.skillopt_runtime_contract()
    runtime_files = contract["skillopt_runtime"]["runtime_files"]
    assert runtime_files == [
        {
            "path": "skillopt/engine/trainer.py",
            "sha256": experiment.sha256_file(
                experiment.OFFICIAL_ROOT / "skillopt/engine/trainer.py"
            ),
        }
    ]
    phases = contract["execution_phases"]["phases"]
    assert [(row["first_step"], row["last_step"]) for row in phases] == [
        (1, 7),
        (8, 20),
    ]
    assert phases[0]["optimizer_serving_contract"]["max_model_len"] == 65_536
    assert phases[1]["optimizer_serving_contract"]["max_model_len"] == 131_072



def test_scope_reduction_excludes_unlaunched_docvqa() -> None:
    record = experiment.read_json(experiment.SCOPE_REDUCTION_RECORD)
    assert record["status"] == "applied_before_resume"
    assert record["active_scope"]["benchmarks"] == ["searchqa"]
    assert record["removed_scope"]["benchmark"] == "docvqa"
    assert record["removed_scope"]["launch_status"] == "never_launched"
    assert record["removed_scope"]["checkpoint_count"] == 0
    assert experiment.sha256_file(experiment.SCOPE_REDUCTION_RECORD) == (
        experiment.load_run_index()["scope_revision"]["record_sha256"]
    )
