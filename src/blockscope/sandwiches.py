"""Conservative, evidence-driven strict sandwich-candidate detection."""

from dataclasses import dataclass
from fractions import Fraction

from blockscope.uniswap_v2 import EnrichedUniswapV2Swap, ReserveState

TOKEN0_TO_TOKEN1 = "token0 -> token1"
TOKEN1_TO_TOKEN0 = "token1 -> token0"
_OPPOSITE_DIRECTION = {
    TOKEN0_TO_TOKEN1: TOKEN1_TO_TOKEN0,
    TOKEN1_TO_TOKEN0: TOKEN0_TO_TOKEN1,
}


class DirectionalQuoteError(ValueError):
    """Raised when reserves cannot define the requested raw directional quote."""


@dataclass(frozen=True, slots=True)
class SandwichCandidate:
    """Exact evidence for a high-precision historical sandwich candidate."""

    pair_address: str
    actor_address: str
    direction: str
    front_run: EnrichedUniswapV2Swap
    victims: tuple[EnrichedUniswapV2Swap, ...]
    back_run: EnrichedUniswapV2Swap
    quote_before_front: Fraction
    quote_after_front: Fraction
    quote_before_first_victim: Fraction
    quote_before_back: Fraction
    quote_after_back: Fraction


@dataclass(frozen=True, slots=True)
class SandwichDiagnostics:
    """Auditable reasons potential front legs were accepted or rejected."""

    continuous_pair_segments: int
    potential_front_legs_considered: int
    rejected_no_victims: int
    rejected_no_closing_back_run: int
    rejected_invalid_leg_sequence: int
    rejected_closing_sender_mismatch: int
    rejected_invalid_quote: int
    rejected_adverse_movement: int
    rejected_back_run_reversal: int
    duplicate_candidates_suppressed: int
    strict_candidates_found: int


@dataclass(frozen=True, slots=True)
class SandwichScanResult:
    """Globally ordered strict candidates and deterministic scan diagnostics."""

    candidates: tuple[SandwichCandidate, ...]
    diagnostics: SandwichDiagnostics


def raw_directional_quote(state: ReserveState, direction: str) -> Fraction:
    """Return the exact output-reserve/input-reserve quote for a raw direction."""
    if direction == TOKEN0_TO_TOKEN1:
        numerator, denominator = state.reserve1, state.reserve0
    elif direction == TOKEN1_TO_TOKEN0:
        numerator, denominator = state.reserve0, state.reserve1
    else:
        raise DirectionalQuoteError(f"Unsupported swap direction: {direction}")
    if denominator <= 0:
        raise DirectionalQuoteError("Raw directional quote has no positive input reserve")
    if numerator < 0:
        raise DirectionalQuoteError("Raw directional quote has a negative output reserve")
    return Fraction(numerator, denominator)


def _execution_key(swap: EnrichedUniswapV2Swap) -> tuple[int, int]:
    return swap.swap.transaction_index, swap.swap.log_index


def build_continuous_pair_segments(
    swaps: tuple[EnrichedUniswapV2Swap, ...],
) -> tuple[tuple[EnrichedUniswapV2Swap, ...], ...]:
    """Group by pair and split whenever exact event-derived state continuity breaks."""
    pair_swaps: dict[str, list[EnrichedUniswapV2Swap]] = {}
    for enriched in sorted(swaps, key=_execution_key):
        pair_swaps.setdefault(enriched.swap.pair_address.lower(), []).append(enriched)

    segments: list[tuple[EnrichedUniswapV2Swap, ...]] = []
    for swaps_for_pair in pair_swaps.values():
        current: list[EnrichedUniswapV2Swap] = []
        for enriched in swaps_for_pair:
            context = enriched.reserve_context
            if context.pre_reserves is None or context.post_reserves is None:
                if current:
                    segments.append(tuple(current))
                    current = []
                continue
            if current and current[-1].reserve_context.post_reserves != context.pre_reserves:
                segments.append(tuple(current))
                current = []
            current.append(enriched)
        if current:
            segments.append(tuple(current))
    return tuple(sorted(segments, key=lambda segment: _execution_key(segment[0])))


def _same_transaction(
    first: EnrichedUniswapV2Swap,
    second: EnrichedUniswapV2Swap,
) -> bool:
    return (
        first.swap.transaction_index == second.swap.transaction_index
        or first.swap.transaction_hash.lower() == second.swap.transaction_hash.lower()
    )


def detect_strict_sandwich_candidates(
    swaps: tuple[EnrichedUniswapV2Swap, ...],
) -> SandwichScanResult:
    """Apply the deterministic first-opposite-close strict scanning rule."""
    segments = build_continuous_pair_segments(swaps)
    candidates: list[SandwichCandidate] = []
    seen: set[tuple[tuple[int, int], ...]] = set()
    considered = 0
    no_victims = 0
    no_closing = 0
    invalid_sequence = 0
    sender_mismatch = 0
    invalid_quote = 0
    adverse_failed = 0
    reversal_failed = 0
    duplicates = 0

    for segment in segments:
        for front_position, front in enumerate(segment):
            considered += 1
            direction = front.swap.direction
            actor = front.transaction_sender
            if direction not in _OPPOSITE_DIRECTION or actor is None:
                invalid_sequence += 1
                continue

            victims: list[EnrichedUniswapV2Swap] = []
            closing: EnrichedUniswapV2Swap | None = None
            sequence_invalid = False
            for subsequent in segment[front_position + 1 :]:
                subsequent_direction = subsequent.swap.direction
                if subsequent_direction == direction:
                    if (
                        subsequent.transaction_sender is None
                        or subsequent.transaction_sender.lower() == actor.lower()
                        or any(_same_transaction(subsequent, leg) for leg in (front, *victims))
                    ):
                        sequence_invalid = True
                        break
                    victims.append(subsequent)
                    continue
                if subsequent_direction == _OPPOSITE_DIRECTION[direction]:
                    closing = subsequent
                    break
                sequence_invalid = True
                break

            if sequence_invalid:
                invalid_sequence += 1
                continue
            if closing is None:
                if victims:
                    no_closing += 1
                else:
                    no_victims += 1
                continue
            if not victims:
                no_victims += 1
                continue
            if any(_same_transaction(closing, leg) for leg in (front, *victims)):
                invalid_sequence += 1
                continue
            if closing.transaction_sender is None or closing.transaction_sender.lower() != actor.lower():
                sender_mismatch += 1
                continue

            front_context = front.reserve_context
            first_victim_context = victims[0].reserve_context
            back_context = closing.reserve_context
            front_pre = front_context.pre_reserves
            front_post = front_context.post_reserves
            first_victim_pre = first_victim_context.pre_reserves
            back_pre = back_context.pre_reserves
            back_post = back_context.post_reserves
            if any(
                state is None
                for state in (front_pre, front_post, first_victim_pre, back_pre, back_post)
            ):
                invalid_sequence += 1
                continue
            try:
                quote_before_front = raw_directional_quote(front_pre, direction)
                quote_after_front = raw_directional_quote(front_post, direction)
                quote_before_first_victim = raw_directional_quote(first_victim_pre, direction)
                quote_before_back = raw_directional_quote(back_pre, direction)
                quote_after_back = raw_directional_quote(back_post, direction)
            except DirectionalQuoteError:
                invalid_quote += 1
                continue
            if not (
                quote_after_front < quote_before_front
                and quote_before_first_victim == quote_after_front
            ):
                adverse_failed += 1
                continue
            if quote_after_back <= quote_before_back:
                reversal_failed += 1
                continue

            legs = (front, *victims, closing)
            key = tuple(_execution_key(leg) for leg in legs)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            candidates.append(
                SandwichCandidate(
                    pair_address=front.swap.pair_address,
                    actor_address=actor.lower(),
                    direction=direction,
                    front_run=front,
                    victims=tuple(victims),
                    back_run=closing,
                    quote_before_front=quote_before_front,
                    quote_after_front=quote_after_front,
                    quote_before_first_victim=quote_before_first_victim,
                    quote_before_back=quote_before_back,
                    quote_after_back=quote_after_back,
                )
            )

    candidates.sort(key=lambda candidate: _execution_key(candidate.front_run))
    diagnostics = SandwichDiagnostics(
        continuous_pair_segments=len(segments),
        potential_front_legs_considered=considered,
        rejected_no_victims=no_victims,
        rejected_no_closing_back_run=no_closing,
        rejected_invalid_leg_sequence=invalid_sequence,
        rejected_closing_sender_mismatch=sender_mismatch,
        rejected_invalid_quote=invalid_quote,
        rejected_adverse_movement=adverse_failed,
        rejected_back_run_reversal=reversal_failed,
        duplicate_candidates_suppressed=duplicates,
        strict_candidates_found=len(candidates),
    )
    return SandwichScanResult(tuple(candidates), diagnostics)
