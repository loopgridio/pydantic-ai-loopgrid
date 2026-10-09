# Changelog

## 0.1.0 — Unreleased candidate

- Native Pydantic AI model/tool evidence hooks and policy-bound tool authorization.
- Host-authenticated approval support alongside native deferred-tool resumption.
- Sha-256 commitments, genuine tool execution results and independent downstream outcomes.
- Optional restart-safe local SQLite authorization store; atomic cross-process reservations.
- Explicit `EvidencePersistenceError` when a completed tool lacks persisted execution evidence; no automatic replay.
- Expanded race, restart, failure, argument-match, and approval tests.
- Complete deterministic runnable README quickstart and sandbox Core examples.
