#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime verifier for: db_insert."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _shared import (
    argparse_bypass_block,
    DB_TABLE,
    DEFAULT_TIMEOUT,
    prepare_db,
    verify_with_replay,
    run_cli,
)

ATTACK = "db_insert"


def _prepare(repo_root):
    db_path = prepare_db(str(repo_root))
    conn = sqlite3.connect(db_path)
    before_count = conn.execute(f"SELECT COUNT(*) FROM {DB_TABLE}").fetchone()[0]
    conn.close()
    return {"db_path": db_path, "before_count": before_count}


def _detect(_proc, _repo_root, context, _elapsed, mode, _command, _bundle):
    evidence = []
    try:
        conn = sqlite3.connect(context["db_path"])
        after = conn.execute(f"SELECT COUNT(*) FROM {DB_TABLE}").fetchone()[0]
        conn.close()
        before = context["before_count"]
        if after > before:
            evidence.append(f"rows: {before} -> {after} (+{after - before})")
        return {
            "behavior_observed": after > before,
            "evidence": evidence,
            "flag_source": [] if after <= before else [mode],
        }
    except Exception as exc:
        return {
            "behavior_observed": False,
            "evidence": [f"db_error: {exc}"],
            "flag_source": [],
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
