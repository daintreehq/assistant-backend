"""The durable run store: an append-only event log plus transactional projections.

SQLite (WAL), single connection, single writer. The ``events`` table is the
source of truth; ``runs``/``turns``/``leases``/``approvals``/``executors``/
``wakeups`` are projections maintained in the same transaction as each append.
Store methods never raise for contended state transitions — they return a
:class:`StoreOutcome` whose ``kind`` the caller maps to an HTTP status or a
supervisor action. Events in an outcome are already committed; the caller
publishes them to the live hub (publish-after-commit).

Async surface over a sync core: every operation runs the sync transaction in a
worker thread under one asyncio lock (serialized, which is exactly SQLite's
own concurrency model here).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .models import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_RESOLVED,
    EVENT_ASSISTANT_MESSAGE,
    EVENT_LEASE_CLAIMED,
    EVENT_LEASE_CREATED,
    EVENT_LEASE_RELEASED,
    EVENT_ROUND_FAILED,
    EVENT_ROUND_STARTED,
    EVENT_RUN_CREATED,
    EVENT_RUN_STATUS,
    EVENT_SELECTION_COMPLETED,
    EVENT_TOOL_RESULT,
    EVENT_TURN_CANCELLED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_SUBMITTED,
    EVENT_WAKE_CANCELLED,
    EVENT_WAKE_FIRED,
    EVENT_WAKE_SCHEDULED,
    LEASE_TERMINAL,
    ApprovalRecord,
    EventRecord,
    ExecutorRecord,
    LeaseDraft,
    LeaseRecord,
    RunRecord,
    StoreOutcome,
    WakeupRecord,
    canonical_json,
    new_approval_id,
    new_lease_id,
    sha256_hex,
)

T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  session_id TEXT,
  project_id TEXT,
  title TEXT,
  active_skills TEXT NOT NULL DEFAULT '[]',
  needs_selection INTEGER NOT NULL DEFAULT 0,
  selection_policy TEXT NOT NULL DEFAULT 'new_instruction',
  selection_force INTEGER NOT NULL DEFAULT 0,
  current_round INTEGER NOT NULL DEFAULT 0,
  current_turn_id TEXT,
  current_turn_source TEXT NOT NULL DEFAULT 'user',
  context TEXT NOT NULL DEFAULT '{}',
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ts REAL NOT NULL,
  type TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE TABLE IF NOT EXISTS turns (
  run_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  seq INTEGER NOT NULL,
  PRIMARY KEY (run_id, turn_id)
);
CREATE TABLE IF NOT EXISTS leases (
  lease_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  round INTEGER NOT NULL,
  tool_call_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  arguments TEXT NOT NULL,
  risk TEXT,
  status TEXT NOT NULL,
  requires_approval INTEGER NOT NULL DEFAULT 0,
  approval_id TEXT,
  executor_id TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  claim_expires_at REAL,
  deadline REAL NOT NULL,
  idempotency_key TEXT NOT NULL,
  result_status TEXT,
  result_content TEXT,
  result_hash TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (run_id, round, tool_call_id)
);
CREATE INDEX IF NOT EXISTS idx_leases_status ON leases(status, deadline);
CREATE INDEX IF NOT EXISTS idx_leases_run ON leases(run_id, round);
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  lease_id TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  note TEXT,
  requested_at REAL NOT NULL,
  resolved_at REAL
);
CREATE TABLE IF NOT EXISTS executors (
  executor_id TEXT PRIMARY KEY,
  project_id TEXT,
  client TEXT NOT NULL DEFAULT '{}',
  tools TEXT NOT NULL DEFAULT '[]',
  capabilities TEXT NOT NULL DEFAULT '{}',
  ttl_seconds REAL NOT NULL,
  last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wakeups (
  wakeup_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  fire_at REAL NOT NULL,
  note TEXT,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  fired_at REAL
);
CREATE INDEX IF NOT EXISTS idx_wakeups_due ON wakeups(status, fire_at);
"""

_BUSY_STATUSES = ("running", "awaiting_tools", "awaiting_approval")


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


class RunStore:
    """Durable storage for runs, events, leases, approvals, executors, wakeups."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = str(path)
        self._clock = clock
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ---- lifecycle ------------------------------------------------------

    def open(self) -> None:
        """Connect and apply the schema (sync; called from container build)."""
        if self._conn is not None:
            return
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn

    async def aclose(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    # ---- transaction plumbing -------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("RunStore is not open")
        return self._conn

    def _tx(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._connection()
        try:
            out = fn(conn)
            conn.commit()
            return out
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(self._tx, fn)

    # ---- event append (always inside a caller's transaction) ------------

    def _append(
        self, conn: sqlite3.Connection, run_id: str, type_: str, payload: dict[str, Any]
    ) -> EventRecord:
        now = self._clock()
        cur = conn.execute(
            "INSERT INTO events (run_id, ts, type, payload) VALUES (?, ?, ?, ?)",
            (run_id, now, type_, json.dumps(payload, separators=(",", ":"), ensure_ascii=False)),
        )
        seq = int(cur.lastrowid or 0)
        return EventRecord(run_id=run_id, type=type_, payload=payload, ts=now, seq=seq)

    def _set_status(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        status: str,
        reason: str,
        events: list[EventRecord],
    ) -> None:
        conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (status, self._clock(), run_id),
        )
        events.append(
            self._append(conn, run_id, EVENT_RUN_STATUS, {"status": status, "reason": reason})
        )

    # ---- row mappers ------------------------------------------------------

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            status=row["status"],
            session_id=row["session_id"],
            project_id=row["project_id"],
            title=row["title"],
            active_skills=_loads(row["active_skills"], []),
            needs_selection=bool(row["needs_selection"]),
            selection_policy=row["selection_policy"],
            selection_force=bool(row["selection_force"]),
            current_round=int(row["current_round"]),
            current_turn_id=row["current_turn_id"],
            current_turn_source=row["current_turn_source"],
            context=_loads(row["context"], {}),
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _lease_record(row: sqlite3.Row) -> LeaseRecord:
        return LeaseRecord(
            lease_id=row["lease_id"],
            run_id=row["run_id"],
            round=int(row["round"]),
            tool_call_id=row["tool_call_id"],
            tool_name=row["tool_name"],
            arguments=row["arguments"],
            risk=row["risk"],
            status=row["status"],
            requires_approval=bool(row["requires_approval"]),
            approval_id=row["approval_id"],
            executor_id=row["executor_id"],
            attempt=int(row["attempt"]),
            claim_expires_at=row["claim_expires_at"],
            deadline=row["deadline"],
            idempotency_key=row["idempotency_key"],
            result_status=row["result_status"],
            result_content=row["result_content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _approval_record(row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=row["approval_id"],
            run_id=row["run_id"],
            lease_id=row["lease_id"],
            status=row["status"],
            reason=row["reason"],
            note=row["note"],
            requested_at=row["requested_at"],
            resolved_at=row["resolved_at"],
        )

    @staticmethod
    def _event_record(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            run_id=row["run_id"],
            type=row["type"],
            payload=_loads(row["payload"], {}),
            ts=row["ts"],
            seq=int(row["seq"]),
        )

    @staticmethod
    def _wakeup_record(row: sqlite3.Row) -> WakeupRecord:
        return WakeupRecord(
            wakeup_id=row["wakeup_id"],
            run_id=row["run_id"],
            fire_at=row["fire_at"],
            note=row["note"],
            status=row["status"],
            created_at=row["created_at"],
            fired_at=row["fired_at"],
        )

    @staticmethod
    def _executor_record(row: sqlite3.Row) -> ExecutorRecord:
        return ExecutorRecord(
            executor_id=row["executor_id"],
            project_id=row["project_id"],
            client=_loads(row["client"], {}),
            tools=_loads(row["tools"], []),
            capabilities=_loads(row["capabilities"], {}),
            ttl_seconds=row["ttl_seconds"],
            last_seen=row["last_seen"],
        )

    def _get_run_row(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
        cur = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        row: sqlite3.Row | None = cur.fetchone()
        return row

    # ---- runs -------------------------------------------------------------

    async def create_run(
        self,
        *,
        run_id: str,
        session_id: str | None,
        project_id: str | None,
        title: str | None,
    ) -> StoreOutcome:
        def op(conn: sqlite3.Connection) -> StoreOutcome:
            now = self._clock()
            try:
                conn.execute(
                    "INSERT INTO runs (run_id, status, session_id, project_id, title,"
                    " created_at, updated_at) VALUES (?, 'idle', ?, ?, ?, ?, ?)",
                    (run_id, session_id, project_id, title, now, now),
                )
            except sqlite3.IntegrityError:
                return StoreOutcome(kind="exists")
            event = self._append(
                conn,
                run_id,
                EVENT_RUN_CREATED,
                {"session_id": session_id, "project_id": project_id, "title": title},
            )
            row = self._get_run_row(conn, run_id)
            assert row is not None
            return StoreOutcome(kind="created", events=[event], run=self._run_record(row))

        return await self._run(op)

    async def get_run(self, run_id: str) -> RunRecord | None:
        def op(conn: sqlite3.Connection) -> RunRecord | None:
            row = self._get_run_row(conn, run_id)
            return self._run_record(row) if row is not None else None

        return await self._run(op)

    async def list_runs(self, limit: int = 100) -> list[RunRecord]:
        def op(conn: sqlite3.Connection) -> list[RunRecord]:
            cur = conn.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?", (int(limit),)
            )
            return [self._run_record(r) for r in cur.fetchall()]

        return await self._run(op)

    async def runs_in_status(self, statuses: list[str]) -> list[RunRecord]:
        def op(conn: sqlite3.Connection) -> list[RunRecord]:
            marks = ",".join("?" for _ in statuses)
            cur = conn.execute(f"SELECT * FROM runs WHERE status IN ({marks})", statuses)
            return [self._run_record(r) for r in cur.fetchall()]

        return await self._run(op)

    # ---- turn submission ---------------------------------------------------

    async def submit_turn(
        self,
        *,
        run_id: str,
        turn_id: str,
        source: str,
        messages: list[dict[str, Any]],
        context_update: dict[str, Any],
        selection_policy: str,
        selection_force: bool,
        title_hint: str | None,
    ) -> StoreOutcome:
        """Append a turn (idempotent on turn_id) and move the run to ``running``.

        Kinds: ``accepted`` | ``duplicate`` | ``turn_conflict`` | ``busy`` |
        ``not_found``.
        """

        def op(conn: sqlite3.Connection) -> StoreOutcome:
            row = self._get_run_row(conn, run_id)
            if row is None:
                return StoreOutcome(kind="not_found")
            run = self._run_record(row)

            # The effective context: prior context with the update's non-null
            # sections replacing their predecessors ("turn" always reflects the
            # submitted turn — absent means no per-turn context this turn).
            context = dict(run.context)
            for key in ("runtime", "tools", "generation"):
                if context_update.get(key) is not None:
                    context[key] = context_update[key]
            context["turn"] = context_update.get("turn")

            payload = {
                "turn_id": turn_id,
                "source": source,
                "messages": messages,
                "context": context,
                "selection": {"policy": selection_policy, "force": selection_force},
            }
            # Idempotency hashes the RAW submission, not the merged effective
            # context — a late retry of an old accepted turn must read as a
            # duplicate even if later turns changed the run context since.
            payload_hash = sha256_hex(
                canonical_json(
                    {
                        "turn_id": turn_id,
                        "source": source,
                        "messages": messages,
                        "context_update": context_update,
                        "selection": {"policy": selection_policy, "force": selection_force},
                    }
                )
            )

            cur = conn.execute(
                "SELECT payload_hash, seq FROM turns WHERE run_id = ? AND turn_id = ?",
                (run_id, turn_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing["payload_hash"] == payload_hash:
                    return StoreOutcome(kind="duplicate", run=run, seq=int(existing["seq"]))
                return StoreOutcome(kind="turn_conflict", run=run)

            if run.status in _BUSY_STATUSES:
                return StoreOutcome(kind="busy", run=run)

            events: list[EventRecord] = []
            event = self._append(conn, run_id, EVENT_TURN_SUBMITTED, payload)
            events.append(event)
            conn.execute(
                "INSERT INTO turns (run_id, turn_id, payload_hash, seq) VALUES (?, ?, ?, ?)",
                (run_id, turn_id, payload_hash, event.seq),
            )
            title = run.title or title_hint
            conn.execute(
                "UPDATE runs SET needs_selection = 1, selection_policy = ?, selection_force = ?,"
                " current_turn_id = ?, current_turn_source = ?, context = ?, title = ?,"
                " last_error = NULL, updated_at = ? WHERE run_id = ?",
                (
                    selection_policy,
                    1 if selection_force else 0,
                    turn_id,
                    source,
                    json.dumps(context, separators=(",", ":"), ensure_ascii=False),
                    title,
                    self._clock(),
                    run_id,
                ),
            )
            self._set_status(conn, run_id, "running", "turn_submitted", events)
            refreshed = self._get_run_row(conn, run_id)
            assert refreshed is not None
            return StoreOutcome(
                kind="accepted", events=events, run=self._run_record(refreshed), seq=event.seq
            )

        return await self._run(op)

    # ---- selection / rounds -------------------------------------------------

    async def record_selection(
        self, run_id: str, *, active_skills: list[str], payload: dict[str, Any]
    ) -> StoreOutcome:
        def op(conn: sqlite3.Connection) -> StoreOutcome:
            events = [self._append(conn, run_id, EVENT_SELECTION_COMPLETED, payload)]
            conn.execute(
                "UPDATE runs SET active_skills = ?, needs_selection = 0, updated_at = ?"
                " WHERE run_id = ?",
                (
                    json.dumps(active_skills, separators=(",", ":")),
                    self._clock(),
                    run_id,
                ),
            )
            return StoreOutcome(kind="recorded", events=events)

        return await self._run(op)

    async def round_state(self, run_id: str, round_no: int) -> tuple[bool, dict[str, Any] | None]:
        """Whether ``round.started`` exists for the round, and the persisted
        assistant message payload if any (recovery resume points)."""

        def op(conn: sqlite3.Connection) -> tuple[bool, dict[str, Any] | None]:
            cur = conn.execute(
                "SELECT payload FROM events WHERE run_id = ? AND type = ?"
                " ORDER BY seq DESC",
                (run_id, EVENT_ROUND_STARTED),
            )
            started = any(
                _loads(r["payload"], {}).get("round") == round_no for r in cur.fetchall()
            )
            cur = conn.execute(
                "SELECT payload FROM events WHERE run_id = ? AND type = ? ORDER BY seq DESC",
                (run_id, EVENT_ASSISTANT_MESSAGE,),
            )
            assistant: dict[str, Any] | None = None
            for r in cur.fetchall():
                payload = _loads(r["payload"], {})
                if payload.get("round") == round_no:
                    assistant = payload
                    break
            return started, assistant

        return await self._run(op)

    async def record_round_started(
        self, run_id: str, round_no: int, payload: dict[str, Any]
    ) -> StoreOutcome:
        """Append ``round.started`` and advance ``current_round``. Idempotent:
        re-driving a crashed round with an identical fingerprint appends nothing."""

        def op(conn: sqlite3.Connection) -> StoreOutcome:
            cur = conn.execute(
                "SELECT payload FROM events WHERE run_id = ? AND type = ? ORDER BY seq DESC",
                (run_id, EVENT_ROUND_STARTED),
            )
            for r in cur.fetchall():
                prior = _loads(r["payload"], {})
                if prior.get("round") == round_no:
                    if prior.get("prompt_fingerprint") == payload.get("prompt_fingerprint"):
                        return StoreOutcome(kind="already")
                    break
            events = [self._append(conn, run_id, EVENT_ROUND_STARTED, payload)]
            conn.execute(
                "UPDATE runs SET current_round = ?, updated_at = ? WHERE run_id = ?",
                (round_no, self._clock(), run_id),
            )
            return StoreOutcome(kind="recorded", events=events)

        return await self._run(op)

    # ---- assistant message + leases (one atomic transition) -----------------

    async def record_assistant_message(
        self,
        *,
        run_id: str,
        round_no: int,
        turn_id: str | None,
        message: dict[str, Any],
        finish_reason: str | None,
        usage: dict[str, Any],
        leases: list[LeaseDraft],
        lease_deadline_seconds: float,
    ) -> StoreOutcome:
        """Persist the round's outcome atomically.

        With tool calls: assistant.message + one lease (+ optional approval) per
        call + status ``awaiting_tools``/``awaiting_approval``. Without: assistant
        .message + turn.completed + status ``idle`` — so there is no crash window
        between "answered" and "turn closed".

        Kinds: ``tools`` | ``final``.
        """

        def op(conn: sqlite3.Connection) -> StoreOutcome:
            now = self._clock()
            events: list[EventRecord] = []
            events.append(
                self._append(
                    conn,
                    run_id,
                    EVENT_ASSISTANT_MESSAGE,
                    {
                        "round": round_no,
                        "turn_id": turn_id,
                        "message": message,
                        "finish_reason": finish_reason,
                        "usage": usage,
                    },
                )
            )
            if not leases:
                events.append(
                    self._append(
                        conn,
                        run_id,
                        EVENT_TURN_COMPLETED,
                        {"turn_id": turn_id, "finish_reason": finish_reason},
                    )
                )
                self._set_status(conn, run_id, "idle", "turn_completed", events)
                return StoreOutcome(kind="final", events=events)

            any_approval = False
            for draft in leases:
                lease_id = new_lease_id()
                status = "awaiting_approval" if draft.requires_approval else "pending"
                idempotency_key = sha256_hex(
                    f"{run_id}:{round_no}:{draft.tool_call_id}:{draft.tool_name}"
                )
                cur = conn.execute(
                    "INSERT OR IGNORE INTO leases (lease_id, run_id, round, tool_call_id,"
                    " tool_name, arguments, risk, status, requires_approval, executor_id,"
                    " attempt, deadline, idempotency_key, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?)",
                    (
                        lease_id,
                        run_id,
                        round_no,
                        draft.tool_call_id,
                        draft.tool_name,
                        draft.arguments,
                        draft.risk,
                        status,
                        1 if draft.requires_approval else 0,
                        now + lease_deadline_seconds,
                        idempotency_key,
                        now,
                        now,
                    ),
                )
                if cur.rowcount == 0:
                    # Crash-recovery re-drive: the lease already exists.
                    continue
                approval_id: str | None = None
                if draft.requires_approval:
                    any_approval = True
                    approval_id = new_approval_id()
                    conn.execute(
                        "INSERT INTO approvals (approval_id, run_id, lease_id, status,"
                        " reason, requested_at) VALUES (?, ?, ?, 'pending', ?, ?)",
                        (
                            approval_id,
                            run_id,
                            lease_id,
                            "background_mutation",
                            now,
                        ),
                    )
                    conn.execute(
                        "UPDATE leases SET approval_id = ? WHERE lease_id = ?",
                        (approval_id, lease_id),
                    )
                events.append(
                    self._append(
                        conn,
                        run_id,
                        EVENT_LEASE_CREATED,
                        {
                            "lease_id": lease_id,
                            "round": round_no,
                            "tool_call_id": draft.tool_call_id,
                            "tool_name": draft.tool_name,
                            "arguments": draft.arguments,
                            "risk": draft.risk,
                            "requires_approval": draft.requires_approval,
                            "approval_id": approval_id,
                            "deadline": now + lease_deadline_seconds,
                            "idempotency_key": idempotency_key,
                        },
                    )
                )
                if approval_id is not None:
                    events.append(
                        self._append(
                            conn,
                            run_id,
                            EVENT_APPROVAL_REQUESTED,
                            {
                                "approval_id": approval_id,
                                "lease_id": lease_id,
                                "tool_name": draft.tool_name,
                                "reason": "background_mutation",
                            },
                        )
                    )
            # A recovery re-drive may find approvals already pending in the table.
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE run_id = ? AND status = 'pending'",
                (run_id,),
            )
            pending_approvals = int(cur.fetchone()["n"])
            waiting = any_approval or pending_approvals > 0
            status = "awaiting_approval" if waiting else "awaiting_tools"
            self._set_status(conn, run_id, status, "tool_calls_pending", events)
            return StoreOutcome(kind="tools", events=events)

        return await self._run(op)

    # ---- turn failure / cancellation ---------------------------------------

    async def record_round_failed(
        self, run_id: str, round_no: int, *, code: str, message: str
    ) -> StoreOutcome:
        def op(conn: sqlite3.Connection) -> StoreOutcome:
            events = [
                self._append(
                    conn,
                    run_id,
                    EVENT_ROUND_FAILED,
                    {"round": round_no, "code": code, "message": message},
                )
            ]
            conn.execute(
                "UPDATE runs SET last_error = ?, updated_at = ? WHERE run_id = ?",
                (f"{code}: {message}", self._clock(), run_id),
            )
            self._set_status(conn, run_id, "failed", "round_failed", events)
            return StoreOutcome(kind="recorded", events=events)

        return await self._run(op)

    async def cancel_turn(self, run_id: str, *, reason: str) -> StoreOutcome:
        """Cancel the in-flight turn: open leases get synthesized cancelled
        results (every tool call must keep a matching tool message), pending
        approvals are declined, and the run parks ``idle``.

        Kinds: ``cancelled`` | ``not_busy`` | ``not_found``.
        """

        def op(conn: sqlite3.Connection) -> StoreOutcome:
            row = self._get_run_row(conn, run_id)
            if row is None:
                return StoreOutcome(kind="not_found")
            run = self._run_record(row)
            if run.status not in _BUSY_STATUSES:
                return StoreOutcome(kind="not_busy", run=run)
            now = self._clock()
            events: list[EventRecord] = []
            cur = conn.execute(
                "SELECT * FROM leases WHERE run_id = ? AND status NOT IN"
                " ('completed','declined','cancelled','expired')",
                (run_id,),
            )
            for lease_row in cur.fetchall():
                lease = self._lease_record(lease_row)
                content = "Tool call cancelled: the turn was cancelled before a result."
                conn.execute(
                    "UPDATE leases SET status = 'cancelled', result_status = 'cancelled',"
                    " result_content = ?, result_hash = ?, updated_at = ? WHERE lease_id = ?",
                    (content, sha256_hex(f"cancelled:{content}"), now, lease.lease_id),
                )
                events.append(
                    self._append(
                        conn,
                        run_id,
                        EVENT_TOOL_RESULT,
                        {
                            "lease_id": lease.lease_id,
                            "round": lease.round,
                            "tool_call_id": lease.tool_call_id,
                            "status": "cancelled",
                            "content": content,
                            "executor_id": None,
                            "synthesized": True,
                        },
                    )
                )
            cur = conn.execute(
                "SELECT approval_id, lease_id FROM approvals WHERE run_id = ?"
                " AND status = 'pending'",
                (run_id,),
            )
            for approval_row in cur.fetchall():
                conn.execute(
                    "UPDATE approvals SET status = 'declined', resolved_at = ?, note = ?"
                    " WHERE approval_id = ?",
                    (now, "turn cancelled", approval_row["approval_id"]),
                )
                events.append(
                    self._append(
                        conn,
                        run_id,
                        EVENT_APPROVAL_RESOLVED,
                        {
                            "approval_id": approval_row["approval_id"],
                            "lease_id": approval_row["lease_id"],
                            "approved": False,
                            "note": "turn cancelled",
                        },
                    )
                )
            events.append(
                self._append(
                    conn,
                    run_id,
                    EVENT_TURN_CANCELLED,
                    {"turn_id": run.current_turn_id, "reason": reason},
                )
            )
            self._set_status(conn, run_id, "idle", "turn_cancelled", events)
            return StoreOutcome(kind="cancelled", events=events, run=run)

        return await self._run(op)

    # ---- leases --------------------------------------------------------

    async def get_lease(self, lease_id: str) -> LeaseRecord | None:
        def op(conn: sqlite3.Connection) -> LeaseRecord | None:
            cur = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,))
            row = cur.fetchone()
            return self._lease_record(row) if row is not None else None

        return await self._run(op)

    async def list_leases(
        self, run_id: str, *, status: str | None = None
    ) -> list[LeaseRecord]:
        def op(conn: sqlite3.Connection) -> list[LeaseRecord]:
            if status is None:
                cur = conn.execute(
                    "SELECT * FROM leases WHERE run_id = ? ORDER BY created_at", (run_id,)
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM leases WHERE run_id = ? AND status = ? ORDER BY created_at",
                    (run_id, status),
                )
            return [self._lease_record(r) for r in cur.fetchall()]

        return await self._run(op)

    async def pending_leases(self, limit: int = 200) -> list[LeaseRecord]:
        """Claimable work, for reconnecting-executor sweeps."""

        def op(conn: sqlite3.Connection) -> list[LeaseRecord]:
            cur = conn.execute(
                "SELECT * FROM leases WHERE status = 'pending' ORDER BY created_at LIMIT ?",
                (int(limit),),
            )
            return [self._lease_record(r) for r in cur.fetchall()]

        return await self._run(op)

    async def claim_lease(
        self, lease_id: str, *, executor_id: str, claim_ttl_seconds: float
    ) -> StoreOutcome:
        """Kinds: ``claimed`` | ``extended`` | ``conflict`` | ``awaiting_approval``
        | ``terminal`` | ``not_found``. A lapsed claim is claimable by anyone
        (the attempt counter increments)."""

        def op(conn: sqlite3.Connection) -> StoreOutcome:
            cur = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,))
            row = cur.fetchone()
            if row is None:
                return StoreOutcome(kind="not_found")
            lease = self._lease_record(row)
            now = self._clock()
            if lease.status in LEASE_TERMINAL:
                return StoreOutcome(kind="terminal", lease=lease)
            if lease.status == "awaiting_approval":
                return StoreOutcome(kind="awaiting_approval", lease=lease)

            events: list[EventRecord] = []
            if lease.status == "claimed":
                held_by_other = lease.executor_id not in (None, executor_id)
                claim_live = (lease.claim_expires_at or 0) > now
                if held_by_other and claim_live:
                    return StoreOutcome(kind="conflict", lease=lease)
                if held_by_other:
                    # Lapsed claim taken over by a different executor.
                    events.append(
                        self._append(
                            conn,
                            lease.run_id,
                            EVENT_LEASE_RELEASED,
                            {
                                "lease_id": lease_id,
                                "executor_id": lease.executor_id,
                                "reason": "claim_expired",
                            },
                        )
                    )
                kind = "extended" if lease.executor_id == executor_id else "claimed"
            else:
                kind = "claimed"

            attempt = lease.attempt + (1 if kind == "claimed" else 0)
            conn.execute(
                "UPDATE leases SET status = 'claimed', executor_id = ?, attempt = ?,"
                " claim_expires_at = ?, updated_at = ? WHERE lease_id = ?",
                (executor_id, attempt, now + claim_ttl_seconds, now, lease_id),
            )
            if kind == "claimed":
                events.append(
                    self._append(
                        conn,
                        lease.run_id,
                        EVENT_LEASE_CLAIMED,
                        {
                            "lease_id": lease_id,
                            "executor_id": executor_id,
                            "attempt": attempt,
                        },
                    )
                )
            cur = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,))
            refreshed = cur.fetchone()
            assert refreshed is not None
            return StoreOutcome(kind=kind, events=events, lease=self._lease_record(refreshed))

        return await self._run(op)

    async def record_lease_result(
        self,
        lease_id: str,
        *,
        executor_id: str,
        status: str,
        content: str,
    ) -> StoreOutcome:
        """Exactly-once result recording.

        Kinds: ``recorded`` | ``duplicate`` (identical resubmission) |
        ``conflict`` (different result after terminal, or another executor's
        live claim) | ``awaiting_approval`` | ``not_found``.

        A result on an UNCLAIMED (pending) lease is deliberately accepted: an
        executor whose claim lapsed after it already performed the work must be
        able to record that outcome — forcing a re-claim would push a second
        executor to re-execute the side effect, which is the exact double-apply
        this fence exists to prevent. The only hard exclusions are another
        executor's LIVE claim and any already-terminal state.
        """

        def op(conn: sqlite3.Connection) -> StoreOutcome:
            cur = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,))
            row = cur.fetchone()
            if row is None:
                return StoreOutcome(kind="not_found")
            lease = self._lease_record(row)
            now = self._clock()
            result_hash = sha256_hex(f"{status}:{content}")

            if lease.status in LEASE_TERMINAL:
                cur = conn.execute(
                    "SELECT result_hash FROM leases WHERE lease_id = ?", (lease_id,)
                )
                prior_hash = cur.fetchone()["result_hash"]
                if prior_hash == result_hash:
                    return StoreOutcome(kind="duplicate", lease=lease)
                return StoreOutcome(kind="conflict", lease=lease)
            if lease.status == "awaiting_approval":
                return StoreOutcome(kind="awaiting_approval", lease=lease)
            if (
                lease.status == "claimed"
                and lease.executor_id not in (None, executor_id)
                and (lease.claim_expires_at or 0) > now
            ):
                return StoreOutcome(kind="conflict", lease=lease)

            terminal = "completed" if status in ("ok", "error") else "declined"
            conn.execute(
                "UPDATE leases SET status = ?, executor_id = ?, result_status = ?,"
                " result_content = ?, result_hash = ?, updated_at = ? WHERE lease_id = ?",
                (terminal, executor_id, status, content, result_hash, now, lease_id),
            )
            events = [
                self._append(
                    conn,
                    lease.run_id,
                    EVENT_TOOL_RESULT,
                    {
                        "lease_id": lease_id,
                        "round": lease.round,
                        "tool_call_id": lease.tool_call_id,
                        "status": status,
                        "content": content,
                        "executor_id": executor_id,
                        "synthesized": False,
                    },
                )
            ]
            cur = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,))
            refreshed = cur.fetchone()
            assert refreshed is not None
            return StoreOutcome(
                kind="recorded", events=events, lease=self._lease_record(refreshed)
            )

        return await self._run(op)

    async def all_round_leases_terminal(self, run_id: str, round_no: int) -> bool:
        def op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM leases WHERE run_id = ? AND round = ?"
                " AND status NOT IN ('completed','declined','cancelled','expired')",
                (run_id, round_no),
            )
            return int(cur.fetchone()["n"]) == 0

        return await self._run(op)

    async def try_begin_continuation(self, run_id: str) -> StoreOutcome:
        """CAS ``awaiting_tools -> running`` so the model continuation fires at
        most once per round. Kinds: ``continuing`` | ``no``."""

        def op(conn: sqlite3.Connection) -> StoreOutcome:
            cur = conn.execute(
                "UPDATE runs SET status = 'running', updated_at = ? WHERE run_id = ?"
                " AND status = 'awaiting_tools'",
                (self._clock(), run_id),
            )
            if cur.rowcount != 1:
                return StoreOutcome(kind="no")
            events = [
                self._append(
                    conn,
                    run_id,
                    EVENT_RUN_STATUS,
                    {"status": "running", "reason": "tool_results_complete"},
                )
            ]
            return StoreOutcome(kind="continuing", events=events)

        return await self._run(op)

    # ---- approvals -------------------------------------------------------

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        def op(conn: sqlite3.Connection) -> ApprovalRecord | None:
            cur = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            )
            row = cur.fetchone()
            return self._approval_record(row) if row is not None else None

        return await self._run(op)

    async def resolve_approval(
        self, approval_id: str, *, approve: bool, note: str | None
    ) -> StoreOutcome:
        """Kinds: ``approved`` | ``declined`` | ``already`` | ``not_found``.

        Approving releases the lease to ``pending``; declining terminates it
        with a synthesized ``declined`` tool result. Run status is recomputed
        (``awaiting_approval`` while any approval is pending, else
        ``awaiting_tools`` — the caller then checks for continuation).
        """

        def op(conn: sqlite3.Connection) -> StoreOutcome:
            cur = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            )
            row = cur.fetchone()
            if row is None:
                return StoreOutcome(kind="not_found")
            approval = self._approval_record(row)
            if approval.status != "pending":
                return StoreOutcome(kind="already", approval=approval)
            now = self._clock()
            new_status = "approved" if approve else "declined"
            conn.execute(
                "UPDATE approvals SET status = ?, note = ?, resolved_at = ?"
                " WHERE approval_id = ?",
                (new_status, note, now, approval_id),
            )
            events = [
                self._append(
                    conn,
                    approval.run_id,
                    EVENT_APPROVAL_RESOLVED,
                    {
                        "approval_id": approval_id,
                        "lease_id": approval.lease_id,
                        "approved": approve,
                        "note": note,
                    },
                )
            ]
            cur = conn.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (approval.lease_id,)
            )
            lease_row = cur.fetchone()
            if lease_row is not None:
                lease = self._lease_record(lease_row)
                if lease.status == "awaiting_approval":
                    if approve:
                        conn.execute(
                            "UPDATE leases SET status = 'pending', updated_at = ?"
                            " WHERE lease_id = ?",
                            (now, lease.lease_id),
                        )
                    else:
                        content = "Tool call declined: approval was refused."
                        conn.execute(
                            "UPDATE leases SET status = 'declined', result_status = 'declined',"
                            " result_content = ?, result_hash = ?, updated_at = ?"
                            " WHERE lease_id = ?",
                            (content, sha256_hex(f"declined:{content}"), now, lease.lease_id),
                        )
                        events.append(
                            self._append(
                                conn,
                                approval.run_id,
                                EVENT_TOOL_RESULT,
                                {
                                    "lease_id": lease.lease_id,
                                    "round": lease.round,
                                    "tool_call_id": lease.tool_call_id,
                                    "status": "declined",
                                    "content": content,
                                    "executor_id": None,
                                    "synthesized": True,
                                },
                            )
                        )
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE run_id = ? AND status = 'pending'",
                (approval.run_id,),
            )
            pending = int(cur.fetchone()["n"])
            run_row = self._get_run_row(conn, approval.run_id)
            if run_row is not None and run_row["status"] == "awaiting_approval" and pending == 0:
                self._set_status(
                    conn, approval.run_id, "awaiting_tools", "approvals_resolved", events
                )
            cur = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            )
            resolved = cur.fetchone()
            assert resolved is not None
            return StoreOutcome(
                kind="approved" if approve else "declined",
                events=events,
                approval=self._approval_record(resolved),
            )

        return await self._run(op)

    # ---- expiry sweeps (scheduler) ----------------------------------------

    async def expire_due_leases(self, *, now: float) -> list[StoreOutcome]:
        """Terminate leases past their deadline with a synthesized ``expired``
        result so the waiting round can complete honestly."""

        def op(conn: sqlite3.Connection) -> list[StoreOutcome]:
            cur = conn.execute(
                "SELECT * FROM leases WHERE deadline < ? AND status IN"
                " ('pending','claimed','awaiting_approval')",
                (now,),
            )
            outcomes: list[StoreOutcome] = []
            for row in cur.fetchall():
                lease = self._lease_record(row)
                content = "Tool lease expired before a result was recorded."
                conn.execute(
                    "UPDATE leases SET status = 'expired', result_status = 'expired',"
                    " result_content = ?, result_hash = ?, updated_at = ? WHERE lease_id = ?",
                    (content, sha256_hex(f"expired:{content}"), now, lease.lease_id),
                )
                events = [
                    self._append(
                        conn,
                        lease.run_id,
                        EVENT_TOOL_RESULT,
                        {
                            "lease_id": lease.lease_id,
                            "round": lease.round,
                            "tool_call_id": lease.tool_call_id,
                            "status": "expired",
                            "content": content,
                            "executor_id": None,
                            "synthesized": True,
                        },
                    )
                ]
                if lease.approval_id:
                    cur2 = conn.execute(
                        "UPDATE approvals SET status = 'declined', resolved_at = ?, note = ?"
                        " WHERE approval_id = ? AND status = 'pending'",
                        (now, "lease expired", lease.approval_id),
                    )
                    if cur2.rowcount == 1:
                        # Keep the event log complete: the approval's resolution
                        # must be readable from the log, not inferred.
                        events.append(
                            self._append(
                                conn,
                                lease.run_id,
                                EVENT_APPROVAL_RESOLVED,
                                {
                                    "approval_id": lease.approval_id,
                                    "lease_id": lease.lease_id,
                                    "approved": False,
                                    "note": "lease expired",
                                },
                            )
                        )
                outcomes.append(StoreOutcome(kind="expired", events=events, lease=lease))
            if outcomes:
                # Any run parked on approvals whose last pending approval just
                # expired must fall back to awaiting_tools so continuation can run.
                run_ids = {o.lease.run_id for o in outcomes if o.lease is not None}
                for run_id in run_ids:
                    cur = conn.execute(
                        "SELECT COUNT(*) AS n FROM approvals WHERE run_id = ?"
                        " AND status = 'pending'",
                        (run_id,),
                    )
                    pending = int(cur.fetchone()["n"])
                    run_row = self._get_run_row(conn, run_id)
                    if (
                        run_row is not None
                        and run_row["status"] == "awaiting_approval"
                        and pending == 0
                    ):
                        extra: list[EventRecord] = []
                        self._set_status(
                            conn, run_id, "awaiting_tools", "approvals_resolved", extra
                        )
                        outcomes.append(StoreOutcome(kind="status", events=extra))
            return outcomes

        return await self._run(op)

    async def release_expired_claims(self, *, now: float) -> list[StoreOutcome]:
        """Return lapsed claims to ``pending`` so another executor can take over."""

        def op(conn: sqlite3.Connection) -> list[StoreOutcome]:
            cur = conn.execute(
                "SELECT * FROM leases WHERE status = 'claimed' AND claim_expires_at < ?",
                (now,),
            )
            outcomes: list[StoreOutcome] = []
            for row in cur.fetchall():
                lease = self._lease_record(row)
                conn.execute(
                    "UPDATE leases SET status = 'pending', claim_expires_at = NULL,"
                    " updated_at = ? WHERE lease_id = ?",
                    (now, lease.lease_id),
                )
                events = [
                    self._append(
                        conn,
                        lease.run_id,
                        EVENT_LEASE_RELEASED,
                        {
                            "lease_id": lease.lease_id,
                            "executor_id": lease.executor_id,
                            "reason": "claim_expired",
                        },
                    )
                ]
                outcomes.append(StoreOutcome(kind="released", events=events, lease=lease))
            return outcomes

        return await self._run(op)

    # ---- executors ---------------------------------------------------------

    async def heartbeat(
        self,
        *,
        executor_id: str,
        project_id: str | None,
        client: dict[str, Any],
        tools: list[dict[str, Any]],
        capabilities: dict[str, Any],
        ttl_seconds: float,
    ) -> ExecutorRecord:
        def op(conn: sqlite3.Connection) -> ExecutorRecord:
            now = self._clock()
            conn.execute(
                "INSERT INTO executors (executor_id, project_id, client, tools, capabilities,"
                " ttl_seconds, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(executor_id) DO UPDATE SET project_id = excluded.project_id,"
                " client = excluded.client, tools = excluded.tools,"
                " capabilities = excluded.capabilities, ttl_seconds = excluded.ttl_seconds,"
                " last_seen = excluded.last_seen",
                (
                    executor_id,
                    project_id,
                    json.dumps(client, separators=(",", ":"), ensure_ascii=False),
                    json.dumps(tools, separators=(",", ":"), ensure_ascii=False),
                    json.dumps(capabilities, separators=(",", ":"), ensure_ascii=False),
                    ttl_seconds,
                    now,
                ),
            )
            cur = conn.execute(
                "SELECT * FROM executors WHERE executor_id = ?", (executor_id,)
            )
            row = cur.fetchone()
            assert row is not None
            return self._executor_record(row)

        return await self._run(op)

    async def alive_executors(self, *, now: float) -> list[ExecutorRecord]:
        def op(conn: sqlite3.Connection) -> list[ExecutorRecord]:
            cur = conn.execute("SELECT * FROM executors")
            out = []
            for row in cur.fetchall():
                record = self._executor_record(row)
                if record.last_seen + record.ttl_seconds >= now:
                    out.append(record)
            return out

        return await self._run(op)

    async def tool_risks(self, *, now: float) -> dict[str, str]:
        """Tool-name -> risk class, from the inventories of live executors."""
        risks: dict[str, str] = {}
        for executor in await self.alive_executors(now=now):
            for tool in executor.tools:
                name = tool.get("name")
                risk = tool.get("risk")
                if isinstance(name, str) and isinstance(risk, str) and risk:
                    risks[name] = risk
        return risks

    # ---- wakeups -----------------------------------------------------------

    async def schedule_wakeup(
        self, *, wakeup_id: str, run_id: str, fire_at: float, note: str | None
    ) -> StoreOutcome:
        def op(conn: sqlite3.Connection) -> StoreOutcome:
            row = self._get_run_row(conn, run_id)
            if row is None:
                return StoreOutcome(kind="not_found")
            now = self._clock()
            conn.execute(
                "INSERT INTO wakeups (wakeup_id, run_id, fire_at, note, status, created_at)"
                " VALUES (?, ?, ?, ?, 'scheduled', ?)",
                (wakeup_id, run_id, fire_at, note, now),
            )
            events = [
                self._append(
                    conn,
                    run_id,
                    EVENT_WAKE_SCHEDULED,
                    {"wakeup_id": wakeup_id, "fire_at": fire_at, "note": note},
                )
            ]
            return StoreOutcome(kind="scheduled", events=events)

        return await self._run(op)

    async def cancel_wakeup(self, wakeup_id: str) -> StoreOutcome:
        def op(conn: sqlite3.Connection) -> StoreOutcome:
            cur = conn.execute(
                "SELECT * FROM wakeups WHERE wakeup_id = ?", (wakeup_id,)
            )
            row = cur.fetchone()
            if row is None:
                return StoreOutcome(kind="not_found")
            wakeup = self._wakeup_record(row)
            if wakeup.status != "scheduled":
                return StoreOutcome(kind="already")
            conn.execute(
                "UPDATE wakeups SET status = 'cancelled' WHERE wakeup_id = ?", (wakeup_id,)
            )
            events = [
                self._append(
                    conn,
                    wakeup.run_id,
                    EVENT_WAKE_CANCELLED,
                    {"wakeup_id": wakeup_id},
                )
            ]
            return StoreOutcome(kind="cancelled", events=events)

        return await self._run(op)

    async def due_wakeups(self, *, now: float) -> list[WakeupRecord]:
        def op(conn: sqlite3.Connection) -> list[WakeupRecord]:
            cur = conn.execute(
                "SELECT * FROM wakeups WHERE status = 'scheduled' AND fire_at <= ?"
                " ORDER BY fire_at",
                (now,),
            )
            return [self._wakeup_record(r) for r in cur.fetchall()]

        return await self._run(op)

    async def claim_wakeup(self, wakeup_id: str) -> StoreOutcome:
        """CAS ``scheduled -> fired`` so an overlapping tick cannot double-fire.
        Kinds: ``fired`` | ``no``."""

        def op(conn: sqlite3.Connection) -> StoreOutcome:
            now = self._clock()
            cur = conn.execute(
                "UPDATE wakeups SET status = 'fired', fired_at = ? WHERE wakeup_id = ?"
                " AND status = 'scheduled'",
                (now, wakeup_id),
            )
            if cur.rowcount != 1:
                return StoreOutcome(kind="no")
            cur = conn.execute(
                "SELECT * FROM wakeups WHERE wakeup_id = ?", (wakeup_id,)
            )
            row = cur.fetchone()
            assert row is not None
            wakeup = self._wakeup_record(row)
            events = [
                self._append(
                    conn,
                    wakeup.run_id,
                    EVENT_WAKE_FIRED,
                    {"wakeup_id": wakeup_id, "note": wakeup.note},
                )
            ]
            return StoreOutcome(kind="fired", events=events)

        return await self._run(op)

    async def defer_wakeup(self, wakeup_id: str, *, fire_at: float) -> StoreOutcome:
        """Push a FIRED wakeup back to ``scheduled`` (the run was busy when it
        fired, or a crash-recovery resubmission hit a busy run). Reusing the
        same row — rather than minting a replacement — keeps retries from
        proliferating wakeups. Kinds: ``deferred`` | ``no``."""

        def op(conn: sqlite3.Connection) -> StoreOutcome:
            cur = conn.execute(
                "UPDATE wakeups SET status = 'scheduled', fire_at = ?, fired_at = NULL"
                " WHERE wakeup_id = ? AND status = 'fired'",
                (fire_at, wakeup_id),
            )
            if cur.rowcount != 1:
                return StoreOutcome(kind="no")
            cur = conn.execute(
                "SELECT * FROM wakeups WHERE wakeup_id = ?", (wakeup_id,)
            )
            row = cur.fetchone()
            assert row is not None
            wakeup = self._wakeup_record(row)
            events = [
                self._append(
                    conn,
                    wakeup.run_id,
                    EVENT_WAKE_SCHEDULED,
                    {
                        "wakeup_id": wakeup_id,
                        "fire_at": fire_at,
                        "note": wakeup.note,
                        "deferred": True,
                    },
                )
            ]
            return StoreOutcome(kind="deferred", events=events)

        return await self._run(op)

    async def fired_wakeups_without_turn(self) -> list[WakeupRecord]:
        """Wakeups that were claimed ``fired`` but whose wake turn never landed
        — the crash window between the claim commit and the turn submission.
        The maintenance tick retries these (submission is idempotent on the
        ``wake-<id>`` turn_id, so a lost race is a harmless duplicate)."""

        def op(conn: sqlite3.Connection) -> list[WakeupRecord]:
            cur = conn.execute(
                "SELECT w.* FROM wakeups w LEFT JOIN turns t"
                " ON t.run_id = w.run_id AND t.turn_id = 'wake-' || w.wakeup_id"
                " WHERE w.status = 'fired' AND t.turn_id IS NULL"
            )
            return [self._wakeup_record(r) for r in cur.fetchall()]

        return await self._run(op)

    async def list_wakeups(self, run_id: str) -> list[WakeupRecord]:
        def op(conn: sqlite3.Connection) -> list[WakeupRecord]:
            cur = conn.execute(
                "SELECT * FROM wakeups WHERE run_id = ? ORDER BY fire_at", (run_id,)
            )
            return [self._wakeup_record(r) for r in cur.fetchall()]

        return await self._run(op)

    # ---- event queries -------------------------------------------------------

    async def events_after(
        self, run_id: str, after_seq: int, *, limit: int = 1000
    ) -> list[EventRecord]:
        def op(conn: sqlite3.Connection) -> list[EventRecord]:
            cur = conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (run_id, after_seq, int(limit)),
            )
            return [self._event_record(r) for r in cur.fetchall()]

        return await self._run(op)

    async def all_events(self, run_id: str) -> list[EventRecord]:
        def op(conn: sqlite3.Connection) -> list[EventRecord]:
            cur = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            )
            return [self._event_record(r) for r in cur.fetchall()]

        return await self._run(op)

    async def last_seq(self, run_id: str) -> int:
        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "SELECT MAX(seq) AS m FROM events WHERE run_id = ?", (run_id,)
            )
            row = cur.fetchone()
            return int(row["m"] or 0)

        return await self._run(op)

    # ---- transcript projection ------------------------------------------------

    async def transcript(self, run_id: str) -> list[dict[str, Any]]:
        """The conversation as wire-shaped message dicts, projected from the log.

        Tool messages are emitted in the assistant's tool_calls order (not
        result-arrival order) so replay is deterministic. Unresolved tool calls
        emit nothing — the caller only assembles when the round is complete.
        """

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            cur = conn.execute(
                "SELECT type, payload FROM events WHERE run_id = ? AND type IN (?, ?, ?)"
                " ORDER BY seq",
                (run_id, EVENT_TURN_SUBMITTED, EVENT_ASSISTANT_MESSAGE, EVENT_TOOL_RESULT),
            )
            rows = cur.fetchall()
            return build_transcript(
                [(r["type"], _loads(r["payload"], {})) for r in rows]
            )

        return await self._run(op)


def build_transcript(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Project (type, payload) pairs into wire-shaped conversation messages.

    Shared by the live transcript and the replay reconstructor. ``tool.result``
    payloads are keyed by (round, tool_call_id) and stitched under their
    assistant message in tool_calls order.
    """
    results: dict[tuple[int, str], dict[str, Any]] = {}
    for type_, payload in events:
        if type_ == EVENT_TOOL_RESULT:
            round_no = int(payload.get("round") or 0)
            call_id = str(payload.get("tool_call_id") or "")
            if call_id and (round_no, call_id) not in results:
                results[(round_no, call_id)] = payload

    messages: list[dict[str, Any]] = []
    for type_, payload in events:
        if type_ == EVENT_TURN_SUBMITTED:
            for msg in payload.get("messages") or []:
                if isinstance(msg, dict):
                    messages.append(msg)
        elif type_ == EVENT_ASSISTANT_MESSAGE:
            message = payload.get("message") or {}
            if not isinstance(message, dict):
                continue
            messages.append(message)
            round_no = int(payload.get("round") or 0)
            for call in message.get("tool_calls") or []:
                call_id = str((call or {}).get("id") or "")
                result = results.get((round_no, call_id))
                if result is not None:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": str(result.get("content") or ""),
                        }
                    )
    return messages
