# BlockScope

BlockScope is the foundation of a counterfactual Ethereum execution and MEV analysis engine.

## Current status: Milestone 1

BlockScope currently fetches a historical Ethereum block over JSON-RPC, converts it and its
full transaction objects into provider-independent typed models, and prints a concise summary.

MEV analysis, swap decoding, transaction tracing, and counterfactual replay are **not implemented
yet**.

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

## Development

Run the offline test suite and linter with:

```bash
pytest
ruff check .
```
