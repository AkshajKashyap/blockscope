"""Command-line interface for BlockScope."""

from datetime import UTC, datetime
from typing import Annotated

import typer

from blockscope.rpc import BlockScopeError, EthereumRPC
from blockscope.types import Transaction

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


if __name__ == "__main__":
    app()
