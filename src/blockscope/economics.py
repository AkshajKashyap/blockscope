"""Observed pair-level economics for strict sandwich candidates."""

from dataclasses import dataclass
from fractions import Fraction

from blockscope.sandwiches import (
    TOKEN0_TO_TOKEN1,
    TOKEN1_TO_TOKEN0,
    SandwichCandidate,
)
from blockscope.types import (
    TransactionReceipt,
    index_transaction_receipts,
    transaction_identity,
)
from blockscope.uniswap_v2 import EnrichedUniswapV2Swap, TokenMetadata


class ObservedEconomicsError(ValueError):
    """Raised when a supplied candidate lacks required observed Swap evidence."""


@dataclass(frozen=True, slots=True)
class ObservedAsset:
    """A pair token position with best-effort historical metadata."""

    pair_position: str
    address: str | None
    symbol: str | None
    decimals: int | None


@dataclass(frozen=True, slots=True)
class ObservedAmount:
    """An exact signed or unsigned raw amount of one observed asset."""

    asset: ObservedAsset
    raw_amount: int


@dataclass(frozen=True, slots=True)
class SwapInvariantEvidence:
    """Exact reserve-product evidence surrounding one reconstructed Swap."""

    enriched_swap: EnrichedUniswapV2Swap
    k_pre: int
    k_post: int
    k_delta: int

    @property
    def decreased(self) -> bool:
        return self.k_delta < 0


@dataclass(frozen=True, slots=True)
class VictimActualExecution:
    """One victim's observed pair flow and exact actual execution ratio."""

    victim: EnrichedUniswapV2Swap
    input_amount: ObservedAmount
    output_amount: ObservedAmount
    actual_raw_execution_ratio: Fraction | None
    gas_fee_wei: int | None
    invariant: SwapInvariantEvidence


@dataclass(frozen=True, slots=True)
class ObservedSandwichEconomics:
    """Observed and directly derived pair-level facts, not wallet-level profit."""

    candidate: SandwichCandidate
    initial_asset: ObservedAsset
    intermediate_asset: ObservedAsset
    front_input: ObservedAmount
    front_output: ObservedAmount
    back_input: ObservedAmount
    back_output: ObservedAmount
    gross_cycle_delta: ObservedAmount
    intermediate_inventory_delta: ObservedAmount
    front_gas_fee_wei: int | None
    back_gas_fee_wei: int | None
    total_outer_gas_fee_wei: int | None
    victim_executions: tuple[VictimActualExecution, ...]
    aggregate_victim_input: ObservedAmount
    aggregate_victim_output: ObservedAmount
    outer_invariants: tuple[SwapInvariantEvidence, SwapInvariantEvidence]


@dataclass(frozen=True, slots=True)
class EconomicsDiagnostics:
    """Counts of calculated economics and explicitly unavailable evidence."""

    economics_calculated: int
    missing_receipt_gas_data: int
    invalid_or_zero_victim_inputs: int
    unusual_invariant_decreases: int
    missing_token_metadata: int


@dataclass(frozen=True, slots=True)
class ObservedEconomicsAnalysis:
    """Ordered candidate economics and aggregate diagnostics."""

    candidates: tuple[ObservedSandwichEconomics, ...]
    diagnostics: EconomicsDiagnostics


def transaction_gas_fee_wei(receipt: TransactionReceipt | None) -> int | None:
    """Return actual mined gas expense when effective gas price is available."""
    if receipt is None or receipt.effective_gas_price is None:
        return None
    return receipt.gas_used * receipt.effective_gas_price


def _asset(
    position: str,
    address: str | None,
    metadata: TokenMetadata | None,
) -> ObservedAsset:
    return ObservedAsset(
        pair_position=position,
        address=address,
        symbol=None if metadata is None else metadata.symbol,
        decimals=None if metadata is None else metadata.decimals,
    )


def _invariant(enriched: EnrichedUniswapV2Swap) -> SwapInvariantEvidence:
    context = enriched.reserve_context
    if context.pre_reserves is None or context.post_reserves is None:
        raise ObservedEconomicsError("Candidate leg lacks reconstructed reserve evidence")
    k_pre = context.pre_reserves.reserve0 * context.pre_reserves.reserve1
    k_post = context.post_reserves.reserve0 * context.post_reserves.reserve1
    return SwapInvariantEvidence(enriched, k_pre, k_post, k_post - k_pre)


def _gas_for(
    enriched: EnrichedUniswapV2Swap,
    receipts: dict[tuple[int, str], TransactionReceipt],
) -> int | None:
    key = transaction_identity(
        enriched.swap.transaction_index,
        enriched.swap.transaction_hash,
    )
    return transaction_gas_fee_wei(receipts.get(key))


def _flow_for_direction(
    enriched: EnrichedUniswapV2Swap,
    direction: str,
    token0: ObservedAsset,
    token1: ObservedAsset,
) -> tuple[ObservedAmount, ObservedAmount]:
    swap = enriched.swap
    if direction == TOKEN0_TO_TOKEN1:
        return ObservedAmount(token0, swap.amount0_in), ObservedAmount(token1, swap.amount1_out)
    if direction == TOKEN1_TO_TOKEN0:
        return ObservedAmount(token1, swap.amount1_in), ObservedAmount(token0, swap.amount0_out)
    raise ObservedEconomicsError("Candidate contains a Swap without an ordinary direction")


def calculate_observed_sandwich_economics(
    candidate: SandwichCandidate,
    receipts: tuple[TransactionReceipt, ...],
) -> ObservedSandwichEconomics:
    """Calculate exact observed pair flows for one strict candidate."""
    pair = candidate.front_run.pair_metadata
    token0 = _asset("token0", pair.token0_address, candidate.front_run.token0_metadata)
    token1 = _asset("token1", pair.token1_address, candidate.front_run.token1_metadata)
    if candidate.direction == TOKEN0_TO_TOKEN1:
        initial_asset, intermediate_asset = token0, token1
    elif candidate.direction == TOKEN1_TO_TOKEN0:
        initial_asset, intermediate_asset = token1, token0
    else:
        raise ObservedEconomicsError("Candidate direction is not an ordinary V2 direction")

    front_input, front_output = _flow_for_direction(
        candidate.front_run, candidate.direction, token0, token1
    )
    back_direction = (
        TOKEN1_TO_TOKEN0 if candidate.direction == TOKEN0_TO_TOKEN1 else TOKEN0_TO_TOKEN1
    )
    back_input, back_output = _flow_for_direction(
        candidate.back_run, back_direction, token0, token1
    )
    receipt_by_transaction = index_transaction_receipts(receipts)
    front_gas = _gas_for(candidate.front_run, receipt_by_transaction)
    back_gas = _gas_for(candidate.back_run, receipt_by_transaction)
    total_outer_gas = None if front_gas is None or back_gas is None else front_gas + back_gas

    victim_executions: list[VictimActualExecution] = []
    for victim in candidate.victims:
        victim_input, victim_output = _flow_for_direction(
            victim, candidate.direction, token0, token1
        )
        ratio = (
            None
            if victim_input.raw_amount <= 0
            else Fraction(victim_output.raw_amount, victim_input.raw_amount)
        )
        victim_executions.append(
            VictimActualExecution(
                victim=victim,
                input_amount=victim_input,
                output_amount=victim_output,
                actual_raw_execution_ratio=ratio,
                gas_fee_wei=_gas_for(victim, receipt_by_transaction),
                invariant=_invariant(victim),
            )
        )

    aggregate_victim_input = ObservedAmount(
        initial_asset,
        sum(execution.input_amount.raw_amount for execution in victim_executions),
    )
    aggregate_victim_output = ObservedAmount(
        intermediate_asset,
        sum(execution.output_amount.raw_amount for execution in victim_executions),
    )
    return ObservedSandwichEconomics(
        candidate=candidate,
        initial_asset=initial_asset,
        intermediate_asset=intermediate_asset,
        front_input=front_input,
        front_output=front_output,
        back_input=back_input,
        back_output=back_output,
        gross_cycle_delta=ObservedAmount(
            initial_asset,
            back_output.raw_amount - front_input.raw_amount,
        ),
        intermediate_inventory_delta=ObservedAmount(
            intermediate_asset,
            front_output.raw_amount - back_input.raw_amount,
        ),
        front_gas_fee_wei=front_gas,
        back_gas_fee_wei=back_gas,
        total_outer_gas_fee_wei=total_outer_gas,
        victim_executions=tuple(victim_executions),
        aggregate_victim_input=aggregate_victim_input,
        aggregate_victim_output=aggregate_victim_output,
        outer_invariants=(_invariant(candidate.front_run), _invariant(candidate.back_run)),
    )


def analyze_observed_sandwich_economics(
    candidates: tuple[SandwichCandidate, ...],
    receipts: tuple[TransactionReceipt, ...],
) -> ObservedEconomicsAnalysis:
    """Calculate ordered observed economics and aggregate evidence diagnostics."""
    calculated = tuple(
        calculate_observed_sandwich_economics(candidate, receipts) for candidate in candidates
    )
    all_victims = tuple(
        victim
        for economics in calculated
        for victim in economics.victim_executions
    )
    all_invariants = tuple(
        invariant
        for economics in calculated
        for invariant in economics.outer_invariants
        + tuple(victim.invariant for victim in economics.victim_executions)
    )
    missing_gas = sum(
        fee is None
        for economics in calculated
        for fee in (
            economics.front_gas_fee_wei,
            economics.back_gas_fee_wei,
            *(victim.gas_fee_wei for victim in economics.victim_executions),
        )
    )
    missing_metadata = sum(
        asset.address is None or asset.symbol is None or asset.decimals is None
        for economics in calculated
        for asset in (economics.initial_asset, economics.intermediate_asset)
    )
    diagnostics = EconomicsDiagnostics(
        economics_calculated=len(calculated),
        missing_receipt_gas_data=missing_gas,
        invalid_or_zero_victim_inputs=sum(
            victim.actual_raw_execution_ratio is None for victim in all_victims
        ),
        unusual_invariant_decreases=sum(invariant.decreased for invariant in all_invariants),
        missing_token_metadata=missing_metadata,
    )
    return ObservedEconomicsAnalysis(calculated, diagnostics)
