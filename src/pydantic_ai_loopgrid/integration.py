"""Pydantic AI native capability for signed LoopGrid decision evidence.

Pydantic AI executes the model and tools. The host supplies authentic authority,
policy, reviewer, and external outcome evidence. LoopGrid Core seals and verifies.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import decimal
import dataclasses
import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field, replace
from .authorization import (AuthorizationStore, AuthorizationDenied,
                            InMemoryAuthorizationStore, SQLiteAuthorizationStore)
from typing import Any, Mapping

from loopgrid import LoopGrid
from pydantic_ai.capabilities import AbstractCapability

VERSION = "0.1.0"
FRAMEWORK = "pydantic-ai"


class EvidenceGateError(RuntimeError):
    """Tool continuation was refused because trusted evidence is unavailable."""


def _need(field: str, value: Any) -> None:
    if value is None or value == "" or value == {} or value == []:
        raise ValueError(f"{field} is required")


def _json_ready(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return _json_ready(obj.model_dump(mode="json"))
    if isinstance(obj, (bytes, bytearray)):
        return {"bytes_base64": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, (datetime.date, datetime.datetime, datetime.time)):
        return {"datetime_iso": obj.isoformat()}
    if isinstance(obj, decimal.Decimal):
        return {"decimal": str(obj)}
    if isinstance(obj, uuid.UUID):
        return {"uuid": str(obj)}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _json_ready(dataclasses.asdict(obj))
    if isinstance(obj, Mapping):
        return {str(k): _json_ready(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_ready(item) for item in obj]
    # Never collapse different opaque values to the same authorization digest.
    # Reject unsupported objects rather than comparing only their type name.
    raise TypeError(f"unsupported evidence/authorization value: {type(obj).__qualname__}")


def _canonical(obj: Any) -> bytes:
    return json.dumps(_json_ready(obj), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _commit(obj: Any) -> dict[str, Any]:
    raw = _canonical(obj)
    return {"algorithm": "sha256", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _idem(*parts: Any) -> str:
    return "lg-pydantic-ai-" + hashlib.sha256(_canonical(parts)).hexdigest()


class EvidencePersistenceError(EvidenceGateError):
    """Evidence is incomplete after a tool returned; reconcile downstream state.

    A caller must never blindly retry a consequential action in response.
    """
    def __init__(self, decision_id: str, tool_name: str, tool_call_id: str | None,
                 result_commitment: dict[str, Any] | None, detail: str) -> None:
        super().__init__(detail)
        self.decision_id = decision_id
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.result_commitment = result_commitment
        self.execution_occurred = True
        self.reconciliation_required = True


class LoopGridPydanticAI:
    """Host-side evidence authority for a consequential Pydantic AI action.

    Start a decision once, write an authentic policy and human review if needed,
    then pass `bridge.capability(decision_id)` to your Pydantic AI agent.
    Only tool calls with exact authorized names/arguments may proceed.
    """

    def __init__(
        self, *, client: LoopGrid | None = None,
        base_url: str = "http://127.0.0.1:8000", api_key: str | None = None,
        workspace_id: str = "default", agent_id: str = "pydantic-agent",
        authorization_store: AuthorizationStore | None = None,
    ) -> None:
        _need("workspace_id", workspace_id)
        _need("agent_id", agent_id)
        self.client = client if client is not None else LoopGrid(
            base_url=base_url, api_key=api_key, workspace_id=workspace_id,
        )
        self.workspace_id = workspace_id
        self.agent_id = agent_id
        self.authorization_store = authorization_store if authorization_store is not None else InMemoryAuthorizationStore()
        self._lock = threading.RLock()

    def start_decision(
        self, *, decision_type: str, agent: Mapping[str, Any],
        authority: Mapping[str, Any], model: Mapping[str, Any],
        context: Mapping[str, Any], proposed_action: Mapping[str, Any],
        policy: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        privacy_mode: str = "redacted", idempotency_key: str | None = None,
        max_tool_calls: int = 1,
    ) -> dict[str, Any]:
        _need("decision_type", decision_type)
        _need("agent.id", agent.get("id"))
        _need("authority", authority)
        _need("model.name", model.get("name"))
        _need("context", context)
        _need("proposed_action", proposed_action)
        name, args = proposed_action.get("tool"), proposed_action.get("arguments")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("proposed_action.tool must be a nonempty tool name")
        if not isinstance(args, Mapping):
            raise ValueError("proposed_action.arguments must be a mapping")
        if isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int) or max_tool_calls < 1:
            raise ValueError("max_tool_calls must be a positive integer")
        md = dict(metadata or {})
        md.update({"framework": FRAMEWORK, "integration": "pydantic-ai-loopgrid", "integration_version": VERSION})
        recorded = self.client.record_decision(
            decision_type=decision_type, agent=dict(agent), authority=dict(authority),
            model=dict(model), context=dict(context), proposed_action=dict(proposed_action),
            metadata=md, privacy_mode=privacy_mode, idempotency_key=idempotency_key,
        )
        decision_id = recorded["decision_id"]
        self.authorization_store.register(
            self.workspace_id, decision_id, name, _commit(args)["sha256"], max_tool_calls,
        )
        if policy is not None:
            self.record_policy(decision_id, policy)
        return recorded

    def record_policy(self, decision_id: str, policy: Mapping[str, Any]) -> Any:
        """Persist host-evaluated policy before enabling any execution."""
        p = dict(policy)
        decision = p.get("decision")
        if decision not in {"auto_allowed", "human_approval_required", "blocked"}:
            raise ValueError("policy.decision must be auto_allowed, human_approval_required, or blocked")
        _need("policy_id", p.get("policy_id"))
        _need("policy version", p.get("version"))
        with self._lock:
            action = self.authorization_store.get(self.workspace_id, decision_id)
            if action is None:
                raise ValueError("unknown decision")
            if action.policy is not None:
                raise ValueError("policy already recorded for this decision")
            result = self.client.policy_evaluated(decision_id, p, actor_id=str(p["policy_id"]))
            self.authorization_store.set_policy(self.workspace_id, decision_id, decision)
        return result

    def record_human_review(self, decision_id: str, *, reviewer: str,
                            approved: bool, reason: str = "") -> Any:
        """Host must authenticate the reviewer and record the genuine verdict."""
        _need("reviewer", reviewer)
        if not isinstance(approved, bool):
            raise ValueError("approved must be bool")
        with self._lock:
            action = self.authorization_store.get(self.workspace_id, decision_id)
            if action is None or action.policy != "human_approval_required":
                raise ValueError("human review requires a human_approval_required policy")
            if action.approved or action.rejected:
                raise ValueError("review already resolved")
            if approved:
                result = self.client.human_approved(decision_id, reviewer, reason)
            else:
                result = self.client.human_rejected(decision_id, reviewer, reason)
            self.authorization_store.set_review(self.workspace_id, decision_id, approved)
        return result

    def record_observed_outcome(self, decision_id: str, *, outcome: Mapping[str, Any],
                                observer: str, receipt_id: str) -> Any:
        """Write an observed downstream outcome supplied by the real host observer."""
        _need("observer", observer)
        _need("receipt_id", receipt_id)
        _need("outcome", outcome)
        payload = {**dict(outcome), "observer": observer, "receipt_id": receipt_id}
        return self.client.outcome_observed(decision_id, payload, actor_id=observer)

    def capability(self, decision_id: str) -> "LoopGridCapability":
        with self._lock:
            if self.authorization_store.get(self.workspace_id, decision_id) is None:
                raise ValueError("unknown decision")
        return LoopGridCapability(bridge=self, decision_id=decision_id)

    def _reserve(self, decision_id: str, name: str, args: Mapping[str, Any]) -> int:
        try:
            return self.authorization_store.reserve(
                self.workspace_id, decision_id, name, _commit(args)["sha256"],
            )
        except AuthorizationDenied as exc:
            raise EvidenceGateError(str(exc)) from None
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceGateError(
                f"durable authorization store unavailable ({type(exc).__name__}); action stopped"
            ) from None

    async def _event(self, decision_id: str, kind: str, payload: dict[str, Any],
                     actor_type: str, actor_id: str, idem: str) -> Any:
        try:
            return await asyncio.to_thread(
                self.client.add_event, decision_id, kind, payload,
                actor_type=actor_type, actor_id=actor_id, idempotency_key=idem,
            )
        except Exception as exc:
            raise EvidenceGateError(
                f"LoopGrid evidence write failed ({type(exc).__name__}); action stopped"
            ) from None

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        return self.client.get_decision(decision_id)

    def export_evidence(self, decision_id: str, include_payloads: bool = True) -> bytes:
        return self.client.export_evidence(decision_id, include_payloads=include_payloads)


@dataclass(kw_only=True)
class LoopGridCapability(AbstractCapability[Any]):
    """Native Pydantic AI capability: model + tool lifecycle to LoopGrid Core.

    `for_run` creates new per-run state while authorization remains bound to the
    decision and enforced across runs by the bridge. Suitable for approval resume.
    """
    bridge: LoopGridPydanticAI
    decision_id: str
    _run_id: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False)
    _ordinal: int = field(default=0, repr=False)
    id: str = "loopgrid-evidence"
    defer_loading: bool = False

    async def for_run(self, ctx: Any) -> "LoopGridCapability":
        return replace(self, _run_id=uuid.uuid4().hex, _ordinal=0)

    async def after_model_request(self, ctx: Any, *, request_context: Any,
                                  response: Any) -> Any:
        self._ordinal += 1
        model = getattr(response, "model_name", None) or getattr(request_context.model, "model_name", None)
        payload = {"framework": FRAMEWORK, "source": "pydantic_ai.AbstractCapability.after_model_request",
                   "model": str(model) if model else None, "status": "completed",
                   "response_commitment": _commit(getattr(response, "parts", response))}
        await self.bridge._event(
            self.decision_id, "model_completed", payload, "agent", self.bridge.agent_id,
            _idem(self.decision_id, self._run_id, "model_completed", self._ordinal),
        )
        return response

    async def wrap_tool_execute(self, ctx: Any, *, call: Any, tool_def: Any,
                                args: dict[str, Any], handler: Any) -> Any:
        name = getattr(call, "tool_name", None)
        if not isinstance(name, str) or not name:
            raise EvidenceGateError("tool has no identifiable Pydantic AI name")
        if not isinstance(args, Mapping):
            raise EvidenceGateError("tool arguments must be validated mapping")
        call_id = getattr(call, "tool_call_id", None)
        self._ordinal += 1
        ident = str(call_id) if call_id else f"{self._run_id}-{self._ordinal}"
        common = {"framework": FRAMEWORK,
                  "source": "pydantic_ai.AbstractCapability.wrap_tool_execute",
                  "tool_name": name, "tool_call_id": str(call_id) if call_id else None,
                  "arguments_commitment": _commit(args)}
        # Core may reject a tool_requested event for a blocked/awaiting review
        # decision. In any case tool execution must never proceed without it.
        await self.bridge._event(
            self.decision_id, "tool_requested", common, "tool", name,
            _idem(self.decision_id, self._run_id, "tool_requested", ident),
        )
        occurrence = self.bridge._reserve(self.decision_id, name, args)
        try:
            result = await handler(args)  # native Pydantic AI tool execution
        except BaseException as exc:
            # Attempted execution is not a successful result or outcome. The
            # reservation is consumed and must not be blindly retried.
            if hasattr(exc, "add_note"):
                exc.add_note(f"LoopGrid decision {self.decision_id}: tool attempted; "
                             "outcome unverified; reconcile with downstream system")
            raise
        # Side effect has ALREADY happened here. Any evidence failure must be
        # distinguishable from a failure *before* executing the handler.
        commitment = None
        try:
            commitment = _commit(result)
            completed = {**common, "occurrence": occurrence, "status": "returned",
                         "result_commitment": commitment, "external_outcome_verified": False}
            await self.bridge._event(
                self.decision_id, "tool_executed", completed, "tool", name,
                _idem(self.decision_id, self._run_id, "tool_executed", ident),
            )
        except Exception as exc:
            raise EvidencePersistenceError(
                self.decision_id, name, str(call_id) if call_id else None,
                commitment, "tool returned but LoopGrid execution evidence was not confirmed; "
                "reconciliation required before retry",
            ) from exc
        return result


__all__ = ["LoopGridPydanticAI", "LoopGridCapability", "EvidenceGateError", "EvidencePersistenceError", "SQLiteAuthorizationStore", "FRAMEWORK", "VERSION"]
