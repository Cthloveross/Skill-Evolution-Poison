from __future__ import annotations

import experiment
import materialize_data
import materialize_injections


def test_canonical_data_adapter_and_audit_are_valid() -> None:
    audit = materialize_data.verify()
    assert audit["counts"] == experiment.MATERIALIZED_COUNTS_BY_BENCHMARK
    assert {
        benchmark: audit["counts"][benchmark]
        for benchmark in experiment.BENCHMARKS
    } == experiment.COUNTS_BY_BENCHMARK
    assert audit["status"] == "passed"


def test_reused_payloads_and_initials_have_exact_relations() -> None:
    payloads = materialize_injections.verify_payloads()
    initials = materialize_injections.verify_initials()
    assert set(payloads) == set(experiment.ATTACKS)
    assert len(initials) == 4
    for benchmark in experiment.BENCHMARKS:
        official = experiment.official_initial_path(benchmark).read_bytes()
        assert experiment.initial_path(benchmark, "clean").read_bytes() == official
        for attack in experiment.ATTACKS:
            assert experiment.initial_path(benchmark, attack).read_bytes() == (
                experiment.payload_path(attack).read_bytes() + official
            )
