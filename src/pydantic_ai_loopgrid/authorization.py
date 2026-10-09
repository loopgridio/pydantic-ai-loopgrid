"""Pluggable, atomic authorization counters for LoopGrid Pydantic AI.

The SQLite store supports independent processes sharing a *local* filesystem
(database on a network filesystem is not a supported distributed lock). The
application and database permissions must protect this trusted state.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, replace
from contextlib import closing
from pathlib import Path
from typing import Protocol, Any, Callable


class AuthorizationDenied(RuntimeError):
    """Authorization is absent, mismatched or already consumed."""


@dataclass
class ActionState:
    name: str
    arguments_sha256: str
    limit: int
    remaining: int
    policy: str | None = None
    approved: bool = False
    rejected: bool = False


class AuthorizationStore(Protocol):
    def register(self, workspace: str, decision: str, name: str, args_sha256: str, limit: int) -> None: ...
    def get(self, workspace: str, decision: str) -> ActionState | None: ...
    def set_policy(self, workspace: str, decision: str, verdict: str) -> None: ...
    def set_review(self, workspace: str, decision: str, approved: bool) -> None: ...
    def reserve(self, workspace: str, decision: str, name: str, args_sha256: str) -> int: ...


def _check(action: ActionState | None, name: str, args_sha256: str) -> None:
    if action is None:
        raise AuthorizationDenied("decision not bound to an authorized action")
    if name != action.name or args_sha256 != action.arguments_sha256:
        raise AuthorizationDenied("tool name or arguments differ from authorized decision")
    if action.rejected:
        raise AuthorizationDenied("host reviewer rejected the action")
    if not (action.policy == 'auto_allowed' or
            (action.policy == 'human_approval_required' and action.approved)):
        raise AuthorizationDenied("persisted policy or approved human review required")
    if action.remaining <= 0:
        raise AuthorizationDenied("authorized invocation limit reached")


class InMemoryAuthorizationStore:
    """Atomic within one Python process, not across restarts/workers."""
    def __init__(self) -> None:
        self._actions: dict[tuple[str, str], ActionState] = {}
        self._lock = threading.RLock()

    def register(self, workspace: str, decision: str, name: str, args_sha256: str, limit: int) -> None:
        with self._lock:
            key = workspace, decision
            old = self._actions.get(key)
            if old:
                if (old.name, old.arguments_sha256, old.limit) != (name, args_sha256, limit):
                    raise ValueError("decision ID already has a different authorized action")
                return
            self._actions[key] = ActionState(name, args_sha256, limit, limit)

    def get(self, workspace: str, decision: str) -> ActionState | None:
        with self._lock:
            action = self._actions.get((workspace, decision))
            return replace(action) if action else None

    def set_policy(self, workspace: str, decision: str, verdict: str) -> None:
        with self._lock:
            action = self._actions.get((workspace, decision))
            if not action:
                raise ValueError('unknown decision')
            if action.policy is not None:
                raise ValueError('policy already recorded for this decision')
            action.policy = verdict

    def set_review(self, workspace: str, decision: str, approved: bool) -> None:
        with self._lock:
            action = self._actions.get((workspace, decision))
            if not action or action.policy != 'human_approval_required':
                raise ValueError('human review requires a human_approval_required policy')
            if action.approved or action.rejected:
                raise ValueError('review already resolved')
            action.approved = approved
            action.rejected = not approved

    def reserve(self, workspace: str, decision: str, name: str, args_sha256: str) -> int:
        with self._lock:
            action = self._actions.get((workspace, decision))
            _check(action, name, args_sha256)
            assert action is not None
            action.remaining -= 1
            return action.limit - action.remaining


class SQLiteAuthorizationStore:
    """SQLite-backed single-host authorization ledger with atomic reservations.

    Use a stable absolute path on local disk, provision secure file/directory
    permissions, and use the same path across processes. A consumed reservation
    remains consumed after a crash (no automatic replay of side effects).
    Policy/review writes to Core and this store are not a distributed transaction;
    any interruption in the middle requires operator reconciliation, never a
    guessed approval or reset of consumed invocations.
    """
    def __init__(self, path: str | Path) -> None:
        if str(path) == ':memory:':
            raise ValueError('SQLiteAuthorizationStore requires a persistent file path')
        self.path = str(Path(path).expanduser().resolve())
        with closing(self._connect()) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS authorizations (
                workspace TEXT NOT NULL, decision TEXT NOT NULL,
                tool TEXT NOT NULL, args_sha256 TEXT NOT NULL,
                call_limit INTEGER NOT NULL CHECK(call_limit>0),
                remaining INTEGER NOT NULL CHECK(remaining>=0),
                policy TEXT, approved INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(workspace, decision)
            )''')

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.execute('PRAGMA busy_timeout=15000')
        return conn

    def _transaction(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with closing(self._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                result = fn(conn)
                conn.execute('COMMIT')
                return result
            except BaseException:
                conn.execute('ROLLBACK')
                raise

    @staticmethod
    def _load(conn: sqlite3.Connection, workspace: str, decision: str) -> ActionState | None:
        row = conn.execute('''SELECT tool,args_sha256,call_limit,remaining,policy,approved,rejected
                              FROM authorizations WHERE workspace=? AND decision=?''',
                           (workspace, decision)).fetchone()
        if row is None:
            return None
        return ActionState(row[0], row[1], row[2], row[3], row[4], bool(row[5]), bool(row[6]))

    def get(self, workspace: str, decision: str) -> ActionState | None:
        with closing(self._connect()) as conn:
            return self._load(conn, workspace, decision)

    def register(self, workspace: str, decision: str, name: str, args_sha256: str, limit: int) -> None:
        def op(conn: sqlite3.Connection) -> None:
            old = self._load(conn, workspace, decision)
            if old:
                if (old.name, old.arguments_sha256, old.limit) != (name, args_sha256, limit):
                    raise ValueError('decision ID already has a different authorized action')
                return
            conn.execute('''INSERT INTO authorizations
                (workspace,decision,tool,args_sha256,call_limit,remaining) VALUES (?,?,?,?,?,?)''',
                (workspace, decision, name, args_sha256, limit, limit))
        self._transaction(op)

    def set_policy(self, workspace: str, decision: str, verdict: str) -> None:
        def op(conn: sqlite3.Connection) -> None:
            result = conn.execute('''UPDATE authorizations SET policy=?
                WHERE workspace=? AND decision=? AND policy IS NULL''', (verdict, workspace, decision))
            if result.rowcount != 1:
                raise ValueError('unknown decision or policy already recorded')
        self._transaction(op)

    def set_review(self, workspace: str, decision: str, approved: bool) -> None:
        def op(conn: sqlite3.Connection) -> None:
            result = conn.execute('''UPDATE authorizations SET approved=?,rejected=?
                WHERE workspace=? AND decision=? AND policy='human_approval_required'
                AND approved=0 AND rejected=0''',
                (int(approved), int(not approved), workspace, decision))
            if result.rowcount != 1:
                raise ValueError('unknown decision, wrong policy, or review already resolved')
        self._transaction(op)

    def reserve(self, workspace: str, decision: str, name: str, args_sha256: str) -> int:
        def op(conn: sqlite3.Connection) -> int:
            action = self._load(conn, workspace, decision)
            _check(action, name, args_sha256)
            assert action is not None
            conn.execute('''UPDATE authorizations SET remaining=remaining-1
                WHERE workspace=? AND decision=?''', (workspace, decision))
            return action.limit - action.remaining + 1
        return self._transaction(op)
