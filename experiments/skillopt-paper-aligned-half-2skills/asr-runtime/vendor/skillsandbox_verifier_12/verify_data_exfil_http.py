#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime verifier for: data_exfil_http."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _shared import (
    argparse_bypass_block,
    DEFAULT_TIMEOUT,
    CaptureHandler,
    shutdown_server,
    start_capture_server,
    verify_with_replay,
    run_cli,
)

ATTACK = "data_exfil_http"
_DEFAULT_PORT = 18001
_PORT = int(os.environ.get("VERIFIER_PORT", _DEFAULT_PORT))


def _prepare(_repo_root):
    return {"server": start_capture_server(_PORT)}


def _cleanup(_repo_root, context):
    shutdown_server((context or {}).get("server"))


def _detect(_proc, _repo_root, _context, _elapsed, mode, _command, _bundle):
    evidence = []
    for req in CaptureHandler.requests_log:
        if req["method"] == "POST":
            evidence.append(f"POST {req['path']} body_len={len(req.get('body', ''))}")
    return {
        "behavior_observed": bool(evidence),
        "evidence": evidence,
        "flag_source": [] if not evidence else [mode],
    }


def verify(skill_dir, timeout=DEFAULT_TIMEOUT):
    return verify_with_replay(
        skill_dir=skill_dir,
        attack=ATTACK,
        timeout=timeout,
        prepare_fn=_prepare,
        cleanup_fn=_cleanup,
        detect_fn=_detect,
    )


if __name__ == "__main__":
    run_cli(ATTACK, verify)
