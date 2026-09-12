# Contributing to BlockScope

BlockScope is an alpha research tool whose credibility depends on conservative claims and explicit
evidence boundaries. Contributions should keep historical observations, mathematical models, and
forked-EVM experiments distinct.

## Development setup

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src tests scripts
git diff --check
```

Offline tests must not depend on a live provider or Anvil. Put opt-in integration verification in
an explicit script or marked workflow, redact RPC credentials from every error and artifact, and
add focused tests for changed semantics. Preserve raw integer token amounts internally; decimals
and symbols are display metadata.

When changing a public result, document the evidence source, assumptions, unavailable states, and
what the result does not prove. Do not silently repair replay inputs or turn provider/backend
failures into successful observations.

## Pull requests

Keep changes scoped, update the README or changelog when the public contract changes, and include
the exact verification commands run. Live artifacts should record configuration and software
metadata and must not contain RPC URLs, credentials, or raw provider payloads.

The repository does not yet include a license. Discuss licensing with the maintainer before making
contributions that assume redistribution terms.
