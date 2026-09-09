from dataclasses import replace
from fractions import Fraction

import pytest

from blockscope.economics import (
    analyze_observed_sandwich_economics,
    calculate_observed_sandwich_economics,
    transaction_gas_fee_wei,
)
from blockscope.sandwiches import TOKEN0_TO_TOKEN1, TOKEN1_TO_TOKEN0, SandwichCandidate
from blockscope.types import TransactionReceipt
from blockscope.uniswap_v2 import (
    EnrichedUniswapV2Swap,
    ReserveState,
    SwapReserveContext,
    TokenMetadata,
    UniswapV2PairMetadata,
    UniswapV2Swap,
)

PAIR = "0x" + "aa" * 20
TOKEN0 = "0x" + "01" * 20
TOKEN1 = "0x" + "02" * 20
ACTOR = "0x" + "10" * 20
VICTIM = "0x" + "11" * 20


def enriched(
    transaction_index: int,
    sender: str,
    amounts: tuple[int, int, int, int],
    pre: tuple[int, int],
    post: tuple[int, int],
    *,
    metadata: bool = True,
) -> EnrichedUniswapV2Swap:
    swap = UniswapV2Swap(
        100,
        f"0x{transaction_index:064x}",
        transaction_index,
        transaction_index,
        PAIR,
        "0x" + "22" * 20,
        "0x" + "23" * 20,
        *amounts,
    )
    pair = UniswapV2PairMetadata(PAIR, None, TOKEN0, TOKEN1)
    return EnrichedUniswapV2Swap(
        SwapReserveContext(swap, ReserveState(*pre), ReserveState(*post)),
        pair,
        TokenMetadata(TOKEN0, 18, "TK0") if metadata else None,
        TokenMetadata(TOKEN1, 6, "TK1") if metadata else None,
        sender,
    )


def candidate(
    *,
    direction: str = TOKEN0_TO_TOKEN1,
    front_amounts: tuple[int, int, int, int] = (100, 0, 0, 50),
    victim_amounts: tuple[int, int, int, int] = (20, 0, 0, 8),
    back_amounts: tuple[int, int, int, int] = (0, 50, 110, 0),
    metadata: bool = True,
) -> SandwichCandidate:
    front = enriched(
        10,
        ACTOR,
        front_amounts,
        (1_000, 2_000),
        (1_100, 1_820),
        metadata=metadata,
    )
    victim = enriched(
        11,
        VICTIM,
        victim_amounts,
        (1_100, 1_820),
        (1_200, 1_680),
        metadata=metadata,
    )
    back = enriched(
        12,
        ACTOR,
        back_amounts,
        (1_200, 1_680),
        (1_100, 1_850),
        metadata=metadata,
    )
    return SandwichCandidate(
        PAIR,
        ACTOR,
        direction,
        front,
        (victim,),
        back,
        Fraction(2),
        Fraction(18, 11),
        Fraction(18, 11),
        Fraction(7, 5),
        Fraction(37, 22),
    )


def receipt(
    transaction_index: int,
    gas_used: int,
    effective_gas_price: int | None,
) -> TransactionReceipt:
    return TransactionReceipt(
        transaction_hash=f"0x{transaction_index:064x}",
        transaction_index=transaction_index,
        block_number=100,
        status=1,
        gas_used=gas_used,
        logs=(),
        effective_gas_price=effective_gas_price,
    )


@pytest.mark.parametrize(
    ("back_input", "back_output", "expected_inventory", "expected_cycle"),
    [(50, 110, 0, 10), (45, 110, 5, 10), (60, 110, -10, 10), (50, 95, 0, -5)],
)
def test_observed_outer_cycle_signed_deltas(
    back_input: int,
    back_output: int,
    expected_inventory: int,
    expected_cycle: int,
) -> None:
    economics = calculate_observed_sandwich_economics(
        candidate(back_amounts=(0, back_input, back_output, 0)),
        (),
    )

    assert economics.front_input.raw_amount == 100
    assert economics.front_output.raw_amount == 50
    assert economics.back_input.raw_amount == back_input
    assert economics.back_output.raw_amount == back_output
    assert economics.gross_cycle_delta.raw_amount == expected_cycle
    assert economics.intermediate_inventory_delta.raw_amount == expected_inventory
    assert economics.gross_cycle_delta.asset.address == TOKEN0
    assert economics.intermediate_inventory_delta.asset.address == TOKEN1


def test_gas_fee_uses_actual_gas_and_effective_price_exactly() -> None:
    front_receipt = receipt(10, 120_000, 20_000_000_000)
    victim_receipt = receipt(11, 80_000, 15_000_000_000)
    back_receipt = receipt(12, 90_000, 30_000_000_000)

    economics = calculate_observed_sandwich_economics(
        candidate(),
        (front_receipt, victim_receipt, back_receipt),
    )

    assert transaction_gas_fee_wei(front_receipt) == 2_400_000_000_000_000
    assert economics.front_gas_fee_wei == 2_400_000_000_000_000
    assert economics.back_gas_fee_wei == 2_700_000_000_000_000
    assert economics.total_outer_gas_fee_wei == 5_100_000_000_000_000
    assert economics.victim_executions[0].gas_fee_wei == 1_200_000_000_000_000


def test_missing_effective_gas_price_keeps_outer_total_unavailable() -> None:
    receipts = (receipt(10, 120_000, None), receipt(11, 80_000, 1), receipt(12, 90_000, 2))

    analysis = analyze_observed_sandwich_economics((candidate(),), receipts)
    economics = analysis.candidates[0]

    assert economics.front_gas_fee_wei is None
    assert economics.back_gas_fee_wei == 180_000
    assert economics.total_outer_gas_fee_wei is None
    assert analysis.diagnostics.missing_receipt_gas_data == 1


def test_victim_actual_execution_ratio_is_exact_for_token0_to_token1() -> None:
    economics = calculate_observed_sandwich_economics(candidate(), ())
    execution = economics.victim_executions[0]

    assert execution.input_amount.raw_amount == 20
    assert execution.input_amount.asset.address == TOKEN0
    assert execution.output_amount.raw_amount == 8
    assert execution.output_amount.asset.address == TOKEN1
    assert execution.actual_raw_execution_ratio == Fraction(2, 5)


def test_victim_actual_execution_ratio_is_exact_for_reverse_direction() -> None:
    reverse = candidate(
        direction=TOKEN1_TO_TOKEN0,
        front_amounts=(0, 100, 50, 0),
        victim_amounts=(0, 20, 8, 0),
        back_amounts=(110, 0, 0, 50),
    )

    economics = calculate_observed_sandwich_economics(reverse, ())
    execution = economics.victim_executions[0]

    assert execution.input_amount.asset.address == TOKEN1
    assert execution.output_amount.asset.address == TOKEN0
    assert execution.actual_raw_execution_ratio == Fraction(2, 5)


def test_large_victim_execution_ratio_never_uses_float() -> None:
    large_input = 2**200 + 123
    large_output = 2**220 + 456
    large = candidate(victim_amounts=(large_input, 0, 0, large_output))

    ratio = calculate_observed_sandwich_economics(large, ()).victim_executions[
        0
    ].actual_raw_execution_ratio

    assert ratio == Fraction(large_output, large_input)
    assert isinstance(ratio, Fraction)


def test_zero_victim_input_is_explicitly_unavailable() -> None:
    invalid = candidate(victim_amounts=(0, 0, 0, 8))

    analysis = analyze_observed_sandwich_economics((invalid,), ())

    assert analysis.candidates[0].victim_executions[0].actual_raw_execution_ratio is None
    assert analysis.diagnostics.invalid_or_zero_victim_inputs == 1


def test_invariant_evidence_is_exact_and_decrease_is_diagnostic_only() -> None:
    base = candidate()
    decreasing_front = replace(
        base.front_run,
        reserve_context=SwapReserveContext(
            base.front_run.swap,
            ReserveState(1_000, 2_000),
            ReserveState(1_100, 1_800),
        ),
    )
    decreasing_candidate = replace(base, front_run=decreasing_front)

    analysis = analyze_observed_sandwich_economics((decreasing_candidate,), ())
    economics = analysis.candidates[0]
    front_invariant = economics.outer_invariants[0]

    assert front_invariant.k_pre == 2_000_000
    assert front_invariant.k_post == 1_980_000
    assert front_invariant.k_delta == -20_000
    assert analysis.diagnostics.unusual_invariant_decreases == 1
    assert len(analysis.candidates) == 1


def test_missing_token_metadata_is_diagnostic_without_losing_raw_accounting() -> None:
    analysis = analyze_observed_sandwich_economics((candidate(metadata=False),), ())

    assert analysis.candidates[0].gross_cycle_delta.raw_amount == 10
    assert analysis.diagnostics.missing_token_metadata == 2


def test_multiple_victims_remain_individual_and_have_exact_aggregates() -> None:
    base = candidate()
    second_victim = enriched(
        13,
        "0x" + "13" * 20,
        (30, 0, 0, 12),
        (1_200, 1_680),
        (1_300, 1_550),
    )
    multiple = SandwichCandidate(
        base.pair_address,
        base.actor_address,
        base.direction,
        base.front_run,
        (*base.victims, second_victim),
        base.back_run,
        base.quote_before_front,
        base.quote_after_front,
        base.quote_before_first_victim,
        base.quote_before_back,
        base.quote_after_back,
    )

    economics = calculate_observed_sandwich_economics(multiple, ())

    assert len(economics.victim_executions) == 2
    assert economics.aggregate_victim_input.raw_amount == 50
    assert economics.aggregate_victim_output.raw_amount == 20
