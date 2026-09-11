import json
from dataclasses import asdict, replace
from unittest.mock import Mock, patch

import pytest

from blockscope.evaluation import (
    BlockEvaluationState,
    EvaluationConfiguration,
    EVMCounterfactualState,
    FailureKind,
    MathematicalState,
    ObservedReplayState,
    ProvenanceState,
    SoftwareMetadata,
    _candidate_record,
    _CountingCachingRPC,
    _empty_block_failure,
    aggregate_metrics,
    candidate_stage_selection,
    deterministic_candidate_order,
    run_evaluation,
    write_evaluation_artifact,
)
from blockscope.replay import ComparisonState
from blockscope.rpc import RPCError
from blockscope.types import Block


def fake_candidate(
    block_number: int,
    front_index: int,
    front_log: int,
    *,
    victim_indexes: tuple[int, ...] = (2,),
) -> Mock:
    candidate = Mock()
    candidate.pair_address = "0x" + f"{front_index + 1:040x}"
    candidate.front_run.swap.block_number = block_number
    candidate.front_run.swap.transaction_index = front_index
    candidate.front_run.swap.log_index = front_log
    candidate.front_run.swap.transaction_hash = f"0x{front_index + 1:064x}"
    victims = []
    for index in victim_indexes:
        victim = Mock()
        victim.swap.transaction_index = index
        victim.swap.transaction_hash = f"0x{index + 1:064x}"
        victims.append(victim)
    candidate.victims = tuple(victims)
    candidate.back_run.swap.transaction_index = max(victim_indexes) + 1
    candidate.back_run.swap.log_index = front_log + 2
    candidate.back_run.swap.transaction_hash = (
        f"0x{candidate.back_run.swap.transaction_index + 1:064x}"
    )
    return candidate


def candidate_record(ordinal: int, *, victims: tuple[int, ...] = (2,)):
    candidate = fake_candidate(100 + ordinal, 1, ordinal, victim_indexes=victims)
    return _candidate_record(
        ordinal,
        candidate,
        None,
        0.1,
        selected_for_candidate_analysis=True,
        selected_for_evm=ordinal <= 2,
    )


def block_record(number: int, pair: str):
    return replace(
        _empty_block_failure(number, 0.1, RPCError("unused"), "https://rpc.example"),
        state=BlockEvaluationState.COMPLETE,
        transaction_count=10,
        receipts_fetched=10,
        swap_count=3,
        unique_pair_count=1,
        swap_pair_addresses=(pair,),
        strict_candidates_found=1,
        receipt_v2_analysis_seconds=0.3,
        candidate_detection_seconds=0.01,
        failure_kind=None,
        failure_detail=None,
    )


@pytest.mark.parametrize(
    ("start", "end", "candidate_limit", "evm_limit", "message"),
    [
        (-1, 2, 1, 1, "non-negative"),
        (3, 2, 1, 1, "greater than or equal"),
        (1, 2, -1, 1, "candidate limit"),
        (1, 2, 1, -1, "EVM experiment limit"),
    ],
)
def test_range_and_limit_validation(
    start: int,
    end: int,
    candidate_limit: int,
    evm_limit: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        EvaluationConfiguration(start, end, candidate_limit, evm_limit)


def test_configuration_serializes_inclusive_range_and_selection_rule() -> None:
    configuration = EvaluationConfiguration(10, 12, 7, 3)

    assert configuration.block_count == 3
    assert configuration.start_block == 10
    assert "ascending block number" in configuration.selection_rule

    with pytest.raises(ValueError, match="cooldown"):
        EvaluationConfiguration(10, 12, 7, 3, -1)


def test_candidate_selection_uses_only_global_ordinal_and_both_caps() -> None:
    configuration = EvaluationConfiguration(1, 2, 2, 5)

    assert candidate_stage_selection(1, configuration) == (True, True)
    assert candidate_stage_selection(2, configuration) == (True, True)
    assert candidate_stage_selection(3, configuration) == (False, False)


def test_candidate_order_is_deterministic_and_outcome_independent() -> None:
    later_block = fake_candidate(101, 0, 0)
    later_tx = fake_candidate(100, 5, 0)
    later_log = fake_candidate(100, 1, 9)
    first = fake_candidate(100, 1, 2)

    ordered = deterministic_candidate_order((later_block, later_tx, later_log, first))

    assert ordered == (first, later_log, later_tx, later_block)


def test_evaluation_rpc_caches_stable_chain_id() -> None:
    rpc = Mock()
    rpc.get_chain_id.return_value = 1
    counted = _CountingCachingRPC(rpc)

    assert counted.get_chain_id() == 1
    assert counted.get_chain_id() == 1
    assert counted.counts().chain_ids == 1
    rpc.get_chain_id.assert_called_once_with()


def test_candidate_record_contains_only_serializable_evidence() -> None:
    record = candidate_record(1)

    payload = json.dumps(asdict(record))

    assert '"block_number": 101' in payload
    assert '"trace_attribution": "not_attempted"' in payload
    assert "Mock" not in payload


def test_aggregate_metrics_preserve_denominators_math_and_fixed_input_outcomes() -> None:
    first = replace(
        candidate_record(1),
        provenance=ProvenanceState.CANONICAL,
        mathematical_state=MathematicalState.AVAILABLE,
        observed_replay=ObservedReplayState.EXACT,
        evm_counterfactual=EVMCounterfactualState.RELIABLE,
        observed_victim_status=1,
        counterfactual_victim_status=1,
        pair_input_comparison=ComparisonState.MATCH,
        counterfactual_pair_swap_present=True,
        math_vs_evm_difference=0,
        evm_seconds=2.0,
    )
    second = replace(
        candidate_record(2),
        provenance=ProvenanceState.CANONICAL,
        mathematical_state=MathematicalState.AVAILABLE,
        observed_replay=ObservedReplayState.SEMANTIC_MISMATCH,
        evm_counterfactual=EVMCounterfactualState.UNRELIABLE,
        observed_victim_status=1,
        counterfactual_victim_status=0,
        pair_input_comparison=ComparisonState.DIFFER,
        counterfactual_pair_swap_present=False,
        math_vs_evm_difference=5,
        failure_kinds=(
            FailureKind.OBSERVED_REPLAY_MISMATCH,
            FailureKind.MODEL_MISMATCH,
        ),
        evm_seconds=4.0,
    )
    third = replace(
        candidate_record(3, victims=(2, 3)),
        selected_for_evm=False,
        provenance=ProvenanceState.NON_CANONICAL,
        mathematical_state=MathematicalState.UNAVAILABLE,
        failure_kinds=(FailureKind.NONCANONICAL_PAIR,),
    )

    metrics, failures, timings = aggregate_metrics(
        (
            block_record(100, "0x" + "aa" * 20),
            block_record(101, "0x" + "aa" * 20),
        ),
        (first, second, third),
    )

    assert metrics.blocks_inspected == 2
    assert metrics.transactions_inspected == 20
    assert metrics.receipts_fetched == 20
    assert metrics.v2_compatible_swaps == 6
    assert metrics.unique_pairs == 1
    assert metrics.strict_candidates == 3
    assert metrics.single_victim_candidates == 2
    assert metrics.multi_victim_candidates == 1
    assert metrics.observed_replay_exact == 1
    assert metrics.observed_replay_attempted == 2
    assert metrics.evm_counterfactual_reliable == 1
    assert metrics.target_success_to_revert == 1
    assert metrics.pair_input_unchanged == 1
    assert metrics.pair_input_changed == 1
    assert metrics.counterfactual_pair_swap_absent == 1
    assert metrics.math_evm_comparisons == 2
    assert metrics.math_evm_exact_matches == 1
    assert metrics.math_evm_nonzero_differences == 1
    assert {failure.kind: failure.count for failure in failures} == {
        FailureKind.MODEL_MISMATCH: 1,
        FailureKind.NONCANONICAL_PAIR: 1,
        FailureKind.OBSERVED_REPLAY_MISMATCH: 1,
    }
    evm_timing = next(
        item for item in timings if item.stage == "observed_and_counterfactual_evm_combined"
    )
    assert evm_timing.count == 2
    assert evm_timing.mean_seconds == 3.0
    assert evm_timing.median_seconds == 3.0


def test_partial_provider_failure_is_recorded_without_aborting_and_url_is_redacted(
    tmp_path,
) -> None:
    secret_url = "https://user:secret@rpc.example/private-key"
    empty_block = Block(
        2,
        "0x" + "aa" * 32,
        "0x" + "bb" * 32,
        1_700_000_000,
        0,
        30_000_000,
        1,
        (),
        "0x" + "11" * 20,
        0,
        "0x" + "22" * 32,
    )
    rpc = Mock()
    rpc.get_block.side_effect = [
        RPCError(f"provider failed at {secret_url}"),
        empty_block,
    ]
    metadata = SoftwareMetadata("0.1.0", "abc", False, None)

    with patch("blockscope.evaluation.software_metadata", return_value=metadata):
        artifact = run_evaluation(
            rpc,
            secret_url,
            EvaluationConfiguration(1, 2, 10, 0),
            progress=None,
        )

    assert artifact.metrics.blocks_inspected == 2
    assert artifact.metrics.blocks_completed == 1
    assert artifact.blocks[0].state is BlockEvaluationState.PROVIDER_FAILURE
    assert artifact.blocks[1].state is BlockEvaluationState.COMPLETE
    path = tmp_path / "evaluation.json"
    write_evaluation_artifact(artifact, path)
    serialized = path.read_text()
    assert secret_url not in serialized
    assert "private-key" not in serialized
    assert "provider_error" in serialized


def test_programmer_error_is_not_converted_into_benchmark_data() -> None:
    rpc = Mock()
    rpc.get_block.side_effect = ValueError("broken invariant")

    with pytest.raises(ValueError, match="broken invariant"):
        run_evaluation(
            rpc,
            "https://rpc.example",
            EvaluationConfiguration(1, 1, 1, 0),
            progress=None,
        )
