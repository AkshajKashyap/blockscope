"""Command-line interface for BlockScope."""

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Annotated

import typer

from blockscope.counterfactual import (
    FixedInputCounterfactual,
    analyze_fixed_input_counterfactuals,
)
from blockscope.economics import (
    ObservedAmount,
    ObservedAsset,
    ObservedSandwichEconomics,
    analyze_observed_sandwich_economics,
)
from blockscope.rpc import BlockScopeError, EthereumRPC
from blockscope.sandwiches import SandwichCandidate, detect_strict_sandwich_candidates
from blockscope.types import Transaction
from blockscope.uniswap_v2 import (
    EnrichedUniswapV2Swap,
    TokenMetadata,
    analyze_block_swaps,
)

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Inspect Ethereum blocks for future counterfactual analysis."""


def _short(value: str, length: int = 12) -> str:
    return value if len(value) <= length else f"{value[:length]}…"


def _transaction_line(transaction: Transaction) -> str:
    destination = (
        _short(transaction.to_address)
        if transaction.to_address is not None
        else "CONTRACT_CREATION"
    )
    return (
        f"#{transaction.transaction_index:<4} "
        f"{_short(transaction.hash):<13} "
        f"{_short(transaction.from_address)} -> {destination} "
        f"value={transaction.value} wei"
    )


def _token_label(metadata: TokenMetadata | None) -> str:
    if metadata is None:
        return "unavailable"
    symbol = metadata.symbol or "symbol unavailable"
    decimals = "?" if metadata.decimals is None else str(metadata.decimals)
    return f"{symbol} {_short(metadata.address)} decimals={decimals}"


def _swap_lines(enriched: EnrichedUniswapV2Swap) -> tuple[str, ...]:
    swap = enriched.swap
    context = enriched.reserve_context
    if context.pre_reserves is not None and context.post_reserves is not None:
        reserves = (
            f"reserves pre=({context.pre_reserves.reserve0}, {context.pre_reserves.reserve1}) "
            f"post=({context.post_reserves.reserve0}, {context.post_reserves.reserve1})"
        )
    else:
        reserves = "reserves unavailable (no valid immediately preceding same-pair Sync)"
    factory = enriched.pair_metadata.factory_address or "unavailable"
    return (
        f"Tx #{swap.transaction_index} / log {swap.log_index}  Pair: {swap.pair_address}",
        (
            f"  {swap.direction}  in0={swap.amount0_in} in1={swap.amount1_in} "
            f"out0={swap.amount0_out} out1={swap.amount1_out}"
        ),
        f"  {reserves}",
        f"  factory={factory}  token0={_token_label(enriched.token0_metadata)}",
        f"  token1={_token_label(enriched.token1_metadata)}",
    )


def _fraction_decimal(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = 18
        return format(Decimal(value.numerator) / Decimal(value.denominator), ".12g")


def _candidate_lines(index: int, candidate: SandwichCandidate) -> tuple[str, ...]:
    victim_lines = tuple(
        f"  Victim {victim_index}: tx #{victim.swap.transaction_index} "
        f"sender={victim.transaction_sender} {victim.swap.direction}"
        for victim_index, victim in enumerate(candidate.victims, start=1)
    )
    return (
        f"Candidate {index}",
        f"  Pair: {candidate.pair_address}",
        f"  Outer transaction sender: {candidate.actor_address}",
        f"  Front: tx #{candidate.front_run.swap.transaction_index} {candidate.direction}",
        *victim_lines,
        (
            f"  Back: tx #{candidate.back_run.swap.transaction_index} "
            f"{candidate.back_run.swap.direction}"
        ),
        "  Raw directional quote:",
        f"    before front: {_fraction_decimal(candidate.quote_before_front)}",
        f"    after front:  {_fraction_decimal(candidate.quote_after_front)}",
        f"    before back:  {_fraction_decimal(candidate.quote_before_back)}",
        f"    after back:   {_fraction_decimal(candidate.quote_after_back)}",
        (
            "  Evidence: continuous pair state; same outer transaction sender; "
            "distinct victim sender(s); adverse front movement; reversing back movement"
        ),
    )


def _asset_label(asset: ObservedAsset) -> str:
    identity = asset.symbol or asset.pair_position
    address = asset.address or "address unavailable"
    decimals = "?" if asset.decimals is None else str(asset.decimals)
    return f"{identity} ({address}, decimals={decimals})"


def _observed_amount(amount: ObservedAmount, *, signed: bool = False) -> str:
    raw = f"{amount.raw_amount:+d}" if signed else str(amount.raw_amount)
    return f"{raw} raw {_asset_label(amount.asset)}"


def _gas_fee(value: int | None) -> str:
    return "unavailable" if value is None else f"{value} wei"


def _exact_decimal_amount(raw_amount: int, decimals: int | None) -> str | None:
    if decimals is None:
        return None
    with localcontext() as context:
        context.prec = max(28, len(str(abs(raw_amount))) + decimals + 2)
        return format(Decimal(raw_amount).scaleb(-decimals), "f")


def _economics_lines(economics: ObservedSandwichEconomics) -> tuple[str, ...]:
    victim_lines: list[str] = []
    for index, victim in enumerate(economics.victim_executions, start=1):
        ratio = (
            "unavailable (zero/invalid input)"
            if victim.actual_raw_execution_ratio is None
            else _fraction_decimal(victim.actual_raw_execution_ratio)
        )
        victim_lines.extend(
            (
                f"  Victim {index} actual input:  {_observed_amount(victim.input_amount)}",
                f"  Victim {index} actual output: {_observed_amount(victim.output_amount)}",
                f"  Victim {index} actual raw execution ratio: {ratio}",
                f"  Victim {index} gas fee: {_gas_fee(victim.gas_fee_wei)}",
                (
                    f"  Victim {index} invariant: k_pre={victim.invariant.k_pre} "
                    f"k_post={victim.invariant.k_post} k_delta={victim.invariant.k_delta:+d}"
                ),
            )
        )
    invariant_lines = tuple(
        (
            f"  Outer {label} invariant: k_pre={invariant.k_pre} "
            f"k_post={invariant.k_post} k_delta={invariant.k_delta:+d}"
        )
        for label, invariant in zip(("front", "back"), economics.outer_invariants, strict=True)
    )
    return (
        "Observed outer pair cycle",
        f"  Front spent:    {_observed_amount(economics.front_input)}",
        f"  Front received: {_observed_amount(economics.front_output)}",
        f"  Back spent:     {_observed_amount(economics.back_input)}",
        f"  Back received:  {_observed_amount(economics.back_output)}",
        (
            "  Gross outer-leg cycle delta: "
            f"{_observed_amount(economics.gross_cycle_delta, signed=True)}"
        ),
        (
            "  Intermediate inventory delta: "
            f"{_observed_amount(economics.intermediate_inventory_delta, signed=True)}"
        ),
        "Outer transaction gas expenditure (native ETH, reported in wei)",
        f"  Front: {_gas_fee(economics.front_gas_fee_wei)}",
        f"  Back:  {_gas_fee(economics.back_gas_fee_wei)}",
        f"  Total: {_gas_fee(economics.total_outer_gas_fee_wei)}",
        "Victim actual execution",
        *victim_lines,
        (
            "  Aggregate victim input:  "
            f"{_observed_amount(economics.aggregate_victim_input)}"
        ),
        (
            "  Aggregate victim output: "
            f"{_observed_amount(economics.aggregate_victim_output)}"
        ),
        *invariant_lines,
        "Important: gross pair-level cycle delta is not wallet-level profit.",
        "Important: observed economics alone do not calculate victim counterfactual loss.",
    )


def _counterfactual_lines(result: FixedInputCounterfactual) -> tuple[str, ...]:
    provenance = result.provenance
    lines = [
        "Fixed-input Pair-Level Counterfactual",
        f"  Model state: {result.status.value}",
        f"  Ethereum chain ID: {provenance.chain_id}",
        f"  Canonical Uniswap V2 provenance: {'yes' if provenance.established else 'no'}",
        f"  Pair-reported factory: {provenance.pair_reported_factory_address}",
        f"  Canonical factory getPair result: {provenance.factory_get_pair_address}",
        "  Intervention: remove front reserve effect; hold victim pair input fixed",
    ]
    if not result.victims:
        lines.append(f"  Unavailable reason: {result.unavailable_reason}")
        return tuple(lines)

    direction = result.candidate.direction
    if direction == "token0 -> token1":
        metadata = result.candidate.front_run.token1_metadata
        output_position = "token1"
    else:
        metadata = result.candidate.front_run.token0_metadata
        output_position = "token0"
    output_identity = (
        output_position
        if metadata is None
        else f"{metadata.symbol or output_position} ({metadata.address})"
    )
    decimals = None if metadata is None else metadata.decimals
    for index, execution in enumerate(result.victims, start=1):
        normalized_delta = _exact_decimal_amount(execution.pair_output_delta, decimals)
        delta_display = f"{execution.pair_output_delta:+d} raw {output_identity}"
        if normalized_delta is not None:
            delta_display += f" ({normalized_delta} decimal-normalized)"
        relative = (
            "unavailable"
            if execution.relative_output_improvement is None
            else _fraction_decimal(execution.relative_output_improvement)
        )
        lines.extend(
            (
                f"  Victim {index}: tx #{execution.victim.swap.transaction_index}",
                (
                    "    Observed: input="
                    f"{execution.observed_input} output={execution.observed_output} "
                    f"pre=({execution.observed_pre_reserves.reserve0}, "
                    f"{execution.observed_pre_reserves.reserve1}) "
                    f"post=({execution.observed_post_reserves.reserve0}, "
                    f"{execution.observed_post_reserves.reserve1})"
                ),
                (
                    "    Counterfactual: fixed_input="
                    f"{execution.fixed_input} output={execution.counterfactual_output} "
                    f"pre=({execution.counterfactual_pre_reserves.reserve0}, "
                    f"{execution.counterfactual_pre_reserves.reserve1}) "
                    f"post=({execution.counterfactual_post_reserves.reserve0}, "
                    f"{execution.counterfactual_post_reserves.reserve1})"
                ),
                f"    Counterfactual pair-output delta: {delta_display}",
                f"    Relative output improvement (delta/observed output): {relative}",
                (
                    "    Observed-model validation: canonical maximum="
                    f"{execution.observed_model_quote} observed={execution.observed_output} "
                    f"difference={execution.observed_model_difference:+d} "
                    f"exact={'yes' if execution.observed_model_exact_match else 'no'}"
                ),
            )
        )
    lines.extend(
        (
            f"  Aggregate observed output: {result.aggregate_observed_output}",
            f"  Aggregate counterfactual output: {result.aggregate_counterfactual_output}",
            f"  Aggregate pair-output delta: {result.aggregate_pair_output_delta:+d}",
            "  Replay stops after the final victim; the back leg is not replayed.",
            "  Important: this is fixed-input pair modeling, not full EVM replay.",
            "  Important: this does not prove what the victim wallet would have received.",
        )
    )
    return tuple(lines)


@app.command("block")
def show_block(
    number: Annotated[int, typer.Argument(min=0, help="Ethereum block number")],
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", min=0, help="Maximum transactions to display"),
    ] = 20,
) -> None:
    """Fetch and display a historical Ethereum block."""
    try:
        block = EthereumRPC.from_env().get_block(number)
    except (BlockScopeError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    timestamp = datetime.fromtimestamp(block.timestamp, tz=UTC).isoformat()
    typer.echo(f"Ethereum Block {block.number}")
    typer.echo(f"Hash: {block.hash}")
    typer.echo(f"Timestamp: {timestamp}")
    typer.echo(f"Transactions: {len(block.transactions)}")
    typer.echo(f"Gas used: {block.gas_used:,} / {block.gas_limit:,}")
    if block.base_fee_per_gas is not None:
        typer.echo(f"Base fee: {block.base_fee_per_gas:,} wei")

    typer.echo("\nTransactions")
    for transaction in block.transactions[:limit]:
        typer.echo(_transaction_line(transaction))
    remaining = len(block.transactions) - limit
    if remaining > 0:
        typer.echo(f"… {remaining} more transaction(s); use --limit to display more")


@app.command("swaps")
def show_swaps(
    number: Annotated[int, typer.Argument(min=0, help="Ethereum block number")],
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", min=0, help="Maximum swap events to display"),
    ] = 50,
) -> None:
    """Display Uniswap V2-compatible Pair Swap events in a block."""
    try:
        analysis = analyze_block_swaps(EthereumRPC.from_env(), number)
    except (BlockScopeError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Uniswap V2-compatible Pair Swap Events — Ethereum Block {number}\n")
    for enriched in analysis.swaps[:limit]:
        for line in _swap_lines(enriched):
            typer.echo(line)
        typer.echo()
    remaining = len(analysis.swaps) - limit
    if remaining > 0:
        typer.echo(f"… {remaining} more event(s); use --limit to display more")
    diagnostics = analysis.diagnostics
    typer.echo("Diagnostics")
    typer.echo(f"Valid Swap events: {diagnostics.valid_swaps}")
    typer.echo(
        "Reserve state reconstructed: "
        f"{diagnostics.swaps_with_reconstructed_reserves}"
    )
    typer.echo(
        "Reserve state unavailable: "
        f"{diagnostics.swaps_without_reconstructed_reserves}"
    )
    typer.echo(f"Malformed matching Swap logs: {diagnostics.malformed_swap_logs}")
    typer.echo(f"Malformed matching Sync logs: {diagnostics.malformed_sync_logs}")
    typer.echo(
        "Invalid reserve reconstructions: "
        f"{diagnostics.invalid_reserve_reconstructions}"
    )
    typer.echo(f"Metadata lookup failures: {diagnostics.metadata_lookup_failures}")


@app.command("sandwiches")
def show_sandwiches(
    number: Annotated[int, typer.Argument(min=0, help="Ethereum block number")],
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", min=0, help="Maximum candidates to display"),
    ] = 20,
    economics: Annotated[
        bool,
        typer.Option("--economics", help="Show observed pair-level economics"),
    ] = False,
    counterfactual: Annotated[
        bool,
        typer.Option(
            "--counterfactual",
            help="Show canonical fixed-input pair-level victim replay",
        ),
    ] = False,
) -> None:
    """Display conservative strict sandwich candidates in a block."""
    try:
        rpc = EthereumRPC.from_env()
        swap_analysis = analyze_block_swaps(rpc, number)
        result = detect_strict_sandwich_candidates(swap_analysis.swaps)
        economics_analysis = (
            analyze_observed_sandwich_economics(result.candidates, swap_analysis.receipts)
            if economics or counterfactual
            else None
        )
        counterfactual_analysis = (
            analyze_fixed_input_counterfactuals(rpc, number, result.candidates)
            if counterfactual
            else None
        )
    except (BlockScopeError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Strict Sandwich Candidates — Ethereum Block {number}\n")
    for index, candidate in enumerate(result.candidates[:limit], start=1):
        for line in _candidate_lines(index, candidate):
            typer.echo(line)
        if economics_analysis is not None:
            for line in _economics_lines(economics_analysis.candidates[index - 1]):
                typer.echo(line)
        if counterfactual_analysis is not None:
            for line in _counterfactual_lines(counterfactual_analysis.candidates[index - 1]):
                typer.echo(line)
        typer.echo()
    remaining = len(result.candidates) - limit
    if remaining > 0:
        typer.echo(f"… {remaining} more candidate(s); use --limit to display more")
    typer.echo(f"{len(result.candidates)} strict sandwich candidate(s)\n")

    diagnostics = result.diagnostics
    typer.echo("Detector diagnostics")
    typer.echo(f"Continuous pair segments: {diagnostics.continuous_pair_segments}")
    typer.echo(f"Potential front legs considered: {diagnostics.potential_front_legs_considered}")
    typer.echo(f"Rejected — no victims: {diagnostics.rejected_no_victims}")
    typer.echo(
        "Rejected — no closing back-run: "
        f"{diagnostics.rejected_no_closing_back_run}"
    )
    typer.echo(
        "Rejected — invalid transaction-level leg sequence: "
        f"{diagnostics.rejected_invalid_leg_sequence}"
    )
    typer.echo(
        "Rejected — closing sender mismatch: "
        f"{diagnostics.rejected_closing_sender_mismatch}"
    )
    typer.echo(f"Rejected — invalid raw quote: {diagnostics.rejected_invalid_quote}")
    typer.echo(f"Rejected — front not adverse: {diagnostics.rejected_adverse_movement}")
    typer.echo(
        "Rejected — back did not improve quote: "
        f"{diagnostics.rejected_back_run_reversal}"
    )
    typer.echo(f"Duplicate candidates suppressed: {diagnostics.duplicate_candidates_suppressed}")
    if economics_analysis is not None:
        economics_diagnostics = economics_analysis.diagnostics
        typer.echo("\nObserved-economics diagnostics")
        typer.echo(f"Economics calculated: {economics_diagnostics.economics_calculated}")
        typer.echo(
            "Missing receipt gas data: "
            f"{economics_diagnostics.missing_receipt_gas_data}"
        )
        typer.echo(
            "Invalid or zero victim inputs: "
            f"{economics_diagnostics.invalid_or_zero_victim_inputs}"
        )
        typer.echo(
            "Unusual invariant decreases: "
            f"{economics_diagnostics.unusual_invariant_decreases}"
        )
        typer.echo(
            f"Missing token metadata: {economics_diagnostics.missing_token_metadata}"
        )
    if counterfactual_analysis is not None:
        counterfactual_diagnostics = counterfactual_analysis.diagnostics
        typer.echo("\nCounterfactual diagnostics")
        typer.echo(
            "Canonical provenance established: "
            f"{counterfactual_diagnostics.canonical_provenance_established}"
        )
        typer.echo(
            "Canonical provenance unavailable: "
            f"{counterfactual_diagnostics.canonical_provenance_unavailable}"
        )
        typer.echo(
            f"Counterfactuals computed: {counterfactual_diagnostics.counterfactuals_computed}"
        )
        typer.echo(
            "Invalid reserve/input cases: "
            f"{counterfactual_diagnostics.invalid_reserve_or_input_cases}"
        )
        typer.echo(
            "Observed-model exact matches: "
            f"{counterfactual_diagnostics.observed_model_exact_matches}"
        )
        typer.echo(
            "Observed-model mismatches: "
            f"{counterfactual_diagnostics.observed_model_mismatches}"
        )


if __name__ == "__main__":
    app()
