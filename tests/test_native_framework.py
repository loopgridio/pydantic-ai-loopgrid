"""REAL native Pydantic AI FunctionModel checks with fake LoopGrid transport.

Uses the actual framework when installed. Requires no paid AI model or Core.
The real Core E2E examples provide the separate cryptographic release gate.
"""
import pytest
import pydantic_ai.capabilities as capabilities

if getattr(capabilities.AbstractCapability, '__loopgrid_fake__', False):
    pytest.skip('real Pydantic AI dependency not installed in this environment', allow_module_level=True)

from pydantic_ai import (Agent, ModelResponse, TextPart, ToolCallPart,
                         DeferredToolRequests, DeferredToolResults)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_loopgrid import EvidenceGateError
from test_bridge import create


@pytest.mark.asyncio
async def test_real_hook_execution_exactly_once():
    bridge, sdk, did = create()
    tally = []
    n = 0
    def model(messages, info):
        nonlocal n
        n += 1
        return ModelResponse(parts=[ToolCallPart('refund', {'amount':25})]) if n == 1 else ModelResponse(parts=[TextPart('done')])
    agent = Agent(FunctionModel(model), capabilities=[bridge.capability(did)])
    @agent.tool_plain
    def refund(amount: int) -> str:
        tally.append(amount)
        return f'sandbox:{amount}'
    result = await agent.run('refund 25')
    assert result.output == 'done'
    assert tally == [25]
    events = [e[1] for e in sdk.events]
    assert events.count('model_completed') == 2
    assert events.count('tool_requested') == events.count('tool_executed') == 1


@pytest.mark.asyncio
async def test_native_deferred_approval_pause_and_resume():
    bridge, sdk, did = create(policy='human_approval_required')
    tally = []
    n = 0
    def model(messages, info):
        nonlocal n
        n += 1
        return ModelResponse(parts=[ToolCallPart('refund', {'amount':25}, tool_call_id='pending1')]) if n == 1 else ModelResponse(parts=[TextPart('done')])
    agent = Agent(FunctionModel(model), capabilities=[bridge.capability(did)], output_type=[str, DeferredToolRequests])
    @agent.tool_plain(requires_approval=True)
    def refund(amount: int) -> str:
        tally.append(amount)
        return f'sandbox:{amount}'
    paused = await agent.run('refund 25')
    assert isinstance(paused.output, DeferredToolRequests)
    assert not tally
    bridge.record_human_review(did, reviewer='synthetic-test-host', approved=True)
    approval = DeferredToolResults()
    for call in paused.output.approvals:
        approval.approvals[call.tool_call_id] = True
    resumed = await agent.run(message_history=paused.all_messages(), deferred_tool_results=approval)
    assert tally == [25]
    assert any(e[1]=='human_approved' for e in sdk.events)


@pytest.mark.asyncio
async def test_real_native_hook_marks_post_execution_evidence_failure():
    from pydantic_ai_loopgrid import EvidencePersistenceError
    from test_bridge import SDK, create
    sdk = SDK()
    bridge, _, did = create(client=sdk)
    sdk.fail_kind = 'tool_executed'
    tally = []
    model_count = 0

    def model(messages, info):
        nonlocal model_count
        model_count += 1
        return (ModelResponse(parts=[ToolCallPart('refund', {'amount':25}, tool_call_id='failing-evidence')])
                if model_count == 1 else ModelResponse(parts=[TextPart('done')]))

    agent = Agent(FunctionModel(model), capabilities=[bridge.capability(did)])

    @agent.tool_plain
    def refund(amount: int) -> str:
        tally.append(amount)
        return 'sandbox-executed'

    with pytest.raises(EvidencePersistenceError) as err:
        await agent.run('refund 25')
    assert err.value.execution_occurred is True
    assert err.value.reconciliation_required is True
    assert tally == [25]
    assert [e[1] for e in sdk.events].count('tool_executed') == 0
