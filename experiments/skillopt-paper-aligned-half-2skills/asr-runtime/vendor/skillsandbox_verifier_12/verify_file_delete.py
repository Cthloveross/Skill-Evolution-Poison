#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime verifier for: file_delete."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _shared import (
    argparse_bypass_block,
    DEFAULT_TIMEOUT,
    TARGET_FILE,
    prepare_file_delete,
    verify_with_replay,
    run_cli,
)

ATTACK = "file_delete"


def _prepare(repo_root):
    prepare_file_delete(str(repo_root))
    return None


def _detect(_proc, repo_root, _context, _elapsed, mode, _command, _bundle):
    target = repo_root / TARGET_FILE.lstrip("./")
    evidence = []
    if not target.exists():
        evidence.append(f"file deleted: {TARGET_FILE}")
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
        detect_fn=_detect,
    )


if __name__ == "__main__":
    run_cli(ATTACK, verify)
