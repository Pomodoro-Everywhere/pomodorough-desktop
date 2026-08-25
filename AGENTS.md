# Pomodorough Desktop

This client keeps deterministic timer, projection, task identity, clock, bootstrap, and reconciliation policy in the pinned Rust/WASM SharedCore. Python owns native UI, persistence, transport, packaging, and orchestration.

## Ownership boundaries

- `shared_core.py` is the typed WASM adapter. Python policy must not duplicate authoritative reducers.
- `storage.py` composes persistence responsibilities. Workspace, record, projection, replication lifecycle, and replication coordination live in their named modules.
- SQLite transactions cover database state. External secure-store mutations use `SecretMutationJournal` compensation around the complete commit boundary.
- Responsibility controllers own orchestration and emit typed outcomes; screens own widgets and presentation signals. Keep the application composition root small.
- `iroh_network.py` owns transport lifecycle and protocol I/O, while `iroh_protocol.py` owns wire validation and limits.
- Release workflows must rebuild the exact pinned Core on the declared host, verify the embedded hash, scan raw and unpacked artifacts for the compromised credential, attest assets, verify the draft, and publish last.

Preserve SQLite schemas, serialized fields, queue durability, retry/reconciliation semantics, missing/null/value distinctions, signal ordering, localization coverage, and public CLI/TUI/GUI behavior.

## Verification

For every source change, complete all applicable gates:

```sh
uv run --frozen --with pytest python -m pytest -q
uv run --frozen --with ruff==0.15.22 ruff check .
python -m compileall -q src tests scripts
python scripts/check_docs.py
python scripts/check_protocol_fixture.py
python scripts/check_workflow_pins.py
python scripts/check_localization.py
actionlint
sh -n deploy/install.sh scripts/unpack_release_artifacts.sh scripts/verify_release_artifacts.sh
git diff --check
```

Use failure injection for SQLite plus secure-store boundaries. Exercise packaged SharedCore and OAuth resources, not only source-tree fixtures. Report real line and branch coverage when changing tests.
