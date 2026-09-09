# BlockScope

BlockScope is the foundation of a counterfactual Ethereum execution and MEV analysis engine.

## Current status: Milestone 3

BlockScope currently fetches historical Ethereum blocks and transaction receipts over JSON-RPC,
normalizes their transactions and logs into provider-independent typed models, and prints concise
block summaries. It recognizes canonical Uniswap V2-compatible Pair `Swap` and `Sync` event
shapes, conservatively associates their transaction-local evidence, and reconstructs raw reserve
state immediately before and after supported swaps.

Matching the event signature identifies Uniswap V2-compatible events; it does not prove that a
pair was deployed by the official Uniswap factory. BlockScope queries and displays the pair's
actual `factory()` address without using it as a filter. MEV analysis, transaction tracing, profit
calculation, and counterfactual replay are **not implemented yet**.

## Setup

BlockScope requires Python 3.12 or newer. Create a virtual environment and install the package:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Configure a standard environment variable with an Ethereum JSON-RPC endpoint. BlockScope does
not automatically read `.env` files.

```bash
export ETH_RPC_URL=https://your-provider.example
```

Then inspect a block:

```bash
blockscope block 17000000
blockscope block 17000000 --limit 20
```

All transactions are fetched so the block model is complete; `--limit` only controls how many
are printed. Calldata is retained in the transaction model but is not printed by default.

Decode supported Swap events from every transaction receipt in a block with:

```bash
blockscope swaps 17000000
blockscope swaps 17000000 --limit 50
```

The decoder supports exactly these canonical event shapes:

```solidity
event Swap(
    address indexed sender,
    uint amount0In,
    uint amount1In,
    uint amount0Out,
    uint amount1Out,
    address indexed to
);

event Sync(uint112 reserve0, uint112 reserve1);
```

For reserve reconstruction, the `Sync` must be the raw receipt log immediately before the `Swap`,
and both logs must have the same pair address, transaction hash, and transaction index. Its
reserves are treated as post-swap reserves, following canonical Pair event ordering. Pre-swap
reserves are reconstructed exactly as:

```text
pre_reserve0 = post_reserve0 - amount0_in + amount0_out
pre_reserve1 = post_reserve1 - amount1_in + amount1_out
```

No `getReserves()` call is used for transaction-position state. Missing, mismatched, malformed,
or arithmetically invalid evidence is reported as unavailable rather than fabricated.

Pair `token0()`, `token1()`, and `factory()` calls and best-effort token `decimals()` and `symbol()`
calls are made against the analyzed historical block. Results are cached in memory for that
command. Metadata failures are reported but do not invalidate event-derived reserve state.

Amounts and reserves are always retained and displayed as raw integer token units. Symbols and
decimals are display metadata only. Multiple Swap logs emitted by one transaction remain separate
and retain `(transaction_index, log_index)` execution order.

## Development

Run the offline test suite and linter with:

```bash
pytest
ruff check .
```
