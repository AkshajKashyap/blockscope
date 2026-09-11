# Evaluation artifacts

`scripts/evaluate_corpus.py` writes compact, machine-readable corpus results here by default.
Artifacts contain configuration, software metadata, aggregate metrics, timings, block diagnostics,
and candidate-level outcomes. They intentionally omit RPC URLs and raw blocks, receipts, and traces.

Generated filenames use the inclusive range:

```text
evaluation_<start>_<end>.json
```

The golden block fixture remains a separate opt-in regression and is not an evaluation artifact.
