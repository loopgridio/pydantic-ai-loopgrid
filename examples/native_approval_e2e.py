"""Native Pydantic AI requires_approval pause/resume, signed LoopGrid sandbox.

Local synthetic reviewer response. Host integration must authenticate users.
Requires running LoopGrid Core and installed Pydantic AI 2.x.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
import uuid

from loopgrid import LoopGrid
from pydantic_ai import (
    Agent, DeferredToolRequests, DeferredToolResults, ModelMessage,
    ModelResponse, TextPart, ToolCallPart,
)
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


def post(path, body):
    request = urllib.request.Request(
        BASE + path, json.dumps(body).encode(),
        {"Content-Type": "application/json"}, method="POST",
    )
    return _core_json(request, f"POST {path}")

async def scenario(approved: bool):
    wid = post("/api/v1/workspaces", {"name":"pydantic-approval-" + uuid.uuid4().hex[:8]})["workspace_id"]
    sdk = LoopGrid(base_url=BASE, workspace_id=wid)
    bridge = LoopGridPydanticAI(client=sdk, workspace_id=wid)
    state = {"models":0, "tools":0}
    def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        state["models"] += 1
        if state["models"] == 1:
            return ModelResponse(parts=[ToolCallPart("sandbox_refund", {"amount": 25}, tool_call_id="native-approval-25")])
        return ModelResponse(parts=[TextPart("Finished sandbox approval flow")])
    did = bridge.start_decision(
        decision_type="sandbox_refund", agent={"id":"pydantic-reviewer-test"},
        authority={"acting_for":"sandbox", "scope":["refund:simulate"]},
        model={"name":"local-function-model"}, context={"prompt_version":"approval-v1"},
        proposed_action={"tool":"sandbox_refund","arguments":{"amount":25}},
        policy={"policy_id":"approval-test", "version":"1", "decision":"human_approval_required"},
    )["decision_id"]
    agent = Agent(FunctionModel(scripted_model),
                  capabilities=[bridge.capability(did)], output_type=[str, DeferredToolRequests])
    @agent.tool_plain(requires_approval=True)
    def sandbox_refund(amount: int) -> str:
        """Record a synthetic refund receipt, not a real payment."""
        state["tools"] += 1
        return f"sandbox-{amount}"
    pending = await agent.run("Simulate refund for 25")
    assert isinstance(pending.output, DeferredToolRequests), pending.output
    assert state["tools"] == 0
    requests = pending.output
    assert len(requests.approvals) == 1
    call = requests.approvals[0]
    assert call.tool_name == "sandbox_refund" and call.args == {"amount":25}
    # Simulates a host-validated response, does NOT claim a production human identity.
    bridge.record_human_review(did, reviewer="synthetic-host-reviewer", approved=approved)
    answers = DeferredToolResults()
    answers.approvals[call.tool_call_id] = approved
    await agent.run(message_history=pending.all_messages(), deferred_tool_results=answers)
    assert state["tools"] == int(approved), state
    if approved:
        bridge.record_observed_outcome(
            did, observer="sandbox-ledger", receipt_id="sandbox-25",
            outcome={"status":"succeeded", "sandbox":True, "real_money_moved":False},
        )
    record = sdk.get_decision(did)
    cover = record.get("coverage", {})
    lifecycle = record.get("summary",{}).get("lifecycle",{})
    lifecycle = lifecycle.get("state") if isinstance(lifecycle,dict) else lifecycle
    verification = record.get("verification", {})
    expected = "evidence_complete" if approved else "rejected"
    assert lifecycle == expected, record
    assert cover.get("score") == 100, record
    assert verification.get("valid") is True, record
    print(f"PASS: native approval={approved} lifecycle={lifecycle} executed={state['tools']} verify={verification['valid']}")

async def main():
    await scenario(False)
    await scenario(True)
    print("PASS: native Pydantic approval suspension and resume with LoopGrid evidence")

if __name__ == "__main__":
    asyncio.run(main())
