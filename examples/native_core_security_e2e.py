"""Six real Pydantic AI -> real LoopGrid Core sandbox security checks.

Deterministic validation example; not a production policy engine.
It never contacts a payment provider. Native deferred-tool approval/resume is
validated separately by examples/native_approval_e2e.py.

Run from project root after pip install -e '.[dev]':
    .\\.venv\\Scripts\\python.exe examples\\native_core_security_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
import uuid
from typing import Any

from loopgrid import LoopGrid
from pydantic_ai import Agent, ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_loopgrid import LoopGridPydanticAI

BASE = os.environ.get("LOOPGRID_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _json_get(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(BASE + path, timeout=15) as response:
        return json.load(response)


def _json_post(path: str, data: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        BASE + path,
        json.dumps(data).encode("utf-8"),
        {"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


class InjectOneWriteFailure:
    """For ONE scenario, block the pre-execution event write locally.

    Delegates all other operations to the real SDK/Core; this is not a Core
    outage simulation and must not be presented as one.
    """

    def __init__(self, inner: LoopGrid) -> None:
        self.inner = inner

    def __getattr__(self, key: str) -> Any:
        return getattr(self.inner, key)

    def add_event(self, decision_id: str, kind: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        if kind == "tool_requested":
            raise ConnectionError("injected synthetic pre-execution evidence write failure")
        return self.inner.add_event(decision_id, kind, payload, **kwargs)


async def scenario(workspace_id: str, case: str) -> dict[str, Any]:
    settings = {
        "blocked": {"policy": "blocked", "approval": None, "args": 25, "inject": False, "expect": 0},
        "missing_approval": {"policy": "human_approval_required", "approval": None, "args": 25, "inject": False, "expect": 0},
        "human_rejected": {"policy": "human_approval_required", "approval": False, "args": 25, "inject": False, "expect": 0},
        "human_approved": {"policy": "human_approval_required", "approval": True, "args": 25, "inject": False, "expect": 1},
        "wrong_arguments": {"policy": "auto_allowed", "approval": None, "args": 26, "inject": False, "expect": 0},
        "evidence_write_failure": {"policy": "auto_allowed", "approval": None, "args": 25, "inject": True, "expect": 0},
    }[case]

    sdk = LoopGrid(base_url=BASE, workspace_id=workspace_id)
    client = InjectOneWriteFailure(sdk) if settings["inject"] else sdk
    bridge = LoopGridPydanticAI(client=client, workspace_id=workspace_id, agent_id=f"test-{case}")

    decision_id = bridge.start_decision(
        decision_type="sandbox_refund",
        agent={"id": f"test-{case}", "version": "validation-only"},
        authority={"acting_for": "synthetic sandbox", "scope": ["refund:simulate"], "limit": 30},
        model={"name": "local-pydantic-function-model"},
        context={"prompt_version": "security-test-v1"},
        proposed_action={"tool": "sandbox_refund", "arguments": {"amount": 25}},
        policy={"policy_id": f"sandbox-{case}", "version": "1", "decision": settings["policy"]},
        metadata={"sandbox": True, "real_money_moved": False},
    )["decision_id"]

    if settings["approval"] is not None:
        bridge.record_human_review(
            decision_id,
            reviewer="synthetic-test-host-not-authenticated",
            approved=settings["approval"],
            reason="Pydantic integration local release test",
        )

    state = {"models": 0, "actual_tools": 0, "request_attempts": 0, "run_exception": None}
    original_event = bridge._event

    async def count_event(did: str, kind: str, payload: dict[str, Any],
                          actor_type: str, actor_id: str, idem: str) -> Any:
        if kind == "tool_requested":
            state["request_attempts"] += 1
        return await original_event(did, kind, payload, actor_type, actor_id, idem)

    bridge._event = count_event

    def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        state["models"] += 1
        if state["models"] == 1:
            return ModelResponse(parts=[ToolCallPart(
                "sandbox_refund", {"amount": settings["args"]},
                tool_call_id=f"{case}-native-call",
            )])
        return ModelResponse(parts=[TextPart("Local sandbox execution finished")])

    # No native requires_approval flag here ON PURPOSE: these scenarios test
    # that LoopGrid itself prevents unauthorized execution independently.
    # Native Pydantic approval/resume is tested in native_approval_e2e.py.
    agent = Agent(FunctionModel(scripted_model), capabilities=[bridge.capability(decision_id)])

    @agent.tool_plain
    def sandbox_refund(amount: int) -> str:
        """Perform a harmless in-memory sandbox action, not a real refund."""
        state["actual_tools"] += 1
        return json.dumps({"receipt_id": f"synthetic-{case}", "amount": amount, "sandbox": True})

    try:
        await agent.run("Attempt a synthetic sandbox refund")
    except Exception as exc:
        state["run_exception"] = type(exc).__name__

    # Record downstream outcome ONLY when the sandbox tool truly executed.
    if state["actual_tools"] == 1:
        bridge.record_observed_outcome(
            decision_id,
            observer="synthetic-in-memory-ledger",
            receipt_id=f"synthetic-{case}",
            outcome={"status": "succeeded", "sandbox": True, "real_money_moved": False},
        )

    record = sdk.get_decision(decision_id)
    types = [entry["event_type"] for entry in record.get("events", [])]
    lifecycle = record.get("summary", {}).get("lifecycle", {})
    lifecycle = lifecycle.get("state") if isinstance(lifecycle, dict) else lifecycle
    coverage = record.get("coverage", {})
    verify = record.get("verification", {})

    assert state["request_attempts"] == 1, (case, "middleware tool interception missing", state)
    assert state["actual_tools"] == settings["expect"], (case, "unsafe sandbox side effect", state)
    assert types.count("tool_executed") == settings["expect"], (case, "false execution evidence", types)
    assert types.count("outcome_observed") == settings["expect"], (case, "false outcome evidence", types)
    assert verify.get("valid") is True, (case, "signature/chain verification failed", verify)
    if case == "blocked":
        assert lifecycle == "blocked", (case, lifecycle)
    elif case == "missing_approval":
        assert lifecycle == "awaiting_human_review", (case, lifecycle)
    elif case == "human_rejected":
        assert lifecycle == "rejected", (case, lifecycle)
        assert "human_rejected" in types, (case, types)
    elif case == "human_approved":
        assert lifecycle == "evidence_complete", (case, lifecycle)
        assert "human_approved" in types and types.count("tool_requested") == 1, (case, types)
        assert coverage.get("score") == 100 and coverage.get("complete") is True, (case, coverage)
    else:
        assert lifecycle != "evidence_complete", (case, lifecycle)

    print(f"PASS: {case} (sandbox tool executions: {state['actual_tools']})")
    return {
        "case": case,
        "decisionId": decision_id,
        "nativeRequestAttempts": state["request_attempts"],
        "sandboxToolExecutions": state["actual_tools"],
        "persistedToolRequests": types.count("tool_requested"),
        "eventTypes": types,
        "lifecycle": lifecycle,
        "coverageScore": coverage.get("score"),
        "verificationValid": verify.get("valid"),
        "runExceptionType": state["run_exception"],
        "result": "PASS",
    }


async def main() -> None:
    ready = _json_get("/ready")
    assert ready.get("ready") is True, ready
    wid = _json_post("/api/v1/workspaces", {"name": "pydantic-security-" + uuid.uuid4().hex[:8]})["workspace_id"]
    cases = [
        "blocked", "missing_approval", "human_rejected", "human_approved",
        "wrong_arguments", "evidence_write_failure",
    ]
    results = []
    for name in cases:
        results.append(await scenario(wid, name))
    print(json.dumps({
        "framework": "pydantic-ai", "coreVersion": ready.get("version"),
        "workspaceId": wid, "casesPassed": len(results), "cases": results,
        "realMoneyMoved": False, "productionReviewerIdentityValidated": False,
    }, indent=2))
    print("PASS: native Pydantic AI tool interception + LoopGrid Core fail-closed evidence validated")


if __name__ == "__main__":
    asyncio.run(main())
