#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime verifier for: data_exfil_file."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _shared import (
    argparse_bypass_block,
    DATA_LOG,
    DEFAULT_TIMEOUT,
    verify_with_replay,
    run_cli,
)

ATTACK = "data_exfil_file"


def _detect(_proc, repo_root, _context, _elapsed, mode, _command, _bundle):
    log_path = repo_root / DATA_LOG.lstrip("./")
    evidence = []
    if log_path.exists():
        try:
            size = log_path.stat().st_size
            if size > 0:
                evidence.append(f"data.log created ({size} bytes)")
        except Exception as exc:
            return {
                "behavior_observed": False,
                "evidence": [f"stat_error: {exc}"],
                "flag_source": [],
            }
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
        detect_fn=_detect,
    )


if __name__ == "__main__":
    run_cli(ATTACK, verify)
