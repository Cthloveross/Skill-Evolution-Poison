from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

pytest.importorskip("skillopt")

from evoincubation.config import load_config
from evoincubation.runner import load_design_rows, prepare_experiment, run_lineage

TOKEN_RE = re.compile(r"(?:BACKUP|CACHE|RECOMPUTED)_(?:RESULT|VALUE):\s*([A-Z]+-\d+)")


def _target_response(user: str) -> str:
    tokens = TOKEN_RE.findall(user)
    if tokens:
        return json.dumps({"action": "submit", "answer": tokens[-1]})
    if "Budget: 2 cost units" in user:
        return '{"action":"verify_cache"}'
    if "Backup status: SAFE_BACKUP" in user or "Backup status: INDEPENDENT_BACKUP" in user:
        return '{"action":"read_backup"}'
    return '{"action":"read_cache"}'


def _optimizer_response(system: str) -> str:
    if "success-pattern analyst" in system:
        return json.dumps(
            {
                "batch_size": 2,
                "success_patterns": [],
                "patch": {
                    "reasoning": "No supported change in fake integration test.",
                    "edits": [],
                },
            }
        )
    if "failure-analysis agent" in system:
        return json.dumps(
            {
                "batch_size": 2,
                "failure_summary": [],
                "patch": {
                    "reasoning": "No supported change in fake integration test.",
                    "edits": [],
                },
            }
        )
    return json.dumps({"reasoning": "No change.", "edits": []})


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size))
        messages = payload.get("messages", [])
        system = str(messages[0].get("content", "")) if messages else ""
        user = str(messages[-1].get("content", "")) if messages else ""
        if "deterministic research canary" in system:
            content = _target_response(user)
        else:
            content = _optimizer_response(system)
        response = {
            "id": "chatcmpl-integration",
            "object": "chat.completion",
            "created": 0,
            "model": payload.get("model", "fake"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        del format, args


def test_official_trainer_adapter_and_lineage_audits(tmp_path: Path) -> None:
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from skillopt.model import configure_openai_compatible

        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        configure_openai_compatible(
            base_url=base_url,
            api_key="dummy",
            model="fake",
            temperature=0,
            optimizer_base_url=base_url,
            optimizer_api_key="dummy",
            optimizer_model="fake",
            target_base_url=base_url,
            target_api_key="dummy",
            target_model="fake",
        )

        source = Path(__file__).parents[1] / "configs" / "pilot_skillopt.yaml"
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        raw["experiment"]["output_root"] = str(tmp_path / "run")
        raw["experiment"]["replicates"] = 1
        raw["canary"].update(
            {
                "train_items": 4,
                "selection_items": 2,
                "clean_test_items": 2,
                "trigger_monitor_items": 2,
                "final_trigger_items": 2,
                "near_trigger_items": 2,
            }
        )
        raw["evolution"].update(
            {
                "epochs": 2,
                "washout_epochs": 1,
                "batch_size": 4,
                "rollout_workers": 1,
                "analyst_workers": 1,
            }
        )
        raw["model"].update({"optimizer": "fake", "target": "fake"})
        raw["skillopt"]["allow_unverified_revision"] = True
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        config = load_config(path)
        prepare_experiment(config)
        row = next(
            row
            for row in load_design_rows(config)
            if row["seed_present"] and row["evolution_enabled"]
        )
        lineage_dir = run_lineage(config, row)
        complete = json.loads((lineage_dir / "COMPLETE.json").read_text(encoding="utf-8"))
        exposure = json.loads((lineage_dir / "exposure_audit.json").read_text(encoding="utf-8"))
        gate = json.loads((lineage_dir / "gate_audit.json").read_text(encoding="utf-8"))
        assert complete["status"] == "complete"
        assert exposure["actual_training_exposure_count"] == 2
        assert exposure["only_step_0001"]
        assert gate["all_changed_transitions_gate_accepted"]
        assert (lineage_dir / "checkpoint_metrics.jsonl").exists()
        assert (lineage_dir / "final_skill.md").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
