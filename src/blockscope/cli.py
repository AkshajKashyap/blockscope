"""Command-line interface for BlockScope."""

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Annotated

import typer

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
) -> None:
    """Display conservative strict sandwich candidates in a block."""
    try:
        swap_analysis = analyze_block_swaps(EthereumRPC.from_env(), number)
        result = detect_strict_sandwich_candidates(swap_analysis.swaps)
    except (BlockScopeError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Strict Sandwich Candidates — Ethereum Block {number}\n")
    for index, candidate in enumerate(result.candidates[:limit], start=1):
        for line in _candidate_lines(index, candidate):
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


if __name__ == "__main__":
    app()
