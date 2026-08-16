from __future__ import annotations

import argparse
import json
import sys

from evoincubation.config import load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evoincubation",
        description="Evolution-conditioned SkillOpt poisoning experiment harness",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="materialize randomized design and data")
    prepare.add_argument("--config", required=True)

    validate = subparsers.add_parser("validate", help="validate design and split integrity")
    validate.add_argument("--config", required=True)

    run_lineage = subparsers.add_parser("run-lineage", help="run one randomized lineage")
    run_lineage.add_argument("--config", required=True)
    choice = run_lineage.add_mutually_exclusive_group(required=True)
    choice.add_argument("--lineage-id")
    choice.add_argument("--index", type=int, help="zero-based design row index")
    run_lineage.add_argument("--force", action="store_true")

    run_block = subparsers.add_parser("run-block", help="run all four arms of one block")
    run_block.add_argument("--config", required=True)
    block_choice = run_block.add_mutually_exclusive_group(required=True)
    block_choice.add_argument("--block-id")
    block_choice.add_argument("--index", type=int, help="zero-based block index")
    run_block.add_argument("--force", action="store_true")

    aggregate = subparsers.add_parser("aggregate", help="aggregate at the lineage/block level")
    aggregate.add_argument("--config", required=True)

    list_cmd = subparsers.add_parser("list", help="list randomized lineages and blocks")
    list_cmd.add_argument("--config", required=True)

    generate = subparsers.add_parser(
        "generate-seeds", help="generate safe canary seed/placebo pairs with an external model"
    )
    generate.add_argument("--n", type=int, default=8)
    generate.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "generate-seeds":
        from pathlib import Path

        from evoincubation.attack_search import generate_candidates

        candidates = generate_candidates(n=args.n, output=Path(args.output).expanduser().resolve())
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
        return
    config = load_config(args.config)
    if args.command == "prepare":
        from evoincubation.runner import prepare_experiment

        path = prepare_experiment(config)
        print(path)
        return
    if args.command == "validate":
        from evoincubation.runner import prepare_experiment, validate_prepared_experiment

        prepare_experiment(config)
        report = validate_prepared_experiment(config)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            raise SystemExit(2)
        return
    if args.command == "run-lineage":
        from evoincubation.runner import run_lineage, select_lineage

        row = select_lineage(config, lineage_id=args.lineage_id, index=args.index)
        print(run_lineage(config, row, force=args.force))
        return
    if args.command == "run-block":
        from evoincubation.runner import block_ids, run_block

        blocks = block_ids(config)
        block_id = args.block_id
        if args.index is not None:
            if args.index < 0 or args.index >= len(blocks):
                raise IndexError(f"Block index {args.index} outside [0, {len(blocks) - 1}]")
            block_id = blocks[args.index]
        assert block_id is not None
        run_block(config, block_id, force=args.force)
        return
    if args.command == "aggregate":
        from evoincubation.metrics import aggregate_experiment

        result = aggregate_experiment(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "list":
        from evoincubation.runner import block_ids, load_design_rows

        payload = {"blocks": block_ids(config), "lineages": load_design_rows(config)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    main(sys.argv[1:])
