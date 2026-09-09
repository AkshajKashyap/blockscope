from fractions import Fraction

import pytest

from blockscope.sandwiches import (
    TOKEN0_TO_TOKEN1,
    TOKEN1_TO_TOKEN0,
    DirectionalQuoteError,
    build_continuous_pair_segments,
    detect_strict_sandwich_candidates,
    raw_directional_quote,
)
from blockscope.uniswap_v2 import (
    EnrichedUniswapV2Swap,
    ReserveState,
    SwapReserveContext,
    UniswapV2PairMetadata,
    UniswapV2Swap,
)

PAIR_A = "0x" + "aa" * 20
PAIR_B = "0x" + "bb" * 20
ACTOR_A = "0x" + "a1" * 20
ACTOR_B = "0x" + "b1" * 20
ACTOR_C = "0x" + "c1" * 20
ACTOR_D = "0x" + "d1" * 20


def leg(
    transaction_index: int,
    sender: str,
    direction: str,
    pre: tuple[int, int] | None,
    post: tuple[int, int] | None,
    *,
    pair: str = PAIR_A,
    log_index: int | None = None,
    transaction_hash: str | None = None,
) -> EnrichedUniswapV2Swap:
    amounts = (10, 0, 0, 9) if direction == TOKEN0_TO_TOKEN1 else (0, 10, 9, 0)
    swap = UniswapV2Swap(
        block_number=100,
        transaction_hash=transaction_hash or f"0x{transaction_index:064x}",
        transaction_index=transaction_index,
        log_index=transaction_index if log_index is None else log_index,
        pair_address=pair,
        sender="0x" + "11" * 20,
        recipient="0x" + "22" * 20,
        amount0_in=amounts[0],
        amount1_in=amounts[1],
        amount0_out=amounts[2],
        amount1_out=amounts[3],
    )
    pair_metadata = UniswapV2PairMetadata(pair, None, None, None)
    return EnrichedUniswapV2Swap(
        reserve_context=SwapReserveContext(
            swap,
            None if pre is None else ReserveState(*pre),
            None if post is None else ReserveState(*post),
        ),
        pair_metadata=pair_metadata,
        token0_metadata=None,
        token1_metadata=None,
        transaction_sender=sender.lower(),
    )


def canonical_candidate() -> tuple[EnrichedUniswapV2Swap, ...]:
    return (
        leg(10, ACTOR_A, TOKEN0_TO_TOKEN1, (1_000, 2_000), (1_100, 1_800)),
        leg(11, ACTOR_B, TOKEN0_TO_TOKEN1, (1_100, 1_800), (1_200, 1_600)),
        leg(12, ACTOR_A, TOKEN1_TO_TOKEN0, (1_200, 1_600), (1_100, 1_750)),
    )


def test_raw_directional_quotes_are_exact_fractions() -> None:
    state = ReserveState(3, 7)

    assert raw_directional_quote(state, TOKEN0_TO_TOKEN1) == Fraction(7, 3)
    assert raw_directional_quote(state, TOKEN1_TO_TOKEN0) == Fraction(3, 7)


def test_raw_directional_quote_rejects_zero_input_reserve() -> None:
    with pytest.raises(DirectionalQuoteError):
        raw_directional_quote(ReserveState(0, 7), TOKEN0_TO_TOKEN1)


def test_detects_exactly_one_canonical_one_victim_candidate() -> None:
    result = detect_strict_sandwich_candidates(canonical_candidate())

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.actor_address == ACTOR_A
    assert candidate.front_run.swap.transaction_index == 10
    assert tuple(victim.swap.transaction_index for victim in candidate.victims) == (11,)
    assert candidate.back_run.swap.transaction_index == 12
    assert candidate.quote_before_front == Fraction(2, 1)
    assert candidate.quote_after_front == Fraction(18, 11)
    assert candidate.quote_before_first_victim == candidate.quote_after_front
    assert candidate.quote_before_back == Fraction(4, 3)
    assert candidate.quote_after_back == Fraction(35, 22)


def test_supports_multiple_execution_ordered_victims() -> None:
    swaps = (
        leg(10, ACTOR_A, TOKEN0_TO_TOKEN1, (1_000, 2_000), (1_100, 1_800)),
        leg(11, ACTOR_B, TOKEN0_TO_TOKEN1, (1_100, 1_800), (1_200, 1_600)),
        leg(12, ACTOR_C, TOKEN0_TO_TOKEN1, (1_200, 1_600), (1_300, 1_450)),
        leg(13, ACTOR_D, TOKEN0_TO_TOKEN1, (1_300, 1_450), (1_400, 1_320)),
        leg(14, ACTOR_A, TOKEN1_TO_TOKEN0, (1_400, 1_320), (1_300, 1_450)),
    )

    result = detect_strict_sandwich_candidates(swaps)

    assert len(result.candidates) == 1
    assert tuple(victim.swap.transaction_index for victim in result.candidates[0].victims) == (
        11,
        12,
        13,
    )


def test_rejects_closing_sender_mismatch() -> None:
    front, victim, _ = canonical_candidate()
    mismatched_back = leg(
        12,
        ACTOR_C,
        TOKEN1_TO_TOKEN0,
        (1_200, 1_600),
        (1_100, 1_750),
    )

    result = detect_strict_sandwich_candidates((front, victim, mismatched_back))

    assert result.candidates == ()
    assert result.diagnostics.rejected_closing_sender_mismatch >= 1


def test_does_not_skip_first_opposite_swap_to_find_matching_sender() -> None:
    front, victim, _ = canonical_candidate()
    first_opposite = leg(12, ACTOR_C, TOKEN1_TO_TOKEN0, (1_200, 1_600), (1_150, 1_700))
    later_matching = leg(13, ACTOR_A, TOKEN1_TO_TOKEN0, (1_150, 1_700), (1_100, 1_800))

    result = detect_strict_sandwich_candidates(
        (front, victim, first_opposite, later_matching)
    )

    assert result.candidates == ()
    assert result.diagnostics.rejected_closing_sender_mismatch >= 1


def test_rejects_direction_mismatch_without_a_victim() -> None:
    swaps = (
        leg(10, ACTOR_A, TOKEN0_TO_TOKEN1, (1_000, 2_000), (1_100, 1_800)),
        leg(11, ACTOR_B, TOKEN1_TO_TOKEN0, (1_100, 1_800), (1_000, 2_000)),
        leg(12, ACTOR_A, TOKEN0_TO_TOKEN1, (1_000, 2_000), (1_100, 1_800)),
    )

    result = detect_strict_sandwich_candidates(swaps)

    assert result.candidates == ()
    assert result.diagnostics.rejected_no_victims >= 1


@pytest.mark.parametrize(
    "broken_swaps",
    [
        (
            leg(10, ACTOR_A, TOKEN0_TO_TOKEN1, (1_000, 2_000), (1_100, 1_800)),
            leg(11, ACTOR_B, TOKEN0_TO_TOKEN1, (1_101, 1_800), (1_200, 1_600)),
            leg(12, ACTOR_A, TOKEN1_TO_TOKEN0, (1_200, 1_600), (1_100, 1_750)),
        ),
        (
            leg(10, ACTOR_A, TOKEN0_TO_TOKEN1, (1_000, 2_000), (1_100, 1_800)),
            leg(11, ACTOR_B, TOKEN0_TO_TOKEN1, (1_100, 1_800), (1_200, 1_600)),
            leg(12, ACTOR_A, TOKEN1_TO_TOKEN0, (1_201, 1_600), (1_100, 1_750)),
        ),
    ],
)
def test_rejects_broken_state_continuity(
    broken_swaps: tuple[EnrichedUniswapV2Swap, ...],
) -> None:
    result = detect_strict_sandwich_candidates(broken_swaps)

    assert result.candidates == ()
    assert len(build_continuous_pair_segments(broken_swaps)) == 2


def test_missing_reserves_break_pair_segment() -> None:
    front, _, back = canonical_candidate()
    unsupported = leg(11, ACTOR_B, TOKEN0_TO_TOKEN1, None, None)

    result = detect_strict_sandwich_candidates((front, unsupported, back))

    assert result.candidates == ()
    assert len(build_continuous_pair_segments((front, unsupported, back))) == 2


def test_rejects_front_that_does_not_worsen_raw_quote() -> None:
    swaps = (
        leg(10, ACTOR_A, TOKEN0_TO_TOKEN1, (1_000, 2_000), (1_100, 2_200)),
        leg(11, ACTOR_B, TOKEN0_TO_TOKEN1, (1_100, 2_200), (1_200, 2_000)),
        leg(12, ACTOR_A, TOKEN1_TO_TOKEN0, (1_200, 2_000), (1_100, 2_100)),
    )

    result = detect_strict_sandwich_candidates(swaps)

    assert result.candidates == ()
    assert result.diagnostics.rejected_adverse_movement == 1


def test_rejects_back_that_does_not_improve_original_direction_quote() -> None:
    swaps = (
        leg(10, ACTOR_A, TOKEN0_TO_TOKEN1, (1_000, 2_000), (1_100, 1_800)),
        leg(11, ACTOR_B, TOKEN0_TO_TOKEN1, (1_100, 1_800), (1_200, 1_600)),
        leg(12, ACTOR_A, TOKEN1_TO_TOKEN0, (1_200, 1_600), (1_300, 1_600)),
    )

    result = detect_strict_sandwich_candidates(swaps)

    assert result.candidates == ()
    assert result.diagnostics.rejected_back_run_reversal == 1


def test_same_actor_middle_trade_is_not_a_victim() -> None:
    swaps = (
        leg(10, ACTOR_A, TOKEN0_TO_TOKEN1, (1_000, 2_000), (1_100, 1_800)),
        leg(11, ACTOR_A, TOKEN0_TO_TOKEN1, (1_100, 1_800), (1_200, 1_600)),
        leg(12, ACTOR_A, TOKEN1_TO_TOKEN0, (1_200, 1_600), (1_100, 1_750)),
    )

    result = detect_strict_sandwich_candidates(swaps)

    assert result.candidates == ()
    assert result.diagnostics.rejected_invalid_leg_sequence >= 1


def test_multiple_logs_from_one_transaction_do_not_become_distinct_legs() -> None:
    shared_hash = "0x" + "10" * 32
    swaps = (
        leg(
            10,
            ACTOR_A,
            TOKEN0_TO_TOKEN1,
            (1_000, 2_000),
            (1_050, 1_900),
            log_index=1,
            transaction_hash=shared_hash,
        ),
        leg(
            10,
            ACTOR_A,
            TOKEN0_TO_TOKEN1,
            (1_050, 1_900),
            (1_100, 1_800),
            log_index=2,
            transaction_hash=shared_hash,
        ),
        leg(11, ACTOR_A, TOKEN1_TO_TOKEN0, (1_100, 1_800), (1_000, 2_000)),
    )

    result = detect_strict_sandwich_candidates(swaps)

    assert result.candidates == ()


def test_candidates_are_deterministic_deduplicated_and_globally_ordered() -> None:
    pair_b_candidate = (
        leg(1, ACTOR_C, TOKEN0_TO_TOKEN1, (1_000, 2_000), (1_100, 1_800), pair=PAIR_B),
        leg(3, ACTOR_D, TOKEN0_TO_TOKEN1, (1_100, 1_800), (1_200, 1_600), pair=PAIR_B),
        leg(5, ACTOR_C, TOKEN1_TO_TOKEN0, (1_200, 1_600), (1_100, 1_750), pair=PAIR_B),
    )
    pair_a_candidate = (
        leg(2, ACTOR_A, TOKEN0_TO_TOKEN1, (2_000, 4_000), (2_200, 3_600)),
        leg(4, ACTOR_B, TOKEN0_TO_TOKEN1, (2_200, 3_600), (2_400, 3_200)),
        leg(6, ACTOR_A, TOKEN1_TO_TOKEN0, (2_400, 3_200), (2_200, 3_500)),
    )
    interleaved = (
        pair_b_candidate[0],
        pair_a_candidate[0],
        pair_b_candidate[1],
        pair_a_candidate[1],
        pair_b_candidate[2],
        pair_a_candidate[2],
    )

    first = detect_strict_sandwich_candidates(interleaved)
    second = detect_strict_sandwich_candidates(tuple(reversed(interleaved)))

    assert tuple(candidate.front_run.swap.transaction_index for candidate in first.candidates) == (
        1,
        2,
    )
    assert first.candidates == second.candidates
    assert first.diagnostics.strict_candidates_found == 2
    assert first.diagnostics.duplicate_candidates_suppressed == 0
