"""Real Pydantic AI + Core test for post-execution evidence-write interruption.

The tool side effect is a local in-memory mutation. We deliberately block
ONLY the SDK's tool_executed write *after* execution, not Core itself.
Do not use this synthetic injection as evidence of a full Core outage.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
import uuid

from loopgrid import LoopGrid
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_loopgrid import LoopGridPydanticAI, EvidencePersistenceError

BASE = os.environ.get('LOOPGRID_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')


def post(path, data):
    req = urllib.request.Request(BASE + path, json.dumps(data).encode(),
                                 {'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.load(res)


class FailOnlyAfterExecution:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, field):
        return getattr(self.inner, field)

    def add_event(self, did, kind, payload, **kwargs):
        if kind == 'tool_executed':
            raise ConnectionError('simulated post-handler SDK evidence write outage')
        return self.inner.add_event(did, kind, payload, **kwargs)


async def main():
    wid = post('/api/v1/workspaces', {
        'name': 'pydantic-postexec-' + uuid.uuid4().hex[:8],
    })['workspace_id']
    sdk = LoopGrid(base_url=BASE, workspace_id=wid)
    bridge = LoopGridPydanticAI(
        client=FailOnlyAfterExecution(sdk), workspace_id=wid,
        agent_id='postexec-validation',
    )
    did = bridge.start_decision(
        decision_type='sandbox_refund',
        agent={'id': 'postexec-validation'},
        authority={'acting_for':'local sandbox', 'scope':['refund:simulate']},
        model={'name': 'local-function-model'}, context={'prompt_version':'postexec-v1'},
        proposed_action={'tool': 'sandbox_refund', 'arguments': {'amount':25}},
        policy={'policy_id':'sandbox-postexec', 'version':'1', 'decision':'auto_allowed'},
        metadata={'sandbox': True, 'real_money_moved':False},
    )['decision_id']
    models = 0
    receipts = []

    def scripted_model(messages, info):
        nonlocal models
        models += 1
        if models == 1:
            return ModelResponse(parts=[ToolCallPart('sandbox_refund', {'amount':25},
                                                     tool_call_id='postexec-25')])
        return ModelResponse(parts=[TextPart('Done')])

    agent = Agent(FunctionModel(scripted_model), capabilities=[bridge.capability(did)])

    @agent.tool_plain
    def sandbox_refund(amount: int) -> str:
        receipt = f'local-receipt-{amount}'
        receipts.append(receipt)  # actual local state change
        return receipt

    try:
        await agent.run('Simulate refund of 25')
    except EvidencePersistenceError as exc:
        assert exc.execution_occurred and exc.reconciliation_required
        assert exc.decision_id == did and exc.tool_name == 'sandbox_refund'
        assert exc.tool_call_id == 'postexec-25'
        assert exc.result_commitment and exc.result_commitment['sha256']
    else:
        raise AssertionError('Expected post-execution evidence interruption')

    assert receipts == ['local-receipt-25'], receipts
    record = sdk.get_decision(did)
    types = [e['event_type'] for e in record.get('events', [])]
    assert types.count('tool_requested') == 1
    assert types.count('tool_executed') == 0
    assert types.count('outcome_observed') == 0
    assert record['verification']['valid'] is True, record
    print(json.dumps({
        'decisionId':did, 'workspaceId':wid,
        'actualSandboxExecutions': len(receipts),
        'toolExecutionEvidencePersisted':False,
        'downstreamReconciliationRequired':True,
        'verificationValidForPersistedEvents': record['verification']['valid'],
        'realMoneyMoved':False,
    }, indent=2))
    print('PASS: real tool returned; evidence write interrupted; no false completion claim')


if __name__ == '__main__':
    asyncio.run(main())
