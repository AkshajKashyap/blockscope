# BlockScope

BlockScope is the foundation of a counterfactual Ethereum execution and MEV analysis engine.

## Current status: Milestone 5

BlockScope currently fetches historical Ethereum blocks and transaction receipts over JSON-RPC,
normalizes their transactions and logs into provider-independent typed models, and prints concise
block summaries. It recognizes canonical Uniswap V2-compatible Pair `Swap` and `Sync` event
shapes, conservatively associates their transaction-local evidence, and reconstructs raw reserve
state immediately before and after supported swaps.

BlockScope can also scan those exact reserve transitions for **strict sandwich candidates**. This
is deliberately a high-precision, low-recall structural detector. Candidates are not confirmed
attacks and do not establish intent, mempool visibility, beneficial ownership, or profitability.

For each candidate, BlockScope can report **observed sandwich economics**: exact pair-level outer
and victim Swap flows, actual mined transaction gas expense, actual victim execution ratios, and
reserve-product evidence. These observations are not wallet-level profit or counterfactual loss.

Matching the event signature identifies Uniswap V2-compatible events; it does not prove that a
pair was deployed by the official Uniswap factory. BlockScope queries and displays the pair's
actual `factory()` address without using it as a filter. Generalized MEV classification,
transaction tracing, profit calculation, and counterfactual replay are **not implemented yet**.

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

Scan for strict sandwich candidates with:

```bash
blockscope sandwiches 17000000
blockscope sandwiches 17000000 --limit 20
blockscope sandwiches 17000000 --economics
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

## Strict sandwich-candidate definition

The detector first groups reconstructed swaps by pair and splits them whenever the previous
post-reserves do not exactly equal the next pre-reserves. Inside each continuous segment it uses
a deterministic first-opposite-close rule. A candidate requires:

- A front transaction, one or more distinct victim transactions, and a distinct back transaction.
- The front and back transaction `from` addresses to match after normalization.
- Every victim transaction sender to differ from that outer sender.
- Every victim to trade in the front direction on the same pair.
- The first subsequent opposite-direction swap to be the back leg; the scanner never skips it to
  search for a later matching sender.
- Exact reserve continuity between every adjacent candidate leg.
- A strictly adverse front movement and strictly reversing back movement in the original trade
  direction's raw reserve quote.

For token0 to token1, the raw quote is `reserve1 / reserve0`. For token1 to token0, it is
`reserve0 / reserve1`. Quotes use exact rational arithmetic internally. They are not token-decimal
normalized prices or USD values.

The transaction sender is only a conservative on-chain actor proxy. Equal senders do not prove
common real-world beneficial ownership, and different senders do not prove different ownership.
Multiple logs from one transaction are never treated as distinct transaction-level candidate
legs. Address clustering and internal-call attribution are not implemented.

Every swap in each continuous segment is considered once as a possible front leg. Results are
deduplicated by the complete ordered sequence of `(transaction_index, log_index)` legs and then
sorted by front-leg execution order. The scanner does not search combinatorial subsets of victims.

Strict candidates are evidence-bearing historical patterns, **not confirmed sandwich attacks**.
BlockScope does not yet calculate attacker profit, victim loss, or counterfactual victim output.

## Observed sandwich economics

The optional `--economics` view separates values observed at the candidate pair from claims that
would require transaction-wide flow attribution or counterfactual execution. For an outer cycle
that starts with token A, receives token B, sends B back, and receives A:

```text
gross_outer_leg_cycle_delta_A = back_output_A - front_input_A
intermediate_inventory_delta_B = front_output_B - back_input_B
```

Both are signed raw integers. A positive gross cycle delta means more raw A emerged from the two
observed Pair Swap legs than entered them; it is not automatically actor wallet profit. A positive
intermediate inventory delta means some observed B output was not consumed by the back Swap. A
negative value means the back Swap required B from somewhere not explained by the front Pair Swap.

For each victim, actual raw execution is `observed_output / observed_input`, represented exactly as
a rational number. There is no hypothetical no-front execution and therefore no victim-loss
calculation.

Receipt gas expense is calculated exactly when `effectiveGasPrice` is available:

```text
gas_fee_wei = gas_used * effective_gas_price
```

Front and back gas are reported separately and combined only with each other. Native ETH gas is
not subtracted from token-denominated cycle deltas. In particular, WETH pair flow and native ETH
gas remain distinct observations.

For every candidate Swap, the view reports `k_pre`, `k_post`, and `k_delta`, where
`k = reserve0 * reserve1`. A decrease is surfaced as unusual evidence rather than used to reject a
V2-compatible event. BlockScope does not assume every compatible pair has canonical fee or token
behavior.

Not established by observed economics: transaction-wide balance changes, actor profit, net profit,
intent, beneficial ownership, mempool observation, bundle usage, or counterfactual victim loss.

## Development

Run the offline test suite and linter with:

```bash
pytest
ruff check .
```
