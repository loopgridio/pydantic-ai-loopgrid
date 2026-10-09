# Pydantic AI / LoopGrid integration contract

**Native extension points:** `AbstractCapability.for_run`, `after_model_request`, `wrap_tool_execute` (Pydantic AI 2.x). The framework executes models/tools and supports deferred human review. No LoopGrid Core changes are required.

- **Decision and delegated identity:** authoritative application-supplied `agent`, `authority`, `model`, `context`, proposed tool and exact arguments.
- **Versioned policy:** authenticated host verdict; no model-generated policy statements accepted as proof.
- **Model event:** native response parts captured as SHA-256 commitment.
- **Tool request:** name and validated argument commitment captured before handler execution.
- **Authorized execution:** native tool handler returns; `tool_executed` only after evidence persistence succeeds.
- **Rejected/error execution:** no false `tool_executed` success; consumed reservation, host reconciliation required for uncertain downstream state.
- **Independent observed outcome:** actual application or downstream receipt observer; never inferred from model text.
- **Human review:** host validates reviewer/decision/tool binding; native `DeferredToolResults` alone is not authorization.
- **Execution budget:** atomic in-memory default, optionally SQLite-backed single-host durable reservation; any interrupted external/Core coordination fails closed.
- **Core:** Ed25519 signature, SHA-256 hash chain, verification, portable export (target Core `0.8.1-design-partner`, SDK `0.8.0`).

One decision binds one consequential tool name and exact argument shape. For workflows with different consequential actions, start separate decisions and apply the same capability at each action boundary.
