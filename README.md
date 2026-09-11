# BlockScope

BlockScope is the foundation of a counterfactual Ethereum execution and MEV analysis engine.

## Current status: Milestone 12

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

For pairs whose canonical Ethereum-mainnet Uniswap V2 provenance can be established, BlockScope
can also compute a **fixed-input pair-level counterfactual**. It removes the front leg's reserve
effect, holds each victim's observed pair input fixed, and quotes the resulting output with the
canonical V2 integer formula. This is an AMM calculation, not arbitrary transaction replay.

BlockScope also has an **observed EVM replay baseline** backed by an external Anvil process. It
replays an unchanged target transaction and the complete preceding block prefix from historical
state, then compares normalized receipt and pair evidence. Its **front-omitted forked-EVM
counterfactual** runs the observed branch and an otherwise identical branch without the detected
front transaction in two independent fresh forks, while leaving the victim request unchanged.
BlockScope can additionally replay the complete observed front/victim/back cycle and retain exact
native ETH and candidate-token balances for a narrow, explicitly defined tracked-address set.
It can trace the locally reproduced front and back transactions, normalize their call trees, and
reconcile realized native-value calls and transaction-wide Transfer-shaped receipt events with
those endpoint balances.

Matching the event signature identifies Uniswap V2-compatible events; it does not prove that a
pair was deployed by the official Uniswap factory. BlockScope queries and displays the pair's
actual `factory()` address without using it as a detector filter. Generalized MEV classification,
counterfactual tracing, wallet-level profit calculation, and arbitrary historical EVM replay are
**not implemented**.

## Architecture

BlockScope keeps normalized Ethereum evidence, protocol semantics, MEV analysis, fork execution,
and presentation as separate concerns. The current pipeline is:

```text
Ethereum RPC
     ↓
normalized blocks, transactions, receipts, and logs
     ↓
Uniswap V2 decoding, metadata, and reserve reconstruction
     ↓
strict sandwich detection
     ↓
observed pair economics
     ↓
canonical fixed-input mathematical counterfactual
     ↓
Anvil observed replay
     ↓
independent front-omitted EVM counterfactual
     ↓
observed full-cycle address attribution
     ↓
observed local-replay call traces and transaction-wide flow reconciliation
```

`types.py` and `rpc.py` own provider-independent Ethereum evidence and upstream access.
`uniswap_v2.py` and `erc20.py` own the narrow protocol/event semantics currently supported.
`sandwiches.py`, `economics.py`, `counterfactual.py`, `evm_counterfactual.py`, and
`observed_attribution.py`, `tracing.py`, and `observed_trace.py` own their specific analyses.
`replay.py` owns the reusable Anvil branch
primitive and explicit receipt/environment evidence. `sandwich_workflow.py` aligns optional
candidate analyses for the CLI without placing analytical policy in presentation code.

Three evidence categories remain explicit:

- Historical evidence is normalized directly from upstream blocks, transactions, receipts, logs,
  and historical calls.
- Derived mathematical evidence uses exact integer/Fraction calculations over historical inputs;
  it does not execute calldata.
- Forked-EVM experimental evidence comes from unchanged or deliberately altered transaction plans
  executed on fresh Anvil forks, with environment and reliability limitations retained.

Within observed attribution, BlockScope also keeps three sources distinct: checkpoint balances are
endpoint-state evidence; receipt logs are event evidence; and local Anvil call traces are
execution-path evidence. Agreement is reported as reconciliation, not treated as proof that any
one source is universally complete.

## Setup

BlockScope requires Python 3.12 or newer. Create a virtual environment and install the package:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Observed EVM replay additionally requires the external Foundry `anvil` executable on `PATH`.
BlockScope never installs it or changes shell configuration. Verify it separately with:

```bash
anvil --version
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
blockscope sandwiches 17000000 --counterfactual
blockscope sandwiches 17000000 --evm-counterfactual
blockscope sandwiches 17000000 --flows
blockscope sandwiches 17000000 --trace-flows
blockscope replay 17000000 1
```

`--counterfactual` also shows the observed economics, so `--economics` is not required alongside
it.

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
The detector alone does not calculate attacker profit, victim loss, or counterfactual output; the
optional fixed-input model described below is a separate, explicitly limited analysis.

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

## Fixed-input pair-level counterfactual

The counterfactual asks one narrow question: if the front leg had not changed the pair reserves,
and every victim supplied the same amount that actually entered the pair, what output would
canonical Uniswap V2 pricing produce? The intervention holds the observed pair input fixed because
Swap logs do not reveal whether the transaction expressed exact-input, exact-output, router,
multi-hop, fee-on-transfer, or custom-contract intent.

Canonical pricing is enabled only when all historical provenance checks succeed on Ethereum
mainnet:

```text
pair.factory() == 0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f
and
canonical_factory.getPair(pair.token0(), pair.token1()) == pair_address
```

The `getPair` call is made at the analyzed block. If either check is unavailable or fails, the
candidate and its observed economics remain visible, but the counterfactual is marked unavailable.
This prevents BlockScope from assigning the canonical 0.30% fee model to an arbitrary
V2-compatible fork.

For positive input and reserves, the pure pricing function uses Python integers and the official
Solidity-style floor operation:

```text
amount_in_with_fee = amount_in * 997
amount_out = (amount_in_with_fee * reserve_out)
             // (reserve_in * 1000 + amount_in_with_fee)
```

Replay begins at the front leg's pre-reserves—not the first victim's observed pre-reserves—and
ends after the last victim. Victims are replayed in execution order, so each sees the preceding
victim's counterfactual post-state. The observed and counterfactual pre/post reserves, fixed input,
outputs, and signed pair-output delta are retained per victim. Aggregates are exact raw output-token
integers; the displayed relative improvement is `pair_output_delta / observed_output` and is kept
as an exact rational internally.

As a model check, BlockScope also applies canonical pricing to each victim's observed pre-state and
observed pair input. Exact agreement with the observed Swap output receives the strongest
categorical model state. A mismatch remains visible and computed but is labeled model-limited,
because a caller may request less than the router's maximum quote.

Counterfactual pair output is not necessarily what a wallet would have received: transfer-tax and
other unusual tokens can make pair and wallet flows differ. This result is not full EVM replay,
does not establish whether the original transaction would still execute, does not replay the back
leg, and is not automatically victim loss. Counterfactual output deltas are never combined with
the outer transactions' gas fees.

## Observed forked-EVM replay

`blockscope replay N V` asks whether transaction index `V` can reproduce its observed receipt and
V2 pair outcome when executed without alteration. The Anvil fork always begins at `N - 1`, because
state at block `N` already includes every transaction in the block. BlockScope selects transactions
`#0` through `#(V-1)` as the complete prefix and queues the prefix plus target in canonical order.
With automatic mining disabled and FIFO ordering requested, one explicit mine places them together
in local block `N`; this preserves the common block number and context rather than spreading them
across unrelated blocks.

Historical senders are submitted with Anvil account impersonation, so original private keys are
not required. BlockScope carries forward the observed recipient, nonce, value, calldata, gas limit,
transaction type, chain ID, access list, and the applicable legacy or EIP-1559 fee fields. It does
not reset nonces, top up balances, substitute Anvil's rich accounts, or write contract storage. A
nonce or balance failure is therefore replay evidence, not something silently bypassed.

The target block number, timestamp, base fee, gas limit, fee recipient, prevrandao, difficulty, and
chain ID are recorded for historical/local comparison. BlockScope attempts Anvil controls for the
fields the backend exposes and reports failed controls and mismatches. The EVM hardfork is inferred
from Ethereum header markers and explicitly pinned (for example, Paris for block `17000000`). This
does not guarantee every header-derived or client-specific execution detail is identical, and such
differences remain visible rather than being folded into an “exact” label.

Impersonated `eth_sendTransaction` does not replay the original signature and may create a different
transaction hash. Both hashes are retained, but hash equality and block-global log indexes are not
reproduction criteria. Receipt logs are compared by address, topics, data, and within-transaction
order after removing local bookkeeping. For the strongest current V2 result,
`pair_execution_exact_match` requires:

```text
same success/revert status
same ordered V2 pair address, direction, and Swap amounts
same adjacent post-Swap Sync reserves
```

Gas usage is compared independently: a gas mismatch remains explicit but does not weaken an
otherwise exact pair-execution comparison. Likewise, target results are not labeled reliable when
any prefix transaction fails the normalized status/log comparison. Receipt equality is useful
evidence, not proof that every unlogged internal state change is identical.

The replay and attribution concepts are intentionally distinct:

- Mathematical V2 replay applies the canonical integer AMM formula to fixed pair-level inputs.
- Observed EVM replay executes the unchanged historical prefix and target in Anvil.
- Front-omitted EVM replay executes the unchanged target after removing one historical transaction.
- Observed full-cycle attribution executes through the unchanged back transaction and reads actual
  address balances; it does not alter history.

Milestone 7 does not skip the front transaction, replay the back leg, alter calldata or state,
calculate counterfactual wallet output, or claim support for arbitrary historical transactions.

## Front-omitted forked-EVM counterfactual

`blockscope sandwiches N --evm-counterfactual` evaluates each displayed strict candidate at its
last victim transaction `V`. It starts two separate Anvil processes from block `N - 1`. The
observed branch submits transactions `#0..#(V-1)` followed by `#V`. The counterfactual branch
submits the same ordered prefix except for the candidate's complete front transaction `F`, then
submits the exact same target request `#V`. For multiple victims, the earlier victims remain in
the prefix; only `F` is omitted. The back transaction is not replayed.

Both branches use fresh forks, the same upstream state, inferred hardfork, requested header
context, historical requests, and one explicit mine. Their fork instance identifiers and backend
process identifiers are retained as isolation evidence. BlockScope does not edit the target's
sender, recipient, nonce, value, calldata, gas limit, transaction type, access list, or fee fields.
It also does not repair a nonce, fund an account, mutate storage, or otherwise force execution.
A counterfactual revert or missing Pair Swap is a valid experimental outcome and is reported
explicitly.

The comparison includes target status, gas used, effective gas price, normalized receipt logs,
whether the candidate Pair Swap occurred, direction, exact pair input/output, post-Swap reserves,
and signed counterfactual-minus-observed output delta when both outputs exist. The fixed-input M6
formula result is shown beside the EVM result as a model comparison; equality is evidence for that
narrow model, not an assumption.

Reliability requires an exact observed replay, independently created branches, equivalent
controllable configuration and prefix requests, an unchanged target request, confirmed omission
of only `F`, and complete counterfactual submission/mining evidence. Anvil does not expose every
historical block-field control on every version: in particular, prevrandao and difficulty can
remain unequal or uncontrollable. Those differences and all failed controls are printed and kept
out of an “equivalent” claim. Even a reliable transaction-level result is not proof of user intent,
wallet receipts, transaction-wide profit or loss, mempool visibility, or causal attribution beyond
this specific front-omission intervention.

## Observed full-cycle address attribution

`blockscope sandwiches N --flows` creates a fresh Anvil fork from `N - 1` and submits every
historical transaction through the candidate's back transaction unchanged and in canonical order.
Front, every victim, and back receipt semantics, V2 Swap/Sync evidence, and gas usage are checked
against history. Attribution is reliable only when those candidate legs and the complete required
prefix reproduce, candidate token identities are known, all checkpoint reads succeed, and the
pending final state agrees with the mined final state.

Transactions remain FIFO-queued and are explicitly mined together once, preserving the historical
one-block execution context. Before-front S0 balances are read from the latest fork state. After
front S1, after-victim S2, and after-back S3 are read from the cumulative `pending` state after the
corresponding transaction is submitted. S3 is read again from `latest` after mining and must match.
This avoids changing block number or timestamp merely to obtain checkpoints.

The tracked-address set is deliberately narrow: the outer transaction sender, front transaction
recipient when present, and back transaction recipient when present, deduplicated without regard
to case. Relationships are displayed literally. A recipient contract is not labeled as owned by
the sender or by a searcher. For each tracked address and checkpoint, BlockScope reads native ETH
with `eth_getBalance` and raw token0/token1 balances with standard `balanceOf` calls against local
Anvil state. Metadata is only for display. Failed reads remain unavailable rather than becoming
fabricated zeroes.

Exact S1-S0, S2-S1, S3-S2, and S3-S0 signed deltas are shown separately for ETH, token0, and
token1. Different assets are never silently netted. The outer sender's front/back native deltas
are also displayed beside actual replay gas expense and transaction `value`; any residual is
evidence of additional native movement, not an inferred bribe, refund, or other causal label.

BlockScope decodes only the standard `Transfer(address,address,uint256)` event shape and retains
candidate-token Transfers from the observed front and back receipts, marking those that touch a
tracked address. Endpoint balances are authoritative for this analysis: non-standard tokens may
omit or misuse Transfer logs, and logs do not expose native internal calls. Pair-level cycle
arithmetic and tracked-address endpoint changes are printed side by side without assuming that
they must reconcile through the limited address set.

Address-level balance evidence is stronger than Pair Swap arithmetic because it observes actual
EVM state for named addresses. It is still not beneficial-owner profit: BlockScope does not infer
common ownership, attribute all intermediate addresses, trace internal calls, or convert between
ETH and tokens. The historical back transaction is intentionally not executed in the
front-omitted counterfactual branch. Because front and back share a sender, omitting the front can
remove the nonce predecessor required by the unchanged historical back; BlockScope does not rewrite
the nonce, insert a dummy transaction, or otherwise force execution.

## Observed transaction-wide trace attribution

`blockscope sandwiches N --trace-flows` implies the observed full-cycle checkpoint analysis and
then traces the locally replayed front and back transactions before their Anvil fork is closed. It
never requires the upstream provider to expose historical debug tracing. Historical hashes and
local replay hashes are retained separately.

The preferred Anvil request is:

```text
debug_traceTransaction(local_replay_hash, {"tracer": "callTracer"})
```

Anvil 1.8.1 returns a directly nested call tree for this request. If that tracer RPC is unavailable,
BlockScope narrowly falls back to Anvil's `trace_transaction(local_replay_hash)` response and uses
its explicit `traceAddress` hierarchy. It does not infer a call tree from opcode `structLogs`.
Malformed responses fail normalization instead of being silently reinterpreted.

Every normalized immutable frame retains a deterministic path (`root`, `root/0`, ...), call type,
caller, code target, execution context, exact value, bounded input/output evidence, gas fields when
available, error evidence, and ordered children. CLI output shows only the four-byte selector and
a small verified local label set rather than dumping calldata. Unknown selectors and call types
remain unknown. For `DELEGATECALL` and `CALLCODE`, the code target remains distinct from the
inherited execution context; the target is never described as the account holding the caller's
balances or storage.

Realized native-flow edges include a successful top-level transaction value exactly once,
successful value-bearing `CALL`, `CREATE`/`CREATE2` endowments, and `SELFDESTRUCT` transfers when
the selected backend trace explicitly provides them. Zero-value calls, `STATICCALL`,
`DELEGATECALL`, `CALLCODE`, errored frames, and every descendant of a reverted parent contribute no
realized edge. Gas is reconciled separately because it is not an ordinary EVM call. Trace-native
coverage does not necessarily expose every SELFDESTRUCT-style balance movement, so checkpoint
balances remain authoritative and any residual is shown exactly.

Front and back receipts are also scanned for every structurally valid
`Transfer(address,address,uint256)` shape, regardless of emitter. These are called
"Transfer-shaped" events because the shape alone does not prove standard ERC-20 behavior.
Emitter `symbol()` and `decimals()` metadata are best effort and never suppress raw evidence.
Nonzero `from`/`to` addresses form a neutral token-flow participant set; zero-address mint/burn
shapes remain explicit. Candidate pair assets are separated from additional observed emitters.

A transaction trace is historically attributable only when its local replay matches status,
normalized receipt semantics, candidate V2 Swap execution, and post-Sync reserves under the
existing reliability rules. Reverted subcalls stay visible as execution evidence but their rolled
back effects are excluded from flow accounting. The trace tree can explain how calls led to
receipt events and endpoint changes, but it cannot establish address ownership, beneficial-owner
profit, intent, or economic significance. Counterfactual branches and the historical-back nonce
problem remain outside this tracing milestone.

## Multi-candidate corpus evaluation

Corpus evaluation is a separate reproducibility tool rather than part of the normal `sandwiches`
workflow:

```bash
python scripts/evaluate_corpus.py \
  --start-block 17000001 \
  --end-block 17000008 \
  --candidate-limit 50 \
  --evm-limit 5 \
  --evm-cooldown-seconds 90
```

The inclusive block range is fixed before outcomes are inspected. Blocks are scanned in ascending
order, and candidates are ordered by block, front transaction index/log index, and back execution
position. Candidate and EVM limits select the first records in that order only; provenance,
mathematical outputs, replay behavior, and apparent outcome never influence selection. Every
strict candidate is still counted and serialized, including candidates beyond an expensive-stage
limit. The golden block `17000000` remains separate unless it naturally occurs in an explicitly
chosen range.

Evaluation is staged:

```text
block/receipt/V2 scan
  → strict candidate detection
  → canonical provenance and fixed-input mathematics
  → first-N observed/front-omitted EVM experiments
  → exact math/EVM comparison
```

No Anvil process is launched for a block without a candidate selected for EVM validation. Trace
attribution is recorded as not attempted and remains disabled for this initial corpus tool. The
existing two-fork EVM API reports combined observed/counterfactual timing; splitting that timer
would require invasive instrumentation, so the artifact labels it explicitly rather than
inventing separate measurements.

The JSON artifact defaults to `artifacts/evaluation_<start>_<end>.json`. It records the inclusive
range, limits, selection rule, UTC creation time, BlockScope version, Git commit/dirty state, Anvil
version, per-block hardfork selection, request counts, block diagnostics, candidate outcomes,
failure taxonomy, and mean/median stage timings. It contains no RPC URL, raw block, raw receipt, or
raw trace payload. Writes are atomic at completion; this initial bounded runner does not implement
resume/checkpoint machinery.

RPC request counts cover direct BlockScope upstream calls. Anvil's internal fork-provider requests
are not observable through this narrow wrapper and are explicitly excluded rather than estimated.
The initial range was preregistered as `17000001–17000050`; the public endpoint returned HTTP 429
for 33/50 blocks and all expensive experiments. Following the predefined rate-limit policy, the
completed study uses the deterministic first eight-block prefix, `17000001–17000008`, without
selecting blocks or candidates by outcome. Its configuration records a fixed 90-second cooldown
between scanning and EVM execution so Anvil begins after the public endpoint's request window has
recovered; the cooldown is operational configuration, not analytical selection.

Provider, Anvil, supported-input, and replay operational failures become bounded typed outcomes so
one bad case need not discard the corpus. Unexpected exceptions and invariant violations still
fail loudly. Reports always retain numerators and denominators and describe only the selected
evaluation range, not Ethereum MEV generally.

The initial completed corpus result is:

```text
range:                         17000001–17000008
blocks completed:             8/8
transactions / receipts:      819 / 819
V2-compatible swaps:          88 across 52 pairs
strict candidates:            3 (all single-victim, all canonical)
observed replay exact:         3/3 attempted
reliable front-omitted EVM:   3/3 attempted
victim success -> revert:      0
candidate-pair Swap absent:    0
pair input unchanged/changed: 2/1
math/EVM exact:                2/3 comparisons
```

The nonzero comparison is candidate 3 in block `17000008`. Its observed pair input was
`809676432957450567`; without the front transaction the unchanged victim call supplied
`777864049811781303` and still received exactly `10000000000000000`. The fixed-input model instead
predicted `10373772338413816`, so exact EVM output minus mathematical output was
`-373772338413816`. This demonstrates empirically that fixed pair input is not a universal
transaction-level assumption. The behavior is compatible with exact-output-style execution, but
BlockScope does not infer user intent from it.

This eight-block evaluation is evidence about only this selected range. It contains no
multi-victim or noncanonical candidate and is not representative evidence for Ethereum MEV as a
whole. The machine-readable result is
`artifacts/evaluation_17000001_17000008.json`; the earlier 50- and 10-block attempts are retained as
explicit HTTP-429 failure artifacts.

## Development

Run the offline test suite and linter with:

```bash
pytest
ruff check .
```

The block `17000000` golden fixture is opt-in because it requires an archive RPC endpoint and
Anvil. It is never part of normal offline `pytest`:

```bash
export PATH="$HOME/.foundry/bin:$PATH"
export ETH_RPC_URL=https://your-provider.example
.venv/bin/python scripts/verify_golden_fixture.py
```

The verifier checks candidate detection, observed reproduction, mathematical and EVM
counterfactual agreement, observed full-cycle attribution, and stable high-level trace
reconciliation facts without placing fixture constants in production analysis code.
