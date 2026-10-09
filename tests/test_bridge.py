import asyncio
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
from pydantic_ai_loopgrid import LoopGridPydanticAI, EvidenceGateError, FRAMEWORK, VERSION
from pydantic_ai_loopgrid.integration import _commit

class SDK:
    def __init__(self):
        self.events = []
        self.fail_kind = None
        self.recorded = []
        self.decisions = {}

    def record_decision(self, **kwargs):
        did = f"dec_{len(self.recorded)+1}"
        self.recorded.append(kwargs)
        self.decisions[did] = kwargs
        return {"decision_id": did}

    def policy_evaluated(self, did, payload, actor_id):
        self.add_event(did, "policy_evaluated", payload, actor_type="policy", actor_id=actor_id)

    def human_approved(self, did, reviewer, reason):
        self.add_event(did, "human_approved", {"approved": True}, actor_type="human", actor_id=reviewer)

    def human_rejected(self, did, reviewer, reason):
        self.add_event(did, "human_rejected", {"approved": False}, actor_type="human", actor_id=reviewer)

    def outcome_observed(self, did, payload, actor_id):
        self.add_event(did, "outcome_observed", payload, actor_type="system", actor_id=actor_id)

    def add_event(self, did, kind, payload, **kwargs):
        if self.fail_kind == kind:
            raise ConnectionError("private host details")
        self.events.append((did, kind, payload, kwargs))
        return {"event_id": len(self.events)}

    def get_decision(self, did):
        return self.decisions[did]
    def export_evidence(self, did, include_payloads=True):
        return b"bundle"


def create(*, policy="auto_allowed", args=None, client=None, calls=1):
    client = client or SDK()
    bridge = LoopGridPydanticAI(client=client, workspace_id="testing", agent_id="test-agent")
    action_args = args if args is not None else {"amount": 25}
    d = bridge.start_decision(
        decision_type="sandbox_refund", agent={"id": "test-agent"},
        authority={"acting_for": "sandbox", "limit": 30},
        model={"name": "function-model"}, context={"request_hash": "sha256:test"},
        proposed_action={"tool": "refund", "arguments": action_args},
        policy={"policy_id": "refund-v1", "version": "1", "decision": policy},
        max_tool_calls=calls,
    )
    return bridge, client, d["decision_id"]


def tc(name="refund", tool_id="test-call"):
    return SimpleNamespace(tool_name=name, tool_call_id=tool_id)


async def execute(cap, *, name="refund", args=None, action=None):
    if args is None:
        args = {"amount": 25}
    if action is None:
        async def action(a):
            return {"sandbox": True, "refund_id": "simulated"}
    return await cap.wrap_tool_execute(None, call=tc(name), tool_def=object(), args=args, handler=action)


def run(coro):
    return asyncio.run(coro)


def test_full_lifecycle_no_raw_payloads():
    bridge, sdk, did = create()
    cap = bridge.capability(did)
    resp = SimpleNamespace(model_name="function", parts=[{"text": "secret result"}])
    run(cap.after_model_request(None, request_context=SimpleNamespace(model=resp), response=resp))
    assert run(execute(cap))["sandbox"] is True
    bridge.record_observed_outcome(did, observer="sandbox-ledger", receipt_id="receipt-25",
                                   outcome={"status": "simulated", "sandbox": True})
    assert [e[1] for e in sdk.events] == ["policy_evaluated", "model_completed", "tool_requested", "tool_executed", "outcome_observed"]
    assert "secret result" not in str(sdk.events)
    assert sdk.events[-2][2]["external_outcome_verified"] is False

@pytest.mark.parametrize('policy', ['blocked','human_approval_required'])
def test_policy_gates_never_call_tool(policy):
    bridge, sdk, did = create(policy=policy)
    tally = []
    async def action(args): tally.append(args)
    with pytest.raises(EvidenceGateError):
        run(execute(bridge.capability(did), action=action))
    assert tally == []

def test_review_does_not_equal_deferred_client_approval():
    bridge, sdk, did = create(policy="human_approval_required")
    bridge.record_human_review(did, reviewer="authenticated-human-1", approved=False)
    with pytest.raises(EvidenceGateError):
        run(execute(bridge.capability(did)))
    assert sdk.events[-1][1] == 'tool_requested'

def test_host_approved_run_and_resumption():
    bridge, sdk, did = create(policy="human_approval_required")
    bridge.record_human_review(did, reviewer="authenticated-human-1", approved=True)
    cap = bridge.capability(did)
    resumed = run(cap.for_run(None))
    assert resumed is not cap
    assert run(execute(resumed))["sandbox"]
    assert any(e[1] == 'human_approved' for e in sdk.events)

def test_wrong_tool_and_exact_argument_match():
    bridge, sdk, did = create()
    calls = []
    async def action(a): calls.append(a)
    with pytest.raises(EvidenceGateError): run(execute(bridge.capability(did), name="delete", action=action))
    with pytest.raises(EvidenceGateError): run(execute(bridge.capability(did), args={"amount": 26}, action=action))
    assert not calls

def test_fail_closed_before_execution():
    sdk = SDK()
    bridge, sdk, did = create(client=sdk)
    sdk.fail_kind = 'tool_requested'
    calls = []
    async def action(a): calls.append(a)
    with pytest.raises(EvidenceGateError, match="ConnectionError"):
        run(execute(bridge.capability(did), action=action))
    assert calls == []

def test_model_evidence_failure_stops_run():
    sdk = SDK()
    bridge, _, did = create(client=sdk)
    sdk.fail_kind = 'model_completed'
    resp = SimpleNamespace(model_name="f", parts=[{"text": "x"}])
    with pytest.raises(EvidenceGateError):
        run(bridge.capability(did).after_model_request(None, request_context=SimpleNamespace(model=resp), response=resp))

def test_single_use_across_repeated_runs_and_parallel_calls():
    bridge, _, did = create()
    called = []
    async def action(a): called.append(a); await asyncio.sleep(.01); return True
    async def scenario():
        cap = bridge.capability(did)
        async def attempt():
            try:
                await execute(cap, action=action)
                return True
            except EvidenceGateError:
                return False
        return await asyncio.gather(attempt(), attempt())
    assert run(scenario()).count(True) == 1
    assert len(called) == 1
    with pytest.raises(EvidenceGateError):
        run(execute(bridge.capability(did)))

def test_idempotency_collision_binding_and_policy_immutability():
    sdk = SDK()
    bridge, sdk, did = create(client=sdk)
    with pytest.raises(ValueError): bridge.record_policy(did, {"policy_id": "different", "version": "2", "decision": "auto_allowed"})

def test_missing_provenance_and_outcome_receipt():
    bridge, sdk, did = create()
    with pytest.raises(ValueError): bridge.record_observed_outcome(did, observer="", receipt_id="r", outcome={"status": "ok"})
    with pytest.raises(ValueError): bridge.record_observed_outcome(did, observer="observer", receipt_id="", outcome={"status": "ok"})

def test_failed_tool_does_not_claim_success():
    bridge, sdk, did = create()
    async def failure(a): raise RuntimeError('sandbox tool failure')
    with pytest.raises(RuntimeError): run(execute(bridge.capability(did), action=failure))
    assert not any(e[1] == "tool_executed" for e in sdk.events)
    assert not any(e[1] == "outcome_observed" for e in sdk.events)

def test_meta_version():
    assert FRAMEWORK == 'pydantic-ai' and VERSION == '0.1.0'
    assert _commit({"a":1, "b":2}) == _commit({"b":2, "a":1})

def test_canonical_rejects_unsupported_objects_and_distinguishes_binary():
    from pydantic_ai_loopgrid.integration import _canonical
    with pytest.raises(TypeError): _canonical({'a': object()})
    assert _canonical({'a': b'first'}) != _canonical({'a': b'second'})


def test_post_execution_write_failure_requires_reconciliation():
    from pydantic_ai_loopgrid import EvidencePersistenceError
    bridge, sdk, did = create()
    sdk.fail_kind = 'tool_executed'
    calls = []
    async def action(a):
        calls.append(a)
        return {'receipt': 'downstream-sandbox-id'}
    with pytest.raises(EvidencePersistenceError) as err:
        run(execute(bridge.capability(did), action=action))
    assert len(calls) == 1  # Actual action ran; never describe this as stopped.
    assert err.value.execution_occurred is True
    assert err.value.reconciliation_required is True
    assert err.value.decision_id == did
    assert err.value.tool_name == 'refund'
    assert err.value.result_commitment['sha256'] == _commit({'receipt': 'downstream-sandbox-id'})['sha256']
    assert not any(e[1] == 'tool_executed' for e in sdk.events)
    # Reservation is spent. Automatic blind retry is prohibited.
    with pytest.raises(EvidenceGateError):
        run(execute(bridge.capability(did), action=action))
    assert len(calls) == 1


def test_post_execution_unsupported_result_requires_reconciliation():
    from pydantic_ai_loopgrid import EvidencePersistenceError
    bridge, sdk, did = create()
    calls = []
    async def action(a):
        calls.append(a)
        return object()  # cannot produce a stable commitment
    with pytest.raises(EvidencePersistenceError) as err:
        run(execute(bridge.capability(did), action=action))
    assert err.value.execution_occurred is True
    assert err.value.result_commitment is None
    assert len(calls) == 1
    assert not any(e[1] == 'tool_executed' for e in sdk.events)


def test_tool_exception_keeps_error_and_consumes_reservation():
    bridge, sdk, did = create()
    async def broken(a):
        raise ValueError('downstream tool failed')
    with pytest.raises(ValueError, match='downstream tool failed') as err:
        run(execute(bridge.capability(did), action=broken))
    assert any('outcome unverified' in note for note in getattr(err.value, '__notes__', []))
    with pytest.raises(EvidenceGateError):
        run(execute(bridge.capability(did), action=broken))


def test_sqlite_persists_authorization_and_consumed_budget(tmp_path):
    from pydantic_ai_loopgrid import SQLiteAuthorizationStore
    db = tmp_path / 'authorizations.db'
    sdk = SDK()
    store1 = SQLiteAuthorizationStore(db)
    bridge = LoopGridPydanticAI(client=sdk, workspace_id='w1', authorization_store=store1)
    did = bridge.start_decision(
        decision_type='test', agent={'id': 'a'}, authority={'scope': 'demo'},
        model={'name': 'local'}, context={'key': 'value'},
        proposed_action={'tool': 'refund', 'arguments': {'amount':25}},
        policy={'policy_id':'policy', 'version':'1', 'decision':'auto_allowed'},
    )['decision_id']
    second = LoopGridPydanticAI(client=sdk, workspace_id='w1',
                                authorization_store=SQLiteAuthorizationStore(db))
    # Can resume a decision using a new bridge instance without re-authorizing.
    assert run(execute(second.capability(did)))['sandbox']
    third = LoopGridPydanticAI(client=sdk, workspace_id='w1',
                               authorization_store=SQLiteAuthorizationStore(db))
    with pytest.raises(EvidenceGateError, match='invocation limit'):
        run(execute(third.capability(did)))
    with pytest.raises(ValueError, match='different authorized action'):
        third.authorization_store.register('w1', did, 'delete', 'bogus', 1)
    # No cross-workspace state leakage.
    assert third.authorization_store.get('other-workspace', did) is None


def test_sqlite_parallel_bridges_reserve_once(tmp_path):
    from pydantic_ai_loopgrid import SQLiteAuthorizationStore
    db = tmp_path / 'shared-authorizations.db'
    sdk = SDK()
    primary = LoopGridPydanticAI(client=sdk, workspace_id='w',
                                 authorization_store=SQLiteAuthorizationStore(db))
    did = primary.start_decision(
        decision_type='test', agent={'id':'a'}, authority={'scope':'demo'},
        model={'name':'local'}, context={'x':'y'},
        proposed_action={'tool':'refund', 'arguments':{'amount':25}},
        policy={'policy_id':'p', 'version':'1', 'decision':'auto_allowed'},
    )['decision_id']
    bridges = [LoopGridPydanticAI(client=sdk, workspace_id='w',
                authorization_store=SQLiteAuthorizationStore(db)) for _ in range(6)]
    import threading
    lock = threading.Lock()
    tally = []
    async def action(a):
        with lock:
            tally.append(a)
        await asyncio.sleep(.01)
        return {'ok':True}
    def attempt(b):
        try:
            return run(execute(b.capability(did), action=action)) == {'ok':True}
        except EvidenceGateError:
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, bridges))
    assert outcomes.count(True) == 1
    assert len(tally) == 1


def test_sqlite_approval_survives_new_instance_and_rejection_persists(tmp_path):
    from pydantic_ai_loopgrid import SQLiteAuthorizationStore
    sdk = SDK()
    store_path = tmp_path / 'shared.db'
    bridge = LoopGridPydanticAI(client=sdk, workspace_id='w',
                                authorization_store=SQLiteAuthorizationStore(store_path))
    did = bridge.start_decision(
        decision_type='review', agent={'id':'a'}, authority={'scope':'demo'},
        model={'name':'local'}, context={'prompt':'sha256:test'},
        proposed_action={'tool':'refund', 'arguments':{'amount':25}},
        policy={'policy_id':'p', 'version':'1', 'decision':'human_approval_required'},
    )['decision_id']
    b2 = LoopGridPydanticAI(client=sdk, workspace_id='w',
                            authorization_store=SQLiteAuthorizationStore(store_path))
    with pytest.raises(EvidenceGateError, match='approved human review'):
        run(execute(b2.capability(did)))
    bridge.record_human_review(did, reviewer='test-reviewer', approved=False)
    with pytest.raises(EvidenceGateError, match='rejected'):
        run(execute(b2.capability(did)))
