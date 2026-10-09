# Release validation — LoopGrid for Pydantic AI v0.1.0

## Windows verification — reported October 9, 2026

Environment: Windows / Python 3.12.6, Pydantic AI 2.54.0, LoopGrid Python SDK 0.8.0, LoopGrid Core 0.8.1-design-partner. The test environment used sandbox tools and synthetic reviewer identities; no real money was moved.

- **Automated tests:** `pytest -q` — 23 passed on the hardened RC2 source.
- **Dependencies:** `pip check` — no broken requirements.
- **Native Core E2E:** passed; two model calls, one sandbox execution, `evidence_complete`, applicable coverage 100%, `verifyValid=true`.
- **Native deferred approval:** rejection — zero executions, verification true; approval — exactly one execution, `evidence_complete`, verification true.
- **Native security E2E:** 6/6 passed — policy blocked, missing approval, reviewer rejected, reviewer approved, wrong arguments, and prerequisite evidence-write failure. No unauthorized sandbox execution.
- **Post-execution recovery:** passed — one action executed, simulated subsequent evidence write failed, `downstreamReconciliationRequired=true`, no false completion claim; pre-existing persisted evidence verified.
- **LoopGrid Core readiness:** reported `ready: true`, `database: ok`, `signer: local_ed25519`, `payload_vault: aes-256-gcm`.
- **Build:** `python -m build` produced wheel and sdist successfully; `python -m twine check dist\*` passed for both.
- **Clean wheel installation:** succeeded in a separate venv; `pip check` clean; import printed `0.1.0 LoopGridPydanticAI`.

The two initial 10-second workspace creation timeouts in RC2 were transient; subsequent reruns succeeded. RC2.1 increases example HTTP timeouts to a configurable 30 seconds and supplies better diagnostics. The adapter, tests, authorization code, and dependencies are identical between RC2 and RC2.1.

## Release metadata cleanup

This GitHub-ready source revises **only** `pyproject.toml` to use SPDX `Apache-2.0` license metadata, explicitly include license documents, and require a modern setuptools build backend. It updates this validation record to match the verified Windows logs. All adapter code, tests, and examples are copied without modification from the RC2.1 consolidated source. No new behavior is claimed on account of metadata changes.

## Remaining publication gates

1. From this exact source checkout, run `python -m build` and `python -m twine check dist\*`, ideally in the same Windows environment.
2. Verify GitHub Actions tests on Windows and Linux (configured for Python 3.11, 3.12, and 3.13).
3. Configure PyPI Trusted Publishing for repository `loopgridio/pydantic-ai-loopgrid` and GitHub Actions environment `pypi` before tagging/publishing `v0.1.0`.
4. Publish only after successful CI and release configuration. Python package version remains `0.1.0`.

Core signature validity establishes integrity of recorded evidence, not independent proof that every external fact is true. The approved sandbox examples do not establish authenticated production reviewer identity or real-world downstream reconciliation.
