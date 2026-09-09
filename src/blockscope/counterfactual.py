"""Canonical Uniswap V2 fixed-input, pair-level victim counterfactuals."""

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from blockscope.rpc import BlockScopeError, EthereumRPC
from blockscope.sandwiches import (
    TOKEN0_TO_TOKEN1,
    TOKEN1_TO_TOKEN0,
    SandwichCandidate,
)
from blockscope.uniswap_v2 import (
    EnrichedUniswapV2Swap,
    ReserveState,
    UniswapV2PairMetadata,
)

ETHEREUM_MAINNET_CHAIN_ID = 1
CANONICAL_UNISWAP_V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
GET_PAIR_SELECTOR = "0xe6a43905"


class CounterfactualInputError(ValueError):
    """Raised when exact-input canonical pricing cannot use supplied evidence."""


class CounterfactualStatus(StrEnum):
    """Categorical model state without probabilistic confidence claims."""

    EXACT_OBSERVED_REPRODUCTION = "canonical model reproduced observed execution"
    MODEL_LIMITED = "canonical model did not exactly reproduce observed execution"
    PROVENANCE_UNAVAILABLE = "counterfactual unavailable: provenance not established"
    INVALID_INPUT = "counterfactual unavailable: invalid reserve/input evidence"


@dataclass(frozen=True, slots=True)
class CanonicalV2Provenance:
    """Historical evidence connecting a pair to the canonical mainnet V2 factory."""

    established: bool
    chain_id: int | None
    canonical_factory_address: str
    pair_reported_factory_address: str | None
    factory_get_pair_address: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class CounterfactualVictimExecution:
    """Observed and fixed-input pair-level evidence for one victim in order."""

    victim: EnrichedUniswapV2Swap
    observed_pre_reserves: ReserveState
    observed_input: int
    observed_output: int
    observed_post_reserves: ReserveState
    observed_model_quote: int
    observed_model_difference: int
    observed_model_exact_match: bool
    counterfactual_pre_reserves: ReserveState
    fixed_input: int
    counterfactual_output: int
    counterfactual_post_reserves: ReserveState
    pair_output_delta: int
    relative_output_improvement: Fraction | None


@dataclass(frozen=True, slots=True)
class FixedInputCounterfactual:
    """A candidate's fixed-input victim replay, ending before the observed back leg."""

    candidate: SandwichCandidate
    provenance: CanonicalV2Provenance
    status: CounterfactualStatus
    victims: tuple[CounterfactualVictimExecution, ...]
    initial_reserves: ReserveState | None
    reserve_trajectory: tuple[ReserveState, ...]
    aggregate_observed_output: int | None
    aggregate_counterfactual_output: int | None
    aggregate_pair_output_delta: int | None
    unavailable_reason: str | None


@dataclass(frozen=True, slots=True)
class CounterfactualDiagnostics:
    """Counts describing provenance, replay, and observed-model validation."""

    canonical_provenance_established: int
    canonical_provenance_unavailable: int
    counterfactuals_computed: int
    invalid_reserve_or_input_cases: int
    observed_model_exact_matches: int
    observed_model_mismatches: int


@dataclass(frozen=True, slots=True)
class CounterfactualAnalysis:
    """Ordered candidate counterfactuals and aggregate diagnostics."""

    candidates: tuple[FixedInputCounterfactual, ...]
    diagnostics: CounterfactualDiagnostics


def canonical_v2_get_amount_out(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    """Return canonical V2 fixed-input output with Solidity-style floor division."""
    if amount_in <= 0:
        raise CounterfactualInputError("amount_in must be positive")
    if reserve_in <= 0:
        raise CounterfactualInputError("reserve_in must be positive")
    if reserve_out <= 0:
        raise CounterfactualInputError("reserve_out must be positive")
    amount_in_with_fee = amount_in * 997
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * 1000 + amount_in_with_fee
    return numerator // denominator


def _address_word(address: str) -> str:
    raw = address.lower().removeprefix("0x")
    if len(raw) != 40:
        raise CounterfactualInputError(f"Invalid address for getPair(): {address}")
    try:
        bytes.fromhex(raw)
    except ValueError as exc:
        raise CounterfactualInputError(f"Invalid address for getPair(): {address}") from exc
    return "0" * 24 + raw


def _decode_address(result: bytes) -> str:
    if len(result) != 32 or any(result[:12]):
        raise CounterfactualInputError("getPair() did not return an ABI-encoded address")
    return f"0x{result[12:].hex()}"


class CanonicalProvenanceResolver:
    """Command-local cached historical provenance resolver."""

    def __init__(self, rpc: EthereumRPC, block_number: int) -> None:
        self._rpc = rpc
        self._block_number = block_number
        self._cache: dict[str, CanonicalV2Provenance] = {}
        self._chain_id_loaded = False
        self._chain_id: int | None = None

    def _load_chain_id(self) -> int | None:
        if not self._chain_id_loaded:
            try:
                self._chain_id = self._rpc.get_chain_id()
            except BlockScopeError:
                self._chain_id = None
            self._chain_id_loaded = True
        return self._chain_id

    def pair(self, metadata: UniswapV2PairMetadata) -> CanonicalV2Provenance:
        key = metadata.pair_address.lower()
        if key in self._cache:
            return self._cache[key]

        actual_factory = metadata.factory_address
        token0 = metadata.token0_address
        token1 = metadata.token1_address
        reason: str | None = None
        factory_pair: str | None = None
        chain_id: int | None = None
        if actual_factory is None:
            reason = "pair factory() metadata is unavailable"
        elif actual_factory.lower() != CANONICAL_UNISWAP_V2_FACTORY.lower():
            reason = "pair factory() is not the canonical Ethereum mainnet V2 factory"
        elif token0 is None or token1 is None:
            reason = "pair token0()/token1() metadata is unavailable"
        else:
            chain_id = self._load_chain_id()
            if chain_id is None:
                reason = "Ethereum chain ID could not be established"
            elif chain_id != ETHEREUM_MAINNET_CHAIN_ID:
                reason = f"connected chain ID {chain_id} is not Ethereum mainnet"
            else:
                try:
                    call_data = GET_PAIR_SELECTOR + _address_word(token0) + _address_word(token1)
                except CounterfactualInputError:
                    reason = "pair token address metadata is invalid"
                else:
                    try:
                        result = self._rpc.eth_call(
                            CANONICAL_UNISWAP_V2_FACTORY,
                            call_data,
                            self._block_number,
                        )
                        factory_pair = _decode_address(result)
                    except (BlockScopeError, CounterfactualInputError):
                        reason = "historical canonical factory getPair() call failed"
                    else:
                        if factory_pair.lower() != metadata.pair_address.lower():
                            reason = "canonical factory getPair() returned a different pair"

        provenance = CanonicalV2Provenance(
            established=reason is None,
            chain_id=chain_id,
            canonical_factory_address=CANONICAL_UNISWAP_V2_FACTORY,
            pair_reported_factory_address=actual_factory,
            factory_get_pair_address=factory_pair,
            reason="canonical mainnet factory and getPair() both match" if reason is None else reason,
        )
        self._cache[key] = provenance
        return provenance


def _observed_flow(candidate: SandwichCandidate, victim_index: int) -> tuple[int, int]:
    swap = candidate.victims[victim_index].swap
    if candidate.direction == TOKEN0_TO_TOKEN1:
        return swap.amount0_in, swap.amount1_out
    if candidate.direction == TOKEN1_TO_TOKEN0:
        return swap.amount1_in, swap.amount0_out
    raise CounterfactualInputError("candidate direction is not an ordinary V2 direction")


def _directional_reserves(state: ReserveState, direction: str) -> tuple[int, int]:
    if direction == TOKEN0_TO_TOKEN1:
        return state.reserve0, state.reserve1
    if direction == TOKEN1_TO_TOKEN0:
        return state.reserve1, state.reserve0
    raise CounterfactualInputError("candidate direction is not an ordinary V2 direction")


def _post_state(
    pre: ReserveState,
    direction: str,
    amount_in: int,
    amount_out: int,
) -> ReserveState:
    if direction == TOKEN0_TO_TOKEN1:
        post = ReserveState(pre.reserve0 + amount_in, pre.reserve1 - amount_out)
    elif direction == TOKEN1_TO_TOKEN0:
        post = ReserveState(pre.reserve0 - amount_out, pre.reserve1 + amount_in)
    else:
        raise CounterfactualInputError("candidate direction is not an ordinary V2 direction")
    if post.reserve0 < 0 or post.reserve1 < 0:
        raise CounterfactualInputError("counterfactual output exceeds available reserves")
    return post


def replay_fixed_input_pair_level(
    candidate: SandwichCandidate,
    provenance: CanonicalV2Provenance,
) -> FixedInputCounterfactual:
    """Purely replay observed victim inputs from front.pre under canonical V2 pricing."""
    if not provenance.established:
        return FixedInputCounterfactual(
            candidate,
            provenance,
            CounterfactualStatus.PROVENANCE_UNAVAILABLE,
            (),
            None,
            (),
            None,
            None,
            None,
            provenance.reason,
        )
    initial = candidate.front_run.reserve_context.pre_reserves
    if initial is None:
        raise CounterfactualInputError("front leg has no reconstructed pre-reserves")

    current = initial
    trajectory = [initial]
    executions: list[CounterfactualVictimExecution] = []
    for victim_index, victim in enumerate(candidate.victims):
        observed_pre = victim.reserve_context.pre_reserves
        observed_post = victim.reserve_context.post_reserves
        if observed_pre is None or observed_post is None:
            raise CounterfactualInputError("victim lacks reconstructed reserve evidence")
        amount_in, observed_output = _observed_flow(candidate, victim_index)
        observed_reserve_in, observed_reserve_out = _directional_reserves(
            observed_pre, candidate.direction
        )
        observed_model_quote = canonical_v2_get_amount_out(
            amount_in,
            observed_reserve_in,
            observed_reserve_out,
        )
        counterfactual_reserve_in, counterfactual_reserve_out = _directional_reserves(
            current, candidate.direction
        )
        counterfactual_output = canonical_v2_get_amount_out(
            amount_in,
            counterfactual_reserve_in,
            counterfactual_reserve_out,
        )
        counterfactual_post = _post_state(
            current,
            candidate.direction,
            amount_in,
            counterfactual_output,
        )
        output_delta = counterfactual_output - observed_output
        executions.append(
            CounterfactualVictimExecution(
                victim=victim,
                observed_pre_reserves=observed_pre,
                observed_input=amount_in,
                observed_output=observed_output,
                observed_post_reserves=observed_post,
                observed_model_quote=observed_model_quote,
                observed_model_difference=observed_model_quote - observed_output,
                observed_model_exact_match=observed_model_quote == observed_output,
                counterfactual_pre_reserves=current,
                fixed_input=amount_in,
                counterfactual_output=counterfactual_output,
                counterfactual_post_reserves=counterfactual_post,
                pair_output_delta=output_delta,
                relative_output_improvement=(
                    None if observed_output == 0 else Fraction(output_delta, observed_output)
                ),
            )
        )
        current = counterfactual_post
        trajectory.append(current)

    exact = all(execution.observed_model_exact_match for execution in executions)
    aggregate_observed = sum(execution.observed_output for execution in executions)
    aggregate_counterfactual = sum(
        execution.counterfactual_output for execution in executions
    )
    return FixedInputCounterfactual(
        candidate=candidate,
        provenance=provenance,
        status=(
            CounterfactualStatus.EXACT_OBSERVED_REPRODUCTION
            if exact
            else CounterfactualStatus.MODEL_LIMITED
        ),
        victims=tuple(executions),
        initial_reserves=initial,
        reserve_trajectory=tuple(trajectory),
        aggregate_observed_output=aggregate_observed,
        aggregate_counterfactual_output=aggregate_counterfactual,
        aggregate_pair_output_delta=aggregate_counterfactual - aggregate_observed,
        unavailable_reason=None,
    )


def analyze_fixed_input_counterfactuals(
    rpc: EthereumRPC,
    block_number: int,
    candidates: tuple[SandwichCandidate, ...],
) -> CounterfactualAnalysis:
    """Resolve provenance at the boundary, then run pure candidate replays."""
    resolver = CanonicalProvenanceResolver(rpc, block_number)
    results: list[FixedInputCounterfactual] = []
    invalid_cases = 0
    for candidate in candidates:
        provenance = resolver.pair(candidate.front_run.pair_metadata)
        try:
            result = replay_fixed_input_pair_level(candidate, provenance)
        except CounterfactualInputError as exc:
            invalid_cases += 1
            result = FixedInputCounterfactual(
                candidate,
                provenance,
                CounterfactualStatus.INVALID_INPUT,
                (),
                None,
                (),
                None,
                None,
                None,
                str(exc),
            )
        results.append(result)

    executions = tuple(execution for result in results for execution in result.victims)
    diagnostics = CounterfactualDiagnostics(
        canonical_provenance_established=sum(result.provenance.established for result in results),
        canonical_provenance_unavailable=sum(
            not result.provenance.established for result in results
        ),
        counterfactuals_computed=sum(bool(result.victims) for result in results),
        invalid_reserve_or_input_cases=invalid_cases,
        observed_model_exact_matches=sum(
            execution.observed_model_exact_match for execution in executions
        ),
        observed_model_mismatches=sum(
            not execution.observed_model_exact_match for execution in executions
        ),
    )
    return CounterfactualAnalysis(tuple(results), diagnostics)
