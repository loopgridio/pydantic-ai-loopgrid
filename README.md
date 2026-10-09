# LoopGrid for Pydantic AI

**Signed, independently verifiable evidence for consequential Pydantic AI actions.**

LoopGrid adds a decision-evidence layer to Pydantic AI agents without replacing their models, tools, native human approvals, or observability. It connects each consequential action to **delegated authority, a versioned policy, authenticated host approval (when required), the actual tool request and execution, an authoritative downstream receipt, and cryptographic verification.**

## What it delivers

- **Native hooks:** Capture real model responses and validated tool execution through Pydantic AI's `AbstractCapability` extension surface.
- **Action authorization:** Enforce a policy verdict, exact tool name and arguments, and a bounded execution count at the native tool boundary.
- **Human oversight:** Integrate with Pydantic AI's deferred-tool approval and require separately recorded host-validated review before an approved action proceeds.
- **Accurate evidence:** Distinguish a tool's return from an independently observed business outcome. Record model/tool SHA-256 commitments rather than raw sensitive content.
- **Fail-closed execution:** Stop on missing authorization, mismatched arguments, failed prerequisite evidence writes, or consumed invocation budgets.
- **Restart-safe local option:** Use SQLite-backed atomic authorization reservations across processes on the **same host/local disk**, without resetting consumed budgets after restart.
- **Post-execution recovery signaling:** If an action returns but its evidence write fails, raise `EvidencePersistenceError` with a result commitment and an explicit reconciliation requirement. Never automatically replay a completed side effect.
- **Portable verification:** Existing LoopGrid Core supplies Ed25519 signatures, SHA-256 evidence chaining, and independent export/verification.

Pydantic AI executes the agent. **LoopGrid preserves and verifies the recorded decision evidence.**

## Install

Install from PyPI:
```bash
pip install pydantic-ai-loopgrid==0.1.0
```

For local development, install from source with Python 3.11+:
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

Requires a running LoopGrid Core (tested target `0.8.1-design-partner`), Python SDK `loopgrid>=0.8.0,<0.9`, and Pydantic AI 2.x native capabilities (`pydantic-ai-slim>=2.42,<3`).

## Quickstart — a complete deterministic sandbox action

Start Core on `http://127.0.0.1:8000` and use a workspace ID that exists in your Core installation. This example runs using Pydantic AI's local `FunctionModel`: **no paid model API key or payment provider required**.

```python
import asyncio
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_loopgrid import LoopGridPydanticAI

async def main():
    bridge = LoopGridPydanticAI(
        base_url="http://127.0.0.1:8000",
        workspace_id="YOUR_EXISTING_WORKSPACE_ID",
        agent_id="refund-sandbox-agent",
    )
    decision = bridge.start_decision(
        decision_type="sandbox_refund",
        agent={"id": "refund-sandbox-agent", "version": "1"},
        authority={"acting_for": "local-sandbox", "scope": ["refund:simulate"]},
        model={"name": "local-function-model"},
        context={"prompt_version": "refund-sandbox-v1"},
        proposed_action={"tool": "sandbox_refund", "arguments": {"amount": 25}},
        # Supply your application's genuine, versioned policy verdict.
        policy={"policy_id": "sandbox-policy", "version": "1", "decision": "auto_allowed"},
    )
    decision_id = decision["decision_id"]
    calls = 0

    def scripted_model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("sandbox_refund", {"amount": 25})])
        return ModelResponse(parts=[TextPart("Sandbox action finished")])

    agent = Agent(FunctionModel(scripted_model), capabilities=[bridge.capability(decision_id)])
    receipts = []

    @agent.tool_plain
    def sandbox_refund(amount: int) -> str:
        receipt = f"sandbox-receipt-{amount}"
        receipts.append(receipt)  # local simulated downstream system
        return receipt

    result = await agent.run("Simulate a refund of 25")
    assert receipts == ["sandbox-receipt-25"]
    bridge.record_observed_outcome(
        decision_id, observer="local-sandbox-ledger", receipt_id=receipts[0],
        outcome={"status": "succeeded", "sandbox": True, "real_money_moved": False},
    )
    record = bridge.get_decision(decision_id)
    print(result.output, record["verification"]["valid"])

asyncio.run(main())
```

For an automatically provisioned workspace and complete assertions (including `evidence_complete`, 100% applicable coverage, and cryptographic verification), run the tested repository example:

```powershell
.\.venv\Scripts\python.exe examples\native_core_e2e.py
```

## Human approval

Use Pydantic AI's native `requires_approval=True` deferred execution. Your application authenticates the reviewer, records the verdict using `bridge.record_human_review(...)`, and resumes Pydantic AI using `DeferredToolResults`. LoopGrid independently checks its recorded approval and the bound tool arguments before allowing execution.

```powershell
.\.venv\Scripts\python.exe examples\native_approval_e2e.py
.\.venv\Scripts\python.exe examples\native_core_security_e2e.py
.\.venv\Scripts\python.exe examples\native_core_recovery_e2e.py
```

## Durable authorization on one host

For restart-safe decisions or multiple local worker processes, provide a **trusted local-disk** SQLite store. All workers sharing the same workspace and decision must use the same SQLite file. A spent authorization never resets automatically after a process crash.

```python
from pydantic_ai_loopgrid import LoopGridPydanticAI, SQLiteAuthorizationStore

bridge = LoopGridPydanticAI(
    base_url="http://127.0.0.1:8000",
    workspace_id="YOUR_EXISTING_WORKSPACE_ID",
    authorization_store=SQLiteAuthorizationStore("/path/on/local/disk/authorizations.db"),
)
# Start/authorize the decision normally. After restarting the process, reopen
# the same store and use bridge.capability(existing_decision_id) to resume.
```

Protect the database and its directory with host filesystem permissions. This store is **not a multi-host distributed authorization service**, and it is not transactional with Core's remote event writes. See [SECURITY.md](SECURITY.md) for safe recovery guidance.

## After-execution evidence recovery

Execution and subsequent evidence persistence are separate operations. If a tool has **already returned** but `tool_executed` evidence cannot be confirmed, the integration raises `EvidencePersistenceError` with `execution_occurred=True`, the decision/tool identifiers, a SHA-256 result commitment if available, and `reconciliation_required=True`.

**Do not automatically retry the action.** Use an authoritative downstream receipt or idempotency key to reconcile the external effect before recording any observed outcome. The reservation remains consumed. A tool exception retains its original exception type, with a reconciliation note; it does not produce a false success event.

## Evidence contract

| Origin | LoopGrid event |
|---|---|
| Host starts a decision under delegated authority | `decision_created` |
| Host supplies versioned policy verdict | `policy_evaluated` |
| Native `after_model_request` | `model_completed` |
| Authenticated host reviewer, where required | `human_approved` / `human_rejected` |
| Native `wrap_tool_execute` before calling the handler | `tool_requested` |
| Tool handler really returned and evidence was persisted | `tool_executed` |
| Host verifies downstream receipt or authoritative result | `outcome_observed` |
| LoopGrid Core | Signed chain, export, verification |

Tool return ≠ verified business outcome. Cryptographic verification establishes the integrity and provenance of **recorded** evidence, not truth about unseen events.

## Developer validation

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests examples
.\.venv\Scripts\python.exe examples\native_core_e2e.py
.\.venv\Scripts\python.exe examples\native_approval_e2e.py
.\.venv\Scripts\python.exe examples\native_core_security_e2e.py
.\.venv\Scripts\python.exe examples\native_core_recovery_e2e.py
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\python.exe -m twine check dist\*
```

See [VALIDATION.md](VALIDATION.md) for the validated baseline and release gates, [INTEGRATION-CONTRACT.md](INTEGRATION-CONTRACT.md) for event mapping, and [SECURITY.md](SECURITY.md) for authorization and reconciliation boundaries.

Apache-2.0. Independent community integration; not affiliated with Pydantic.
