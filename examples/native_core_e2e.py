"""Deterministic REAL Pydantic AI FunctionModel -> REAL LoopGrid Core E2E.

No external model keys, no real financial action. Required release gate on a
machine with `pydantic-ai-slim`, `loopgrid` and Core running at localhost:8000.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
import uuid

from loopgrid import LoopGrid
from pydantic_ai import Agent, ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_loopgrid import LoopGridPydanticAI

BASE = os.getenv("LOOPGRID_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# Example-only HTTP timeout. Does not change LoopGrid SDK/Core timeouts.
HTTP_TIMEOUT = float(os.getenv("LOOPGRID_EXAMPLE_HTTP_TIMEOUT", "30"))


def _core_json(request, operation):
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.load(response)
    except TimeoutError as exc:
        raise RuntimeError(
            f"LoopGrid Core at {BASE} did not respond within {HTTP_TIMEOUT:g}s "
            f"while {operation}. Check Core/Docker health and server logs. "
            "A workspace-creation POST may have reached Core despite the timeout; "
            "this example does not retry POST automatically."
        ) from exc


def get(path):
    return _core_json(BASE + path, f"GET {path}")


def post(path, body):
    request = urllib.request.Request(
        BASE + path, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST",
    )
    return _core_json(request, f"POST {path}")


async def main():
    ready = get("/ready")
    assert ready["ready"] is True
    workspace = post("/api/v1/workspaces", {"name": "pydantic-e2e-" + uuid.uuid4().hex[:8]})
    wid = workspace["workspace_id"]
    sdk = LoopGrid(base_url=BASE, workspace_id=wid)
    bridge = LoopGridPydanticAI(client=sdk, workspace_id=wid, agent_id="pydantic-sandbox")
    receipt_ledger = []
    model_count = 0

    def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_count
        model_count += 1
        if model_count == 1:
            return ModelResponse(parts=[ToolCallPart("sandbox_refund", {"amount": 25}, tool_call_id="sandbox-call-25")])
        return ModelResponse(parts=[TextPart("Sandbox refund simulated; no real money moved.")])

    decision = bridge.start_decision(
        decision_type="sandbox_refund",
        agent={"id": "pydantic-sandbox", "version": "e2e"},
        authority={"acting_for": "local sandbox", "scope": ["refund:simulate"], "limit": 30},
        model={"provider": "local", "name": "pydantic-function-model"},
        context={"prompt_version": "pydantic-sandbox-v1"},
        proposed_action={"tool": "sandbox_refund", "arguments": {"amount": 25}},
        policy={"policy_id": "sandbox-refund-v1", "version": "1", "decision": "auto_allowed"},
        metadata={"sandbox": True, "real_money_moved": False},
    )
    did = decision["decision_id"]
    agent = Agent(FunctionModel(scripted_model), capabilities=[bridge.capability(did)])

    @agent.tool_plain
    def sandbox_refund(amount: int) -> str:
        """Simulate a refund in a local in-memory ledger, not a payment provider."""
        receipt = {"id": "sandbox-" + uuid.uuid4().hex[:10], "amount": amount,
                   "status": "succeeded", "sandbox": True, "real_money_moved": False}
        receipt_ledger.append(receipt)
        return json.dumps(receipt)

    await agent.run("Simulate a refund for 25.")
    assert len(receipt_ledger) == 1
    bridge.record_observed_outcome(
        did, observer="sandbox-ledger", receipt_id=receipt_ledger[0]["id"],
        outcome={"status": "succeeded", "amount": receipt_ledger[0]["amount"],
                 "sandbox": True, "real_money_moved": False},
    )
    record = sdk.get_decision(did)
    event_types = [e["event_type"] for e in record.get("events", [])]
    summary = record.get("summary", {}).get("lifecycle", {})
    state = summary.get("state") if isinstance(summary, dict) else summary
    coverage = record.get("coverage", {})
    verify = record.get("verification", {})
    assert state == "evidence_complete", record
    assert coverage.get("score") == 100 and coverage.get("complete") is True, record
    assert verify.get("valid") is True and not verify.get("failures", []), record
    assert event_types.count("tool_requested") == 1, record
    assert event_types.count("tool_executed") == 1, record
    assert event_types.count("outcome_observed") == 1, record
    assert event_types.count("model_completed") >= 1, record
    print(json.dumps({
        "framework": "pydantic-ai", "coreVersion": ready.get("version"),
        "workspaceId": wid, "decisionId": did, "modelCalls": model_count,
        "actualSandboxExecutions": len(receipt_ledger), "lifecycle": state,
        "coverageScore": coverage.get("score"), "coverageComplete": coverage.get("complete"),
        "verifyValid": verify.get("valid"), "eventTypes": event_types,
        "realMoneyMoved": False,
    }, indent=2))
    print("PASS: real Pydantic AI -> real sandbox tool -> LoopGrid signed Core verification")

if __name__ == "__main__":
    asyncio.run(main())
