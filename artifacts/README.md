# Evaluation artifacts

`scripts/evaluate_corpus.py` writes deterministic, machine-readable evaluation results here.
Each artifact contains its configuration, software metadata, aggregate metrics, timings, block
diagnostics, candidate outcomes, and failure taxonomy. RPC URLs and raw blocks, receipts, and
traces are intentionally excluded.

## Release corpus

`evaluation_17000001_17000008.json` is the completed `v0.1.0` release artifact. It records eight
completed blocks, 819 inspected transactions and receipts, 88 V2-compatible swaps, 52 unique
pairs, and three strict candidates. All three candidates have canonical provenance, exact observed
replay, and reliable front-omitted EVM results. Fixed-input mathematics agrees exactly with the EVM
for two; the third retains an informative model mismatch.

The artifact reports a dirty worktree and base commit `9522044` because the evaluation framework
itself was uncommitted when the live run was generated. The checked-in analysis code is commit
`b42f0e1`; no analytical code changed between the run and that commit.

## Incomplete provider-limited attempts

Two earlier generated artifacts were reviewed and removed from the release tree because they were
incomplete provider-quota diagnostics, not corpus results:

| Requested range | Blocks completed | Blocks failed | EVM provider failures |
|---|---:|---:|---:|
| `17000001..17000050` | 17 / 50 | 33 | 5 / 5 attempted |
| `17000001..17000010` | 8 / 10 | 2 | 3 / 3 attempted |

Both failed with HTTP 429 responses. That experience motivated the smaller deterministic prefix
and explicit EVM cooldown used for the release corpus. This history is retained here so the final
range is not mistaken for an analytically selected success-only sample.

Generated filenames use the inclusive range `evaluation_<start>_<end>.json`. The live golden block
verifier is a separate opt-in regression and is not an evaluation artifact.
