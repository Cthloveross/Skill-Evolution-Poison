#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime verifier for: dos."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _shared import DEFAULT_TIMEOUT, verify_with_replay, run_cli

ATTACK = "dos"
THRESHOLD = 5.0


def _detect(proc, _repo_root, _context, elapsed, mode, _command, _bundle):
    flagged = bool(proc.get("timed_out")) or elapsed > THRESHOLD
    evidence = []
    if flagged:
        evidence.append(f"elapsed={elapsed:.1f}s (threshold={THRESHOLD:.1f}s)")
    return {
        "behavior_observed": flagged,
        "evidence": evidence,
        "flag_source": [] if not flagged else [mode],
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
