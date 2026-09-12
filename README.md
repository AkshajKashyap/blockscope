# BlockScope

BlockScope is an Ethereum execution-analysis engine that detects conservative Uniswap V2
sandwich candidates, reproduces their historical execution in a forked EVM, and runs controlled
front-omission counterfactual experiments.

Most sandwich analyses stop at event patterns or AMM arithmetic. BlockScope keeps those useful
signals, then asks the harder question: **does the unchanged historical victim transaction still
execute when the suspected front transaction is removed from an otherwise equivalent forked
history?** The answer is reported with replay evidence and limitations, not promoted into a claim
about intent or wallet-level loss.

BlockScope `v0.1.0` is an alpha research tool. Its supported surface is deliberately narrow:
Ethereum mainnet history, Uniswap V2-compatible `Swap`/`Sync` evidence, canonical-factory
fixed-input mathematics, and Anvil-backed replay.

## What it does

- Fetches and normalizes historical blocks, transactions, receipts, and logs through JSON-RPC.
- Decodes Uniswap V2-compatible Pair events and reconstructs transaction-position reserve state.
- Detects strict, high-precision sandwich-shaped transaction sequences.
- Reports observed pair economics without calling them wallet profit or victim loss.
- Verifies canonical Uniswap V2 provenance before applying the 0.30% integer pricing formula.
- Reproduces observed transactions with their complete block prefix on an Anvil fork.
- Executes the same victim request on independent observed and front-omitted forks.
- Optionally attributes observed full-cycle endpoint balances and reconciles local call traces.
- Produces deterministic JSON corpus artifacts with diagnostics, timings, and failure taxonomy.

## Architecture

```text
Ethereum JSON-RPC
        │
        ▼
normalized block + receipt evidence
        │
        ▼
V2 decoding + reserve reconstruction
        │
        ▼
strict candidate detector
        │
        ├── observed pair economics
        ├── canonical fixed-input AMM model
        └── forked execution
              ├── observed replay
              ├── front-omitted counterfactual
              └── optional balance/trace reconciliation
```

The modules follow those boundaries: `rpc.py` and `types.py` own upstream and normalized evidence;
`uniswap_v2.py` and `erc20.py` own event/protocol semantics; the analysis modules each own one
interpretation; `replay.py` owns the reusable Anvil primitive; and `cli.py` only presents results.

## Quick start

BlockScope requires Python 3.12 or newer.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
export ETH_RPC_URL=https://your-provider.example
.venv/bin/blockscope sandwiches 17000000 --counterfactual
```

Anvil is required only for `replay`, `--evm-counterfactual`, `--flows`, `--trace-flows`, the live
golden verifier, and EVM-enabled corpus evaluation. Install Foundry separately and check:

```bash
anvil --version
```

BlockScope does not load `.env` automatically. Public RPC endpoints often rate-limit receipt-heavy
or fork-backed workloads; use an archive-capable endpoint appropriate for historical state and
expect provider-specific quotas. RPC URLs are redacted from stored artifacts and user-facing
errors.

## CLI examples

```bash
# Historical evidence
.venv/bin/blockscope block 17000000
.venv/bin/blockscope swaps 17000000 --limit 50
.venv/bin/blockscope sandwiches 17000000 --economics

# Pair-level model and forked execution
.venv/bin/blockscope sandwiches 17000000 --counterfactual
.venv/bin/blockscope replay 17000000 1
.venv/bin/blockscope sandwiches 17000000 --evm-counterfactual

# Observed cycle attribution and trace reconciliation
.venv/bin/blockscope sandwiches 17000000 --flows
.venv/bin/blockscope sandwiches 17000000 --trace-flows
```

`--counterfactual` includes observed economics. `--evm-counterfactual`, `--flows`, and
`--trace-flows` each invoke the analyses implied by their prerequisites. Run `blockscope --help`
or a command's `--help` for the stable option surface.

## Golden execution result: block 17000000

The opt-in golden fixture covers the complete supported pipeline on one historical candidate:

- observed victim pair flow: **0.19475224366 WETH → approximately 348,906.54221 TRUMP**;
- same victim transaction with only the front transaction omitted: **approximately
  401,242.21485 TRUMP** at the same observed pair input;
- EVM pair-output difference: **approximately 52,335.67264 TRUMP**;
- canonical fixed-input mathematical output: **exactly equal** to the front-omitted EVM output;
- observed full-cycle recipient-contract balance delta: **+21,983,022,503,039,222 raw WETH** and
  **0 raw TRUMP**.

Exact raw outputs are `348906542210008017802287` observed,
`401242214846052323951351` front-omitted, and `52335672636044306149064` difference. Token symbols
and decimals are historical display metadata; all comparisons retain raw integers.

Reproduce the live assertion suite with:

```bash
ETH_RPC_URL=https://your-provider.example .venv/bin/python scripts/verify_golden_fixture.py
```

This fixture demonstrates why execution matters: the unchanged request succeeds in both recorded
fork histories and, here, the narrow AMM model matches the EVM exactly. It does not turn the output
difference into wallet loss or establish the outer sender's intent.

## A useful model failure: block 17000008

The checked-in corpus also contains a case where replay prevents over-interpreting AMM arithmetic.
For pair `0x4a3d1fd5f8ab514622d364ba20f325beb3327856`:

```text
observed pair input                 809676432957450567
front-omitted EVM pair input        777864049811781303
observed pair output                  10000000000000000
front-omitted EVM pair output         10000000000000000
fixed-input mathematical output       10373772338413816
EVM minus mathematical output          -373772338413816
```

The victim succeeds and preserves its pair output while its input changes. That behavior is
**consistent with output-preserving semantics**, but the available evidence does not prove the
transaction's exact-output intent. The fixed-input formula answers a different question and its
mismatch is retained rather than hidden.

## Checked-in corpus evaluation

The deterministic release corpus covers Ethereum blocks `17000001` through `17000008`:

| Measure | Result |
|---|---:|
| Blocks completed | 8 / 8 |
| Transactions / receipts inspected | 819 / 819 |
| V2-compatible swaps | 88 |
| Unique pairs | 52 |
| Strict candidates | 3 |
| Canonical candidates | 3 / 3 |
| Exact observed replays | 3 / 3 |
| Reliable front-omitted EVM experiments | 3 / 3 |
| Unchanged pair input | 2 / 3 |
| Exact fixed-input math / EVM agreement | 2 / 3 |

This is a small deterministic fixture, not a representative prevalence, precision, profitability,
or performance study. It was selected to exercise the pipeline and includes two candidates that
share outer transactions in block `17000008`.

The machine-readable source is
[`artifacts/evaluation_17000001_17000008.json`](artifacts/evaluation_17000001_17000008.json), with
field and failed-attempt notes in [`artifacts/README.md`](artifacts/README.md). Re-run it with:

```bash
ETH_RPC_URL=https://your-provider.example .venv/bin/python scripts/evaluate_corpus.py \
  --start-block 17000001 \
  --end-block 17000008 \
  --candidate-limit 50 \
  --evm-limit 5 \
  --evm-cooldown-seconds 90
```

The 90-second cooldown is an explicit provider-quota accommodation, not analysis time. Direct RPC
request counts exclude Anvil's internal fork traffic, which the upstream provider may also meter.

## Evidence hierarchy

BlockScope does not collapse different forms of evidence into one confidence score:

1. **Historical evidence** — normalized blocks, transactions, receipts, logs, and historical
   contract calls obtained from the configured provider.
2. **Derived mathematical evidence** — exact integer and rational calculations over historical
   pair inputs and reserves; no calldata is executed.
3. **Forked-EVM experimental evidence** — unchanged or deliberately omitted transaction plans run
   on fresh Anvil forks, with environment controls, replay mismatches, and limitations retained.

Within observed attribution, checkpoint balances are endpoint-state evidence, receipt logs are
event evidence, and call traces are execution-path evidence. Agreement is reconciliation, not
proof that any one source captures every state transition.

## Supported claims

When the corresponding result is available, BlockScope can establish that:

- logs match the supported V2-compatible event shapes and have reconstructible adjacent reserves;
- transactions form the detector's strict sandwich-shaped pattern;
- a pair's historical factory provenance matches canonical Ethereum-mainnet Uniswap V2;
- a fixed observed pair input produces a stated output under the canonical integer formula;
- an unchanged historical transaction reproduces selected receipt and V2 pair semantics;
- a controlled front-omission branch succeeds, reverts, changes pair input/output, or lacks the
  candidate-pair Swap under the recorded local environment;
- selected endpoint balances, Transfer-shaped logs, and locally reproduced call traces reconcile.

## Claims BlockScope does not make

BlockScope does not establish:

- attacker intent, mempool visibility, bundle use, or common beneficial ownership;
- detector recall, population prevalence, or that every candidate is a confirmed attack;
- wallet-level victim loss, attacker profit, net profit, or USD value;
- that event-compatible pairs use canonical fees or token behavior without provenance checks;
- arbitrary-contract replay equivalence or equality of every unobserved internal state change;
- exact-output intent merely because output is preserved while input changes;
- representativeness from the golden block or eight-block corpus.

## Detection and counterfactual boundaries

The detector groups swaps by pair, splits discontinuous reserve segments, and uses a deterministic
first-opposite-close rule. It requires distinct front, victim, and back transactions; the same
normalized sender on the outer transactions; different victim senders; same-pair/same-direction
victims; exact reserve continuity; adverse front movement; and reversing back movement. It does
not search combinatorial victim subsets or skip an earlier opposite-direction close.

The mathematical counterfactual begins at the front leg's reconstructed pre-reserves, removes the
front reserve effect, holds each victim's observed pair input fixed, and applies:

```text
amount_in_with_fee = amount_in * 997
amount_out = (amount_in_with_fee * reserve_out)
             // (reserve_in * 1000 + amount_in_with_fee)
```

It is enabled only when `pair.factory()` and the historical canonical factory `getPair()` result
both match. Pair output need not equal wallet receipt for transfer-tax or unusual tokens.

The EVM counterfactual starts two independent forks at block `N - 1`. Both reproduce the prefix
through victim `V`; the experimental branch omits the complete front transaction `F` and submits
the exact same victim request. Earlier victims remain in a multi-victim prefix and the back
transaction is not replayed. BlockScope never repairs nonces, funds accounts, edits calldata,
mutates storage, or forces success.

## Replay fidelity and limitations

Historical senders are impersonated, so original signatures and transaction hashes are not
reproduced. BlockScope preserves recipient, nonce, value, calldata, gas limit, transaction type,
chain ID, access list, and applicable fee fields. Exact pair replay requires matching status,
ordered V2 Swap semantics, and adjacent post-Swap Sync reserves. Gas and normalized receipt-log
comparisons remain separate evidence.

Anvil capabilities vary by version. Some historical header fields—especially `prevrandao` and
`difficulty`—may be uncontrollable or differ locally; BlockScope records these deviations. Fork
results also depend on the configured provider's historical state, tracing support, quota, and
retention. A reliable label means the implemented controls and comparisons passed, not universal
EVM equivalence.

## Development and release status

Run the offline checks with:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src tests scripts
git diff --check
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for contribution expectations and
[`CHANGELOG.md`](CHANGELOG.md) for the alpha release scope.

No license has been selected. Until the repository includes a license, copyright law reserves the
usual rights; choose an appropriate license before presenting BlockScope as open-source software.
