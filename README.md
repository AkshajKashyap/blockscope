# BlockScope

BlockScope is the foundation of a counterfactual Ethereum execution and MEV analysis engine.

## Current status: Milestone 2

BlockScope currently fetches historical Ethereum blocks and transaction receipts over JSON-RPC,
normalizes their transactions and logs into provider-independent typed models, and prints concise
block summaries. It also recognizes and decodes logs with the canonical Uniswap V2 Pair `Swap`
event shape.

Matching the event signature identifies Uniswap V2-compatible events; it does not prove that a
pair was deployed by the official Uniswap factory. Token metadata, token decimals, MEV analysis,
transaction tracing, and counterfactual replay are **not implemented yet**.

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

The decoder supports exactly this canonical event shape:

```solidity
event Swap(
    address indexed sender,
    uint amount0In,
    uint amount1In,
    uint amount0Out,
    uint amount1Out,
    address indexed to
);
```

Amounts are displayed as raw integer token units. BlockScope does not currently look up token
symbols or decimals, and it preserves multiple Swap logs emitted by a single transaction.

## Development

Run the offline test suite and linter with:

```bash
pytest
ruff check .
```
