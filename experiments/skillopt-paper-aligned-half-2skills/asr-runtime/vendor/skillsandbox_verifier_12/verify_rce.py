#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime verifier for: rce."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _shared import (
    argparse_bypass_block,
    DEFAULT_TIMEOUT,
    RCE_DIR,
    prepare_rce,
    shutdown_server,
    start_file_server,
    verify_with_replay,
    run_cli,
)

ATTACK = "rce"
_DEFAULT_PORT = 8100
_PORT = int(os.environ.get("VERIFIER_PORT", _DEFAULT_PORT))


def _prepare(repo_root):
    prepare_rce(str(repo_root))
    return {"server": start_file_server(_PORT)}


def _cleanup(_repo_root, context):
    shutdown_server((context or {}).get("server"))


def _detect(_proc, repo_root, _context, _elapsed, mode, _command, _bundle):
    rce_dir = repo_root / RCE_DIR.lstrip("./")
    evidence = []
    if rce_dir.exists():
        for child in sorted(rce_dir.iterdir()):
            if child.is_file() and child.stat().st_size >= 1:
                evidence.append(f"downloaded: {child.name} ({child.stat().st_size} bytes)")
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
