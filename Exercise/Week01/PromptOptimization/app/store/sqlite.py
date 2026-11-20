import asyncio
import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any

from app.events.types import EVENT_ADAPTER, OPENING_TYPES, RunEvent
from app.store.memory import (
    RUN_STATUSES,
    STATUS_OF,
    TERMINAL_STATUSES,
    OutputAttempt,
    PromptCall,
    RunState,
)
from app.tools.effects import Receipt

_OPEN_STATUSES = ("queued", "running", "waiting_for_approval")
_BOUNDARY_TYPES = frozenset(
    {
        "run.started",
        "message.completed",
        "tool.result",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }
)
_TABLES = frozenset(
    {
        "threads",
        "messages",
        "runs",
        "run_events",
        "effect_receipts",
        "checkpoints",
        "usage",
        "idempotency_keys",
        "prompt_calls",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  thread_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS messages (
  thread_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  extra TEXT NOT NULL,
  PRIMARY KEY (thread_id, position)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  status TEXT NOT NULL,
  seq INTEGER NOT NULL,
  version INTEGER NOT NULL,
  cancel_requested INTEGER NOT NULL,
  model_key TEXT,
  continue_used INTEGER NOT NULL,
  min_seq INTEGER NOT NULL,
  prompt_name TEXT,
  prompt_version INTEGER,
  prompt_hash TEXT,
  prompt_variables TEXT,
  repaired INTEGER NOT NULL DEFAULT 0,
  degraded INTEGER NOT NULL DEFAULT 0,
  degrade_reason TEXT,
  output_attempts TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  body TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS effect_receipts (
  idempotency_key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  call_id TEXT NOT NULL,
  name TEXT NOT NULL,
  arguments TEXT NOT NULL,
  status TEXT NOT NULL,
  ok INTEGER,
  output TEXT
);

CREATE TABLE IF NOT EXISTS checkpoints (
  run_id TEXT PRIMARY KEY,
  through_seq INTEGER NOT NULL,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
  run_id TEXT PRIMARY KEY,
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  ttft_ms REAL,
  cost REAL,
  estimated INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_calls (
  run_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  PRIMARY KEY (run_id, trace_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_run
  ON runs(thread_id)
  WHERE status IN ('queued', 'running', 'waiting_for_approval');
"""


class SqliteStore:
    """持久化 Run Store。方法与内存实现相同，序号在事务里分配。"""

    def __init__(self, path: str | Path) -> None:
        self._lock = RLock()
        self._cache: dict[str, RunState] = {}
        self._changed = asyncio.Condition()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        _ensure_prompt_columns(self._db)
        self._db.commit()
        self.effects = SqliteEffectLedger(self)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def ensure_thread(self, thread_id: str) -> str:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO threads(thread_id) VALUES (?)", (thread_id,))
            self._db.commit()
        return thread_id

    def add_message(self, thread_id: str, role: str, content: str, **extra: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO threads(thread_id) VALUES (?)", (thread_id,))
            if self._same_as_last(thread_id, role, content, extra):
                self._db.commit()
                return
            position = self._next_position(thread_id)
            self._db.execute(
                "INSERT INTO messages(thread_id, position, role, content, extra) VALUES (?, ?, ?, ?, ?)",
                (thread_id, position, role, content, _dump(extra)),
            )
            self._db.commit()

    def messages(self, thread_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT role, content, extra FROM messages WHERE thread_id = ? ORDER BY position",
                (thread_id,),
            ).fetchall()
        return [_message(row) for row in rows]

    def create_run(self, thread_id: str, run_id: str) -> RunState:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO threads(thread_id) VALUES (?)", (thread_id,))
            if self._db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone():
                raise ValueError(f"运行已存在: {run_id}")
            busy = self._db.execute(
                f"SELECT 1 FROM runs WHERE thread_id = ? AND status IN ({_placeholders(_OPEN_STATUSES)})",
                (thread_id, *_OPEN_STATUSES),
            ).fetchone()
            if busy is not None:
                raise ValueError(f"线程已有未结束的运行: {thread_id}")
            self._db.execute(
                """
                INSERT INTO runs(
                  run_id, thread_id, status, seq, version, cancel_requested, continue_used, min_seq
                ) VALUES (?, ?, 'queued', 0, 0, 0, 0, 0)
                """,
                (run_id, thread_id),
            )
            self._db.commit()
            return self._load_run(run_id)

    def get_run(self, run_id: str) -> RunState:
        with self._lock:
            return self._load_run(run_id)

    async def append(self, run_id: str, body: dict[str, Any]) -> RunEvent:
        with self._lock:
            event = self._append_locked(run_id, body)
        async with self._changed:
            self._changed.notify_all()
        return event

    def since(self, run_id: str, seq: int) -> list[RunEvent]:
        if seq < 0:
            raise ValueError(f"游标不能为负: {seq}")
        with self._lock:
            self._load_run(run_id)
            rows = self._db.execute(
                "SELECT body FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, seq),
            ).fetchall()
        return [EVENT_ADAPTER.validate_python(json.loads(row["body"])) for row in rows]

    def min_seq(self, run_id: str) -> int:
        with self._lock:
            row = self._run_row(run_id)
        return int(row["min_seq"])

    def active_run(self, thread_id: str) -> RunState | None:
        with self._lock:
            row = self._db.execute(
                f"""
                SELECT run_id FROM runs
                WHERE thread_id = ? AND status NOT IN ({_placeholders(tuple(TERMINAL_STATUSES))})
                """,
                (thread_id, *TERMINAL_STATUSES),
            ).fetchone()
            if row is None:
                return None
            return self._load_run(row["run_id"])

    def find_idempotency(self, key: str) -> tuple[str, str] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT fingerprint, run_id FROM idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return (row["fingerprint"], row["run_id"])

    def remember_idempotency(self, key: str, fingerprint: str, run_id: str) -> None:
        with self._lock:
            current = self._db.execute(
                "SELECT fingerprint, run_id FROM idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
            if current is not None and (current["fingerprint"], current["run_id"]) != (fingerprint, run_id):
                raise ValueError(f"幂等键已被不同的请求使用: {key}")
            self._db.execute(
                "INSERT OR REPLACE INTO idempotency_keys(key, fingerprint, run_id) VALUES (?, ?, ?)",
                (key, fingerprint, run_id),
            )
            self._db.commit()

    async def wait_for_change(self, run_id: str, seq: int) -> None:
        async with self._changed:
            while not self.since(run_id, seq):
                await self._changed.wait()

    def request_cancel(self, run_id: str) -> RunState:
        with self._lock:
            state = self._load_run(run_id)
            if state.status in TERMINAL_STATUSES:
                return state
            self._db.execute("UPDATE runs SET cancel_requested = 1 WHERE run_id = ?", (run_id,))
            self._db.commit()
            state.cancel_event.set()
            return state

    def note_usage(self, run_id: str, input_tokens: int, output_tokens: int) -> None:
        state = self.get_run(run_id)
        state.input_tokens += input_tokens
        state.output_tokens += output_tokens

    def note_model_key(self, run_id: str, key: str) -> None:
        with self._lock:
            self._load_run(run_id)
            self._db.execute("UPDATE runs SET model_key = ? WHERE run_id = ?", (key, run_id))
            self._db.commit()
            self._cache[run_id].model_key = key

    def bind_prompt(
        self,
        run_id: str,
        *,
        name: str,
        version: int,
        content_hash: str,
        variables: dict[str, str],
    ) -> None:
        encoded = json.dumps(variables, ensure_ascii=False, sort_keys=True)
        with self._lock:
            state = self._load_run(run_id)
            if state.prompt_name is not None and (
                state.prompt_name != name
                or state.prompt_version != version
                or state.prompt_hash != content_hash
            ):
                raise ValueError(f"运行已绑定其他提示词版本: {run_id}")
            self._db.execute(
                """
                UPDATE runs
                SET prompt_name = ?, prompt_version = ?, prompt_hash = ?, prompt_variables = ?
                WHERE run_id = ?
                """,
                (name, version, content_hash, encoded, run_id),
            )
            self._db.commit()
            state.prompt_name = name
            state.prompt_version = version
            state.prompt_hash = content_hash
            state.prompt_variables = dict(variables)

    def note_prompt_call(
        self,
        run_id: str,
        trace_id: str,
        name: str,
        version: int,
        content_hash: str,
    ) -> None:
        with self._lock:
            self._load_run(run_id)
            row = self._db.execute(
                "SELECT name, version, content_hash FROM prompt_calls WHERE run_id = ? AND trace_id = ?",
                (run_id, trace_id),
            ).fetchone()
            if row is not None:
                if (row["name"], int(row["version"]), row["content_hash"]) != (name, version, content_hash):
                    raise ValueError(f"同一次模型调用不能改用另一版提示词: {trace_id}")
                return
            self._db.execute(
                """
                INSERT INTO prompt_calls(run_id, trace_id, name, version, content_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, trace_id, name, version, content_hash),
            )
            self._db.commit()

    def prompt_calls(self, run_id: str) -> list[PromptCall]:
        with self._lock:
            self._load_run(run_id)
            rows = self._db.execute(
                """
                SELECT trace_id, name, version, content_hash
                FROM prompt_calls WHERE run_id = ? ORDER BY rowid
                """,
                (run_id,),
            ).fetchall()
        return [
            PromptCall(
                trace_id=row["trace_id"],
                name=row["name"],
                version=int(row["version"]),
                content_hash=row["content_hash"],
            )
            for row in rows
        ]

    def note_output_attempt(
        self,
        run_id: str,
        attempt: int,
        failed_field: str | None,
        code: str,
    ) -> None:
        with self._lock:
            state = self._load_run(run_id)
            state.output_attempts.append(
                OutputAttempt(attempt=attempt, failed_field=failed_field, code=code)
            )
            self._db.execute(
                "UPDATE runs SET output_attempts = ? WHERE run_id = ?",
                (_dump_attempts(state.output_attempts), run_id),
            )
            self._db.commit()

    def note_output_outcome(
        self,
        run_id: str,
        *,
        repaired: bool,
        degraded: bool,
        degrade_reason: str | None,
    ) -> None:
        with self._lock:
            state = self._load_run(run_id)
            self._db.execute(
                """
                UPDATE runs
                SET repaired = ?, degraded = ?, degrade_reason = ?
                WHERE run_id = ?
                """,
                (1 if repaired else 0, 1 if degraded else 0, degrade_reason, run_id),
            )
            self._db.commit()
            state.repaired = repaired
            state.degraded = degraded
            state.degrade_reason = degrade_reason

    def output_attempts(self, run_id: str) -> list[OutputAttempt]:
        with self._lock:
            state = self._load_run(run_id)
        return list(state.output_attempts)

    def mark_continued(self, run_id: str) -> None:
        with self._lock:
            self._load_run(run_id)
            self._db.execute("UPDATE runs SET continue_used = 1 WHERE run_id = ?", (run_id,))
            self._db.commit()
            self._cache[run_id].continue_used = True

    def continued(self, run_id: str) -> bool:
        return self.get_run(run_id).continue_used

    def unfinished(self) -> list[RunState]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT run_id FROM runs WHERE status IN ({_placeholders(_OPEN_STATUSES)})",
                _OPEN_STATUSES,
            ).fetchall()
            return [self._load_run(row["run_id"]) for row in rows]

    def repair_status(self, run_id: str, status: str) -> None:
        if status not in RUN_STATUSES:
            raise RuntimeError(f"未知状态: {status}")
        with self._lock:
            self._load_run(run_id)
            self._db.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))
            self._db.commit()
            self._cache[run_id].status = status

    def checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._load_run(run_id)
            row = self._db.execute(
                "SELECT through_seq, status FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {"through_seq": int(row["through_seq"]), "status": row["status"]}

    def usage(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._load_run(run_id)
            row = self._db.execute("SELECT * FROM usage WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def columns(self, table: str) -> list[str]:
        if table not in _TABLES:
            raise ValueError(f"未知的表: {table}")
        with self._lock:
            rows = self._db.execute(f"PRAGMA table_info({table})").fetchall()
        return [row["name"] for row in rows]

    def trim_folded(self, run_id: str) -> int:
        with self._lock:
            self._load_run(run_id)
            point = self._db.execute(
                "SELECT through_seq FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if point is None:
                return int(self._run_row(run_id)["min_seq"])
            through_seq = int(point["through_seq"])
            self._db.execute(
                "DELETE FROM run_events WHERE run_id = ? AND seq < ?",
                (run_id, through_seq),
            )
            self._db.execute(
                "UPDATE runs SET min_seq = ? WHERE run_id = ? AND min_seq < ?",
                (through_seq, run_id, through_seq),
            )
            self._db.commit()
            return int(self._run_row(run_id)["min_seq"])

    def _append_locked(self, run_id: str, body: dict[str, Any]) -> RunEvent:
        row = self._run_row(run_id)
        if row["status"] in TERMINAL_STATUSES:
            raise RuntimeError(f"运行已结束: {run_id}")
        event_type = body.get("type")
        if row["seq"] == 0 and event_type not in OPENING_TYPES:
            raise ValueError(f"流必须以 run.started 或 run.failed 开始: {run_id}")
        seq = int(row["seq"]) + 1
        version = int(row["version"]) + 1
        event = EVENT_ADAPTER.validate_python(
            {**body, "seq": seq, "thread_id": row["thread_id"], "run_id": run_id}
        )
        status = STATUS_OF.get(event.type, row["status"])
        if status not in RUN_STATUSES:
            raise RuntimeError(f"未知状态: {status}")
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO run_events(run_id, seq, body) VALUES (?, ?, ?)",
                    (run_id, seq, json.dumps(event.model_dump(exclude_none=True), ensure_ascii=False)),
                )
                self._db.execute(
                    "UPDATE runs SET seq = ?, version = ?, status = ? WHERE run_id = ?",
                    (seq, version, status, run_id),
                )
                self._project(run_id, row["thread_id"], event)
                if event.type in _BOUNDARY_TYPES:
                    self._db.execute(
                        """
                        INSERT INTO checkpoints(run_id, through_seq, status) VALUES (?, ?, ?)
                        ON CONFLICT(run_id) DO UPDATE SET
                          through_seq = excluded.through_seq,
                          status = excluded.status
                        """,
                        (run_id, seq, status),
                    )
        except Exception:
            self._db.rollback()
            raise
        self._load_run(run_id)
        return event

    def _project(self, run_id: str, thread_id: str, event: RunEvent) -> None:
        if event.type == "message.completed":
            text = self._folded_text(run_id, event.message_id)
            if text and not self._same_as_last(thread_id, "assistant", text, {}):
                self._db.execute(
                    "INSERT INTO messages(thread_id, position, role, content, extra) VALUES (?, ?, ?, ?, ?)",
                    (thread_id, self._next_position(thread_id), "assistant", text, _dump({})),
                )
        elif event.type == "tool.result":
            extra = {"tool_call_id": event.call_id}
            if not self._same_as_last(thread_id, "tool", event.output, extra):
                self._db.execute(
                    "INSERT INTO messages(thread_id, position, role, content, extra) VALUES (?, ?, ?, ?, ?)",
                    (thread_id, self._next_position(thread_id), "tool", event.output, _dump(extra)),
                )
        elif event.type == "run.completed":
            self._db.execute(
                """
                INSERT INTO usage(run_id, model, input_tokens, output_tokens, ttft_ms, cost, estimated)
                VALUES (?, NULL, ?, ?, NULL, NULL, 1)
                ON CONFLICT(run_id) DO UPDATE SET
                  input_tokens = excluded.input_tokens,
                  output_tokens = excluded.output_tokens
                """,
                (run_id, event.usage.input_tokens, event.usage.output_tokens),
            )

    def _folded_text(self, run_id: str, message_id: str) -> str:
        rows = self._db.execute(
            "SELECT body FROM run_events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        parts: list[str] = []
        for row in rows:
            body = json.loads(row["body"])
            if body.get("type") == "message.delta" and body.get("message_id") == message_id:
                parts.append(body.get("delta") or "")
        return "".join(parts)

    def _same_as_last(self, thread_id: str, role: str, content: str, extra: dict[str, Any]) -> bool:
        row = self._db.execute(
            "SELECT role, content, extra FROM messages WHERE thread_id = ? ORDER BY position DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        if row is None:
            return False
        return _message(row) == {"role": role, "content": content, **extra}

    def _next_position(self, thread_id: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(position), 0) AS position FROM messages WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return int(row["position"]) + 1

    def _run_row(self, run_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"未知运行: {run_id}")
        return row

    def _load_run(self, run_id: str) -> RunState:
        row = self._run_row(run_id)
        state = self._cache.get(run_id)
        if state is None:
            state = RunState(thread_id=row["thread_id"], run_id=run_id)
            self._cache[run_id] = state
        state.status = row["status"]
        state.seq = int(row["seq"])
        state.version = int(row["version"])
        state.model_key = row["model_key"]
        state.continue_used = bool(row["continue_used"])
        state.prompt_name = row["prompt_name"]
        state.prompt_version = None if row["prompt_version"] is None else int(row["prompt_version"])
        state.prompt_hash = row["prompt_hash"]
        raw_variables = row["prompt_variables"]
        state.prompt_variables = json.loads(raw_variables) if raw_variables else {}
        state.repaired = bool(row["repaired"])
        state.degraded = bool(row["degraded"])
        state.degrade_reason = row["degrade_reason"]
        state.output_attempts = _load_attempts(row["output_attempts"])
        if row["cancel_requested"]:
            state.cancel_event.set()
        return state


class SqliteEffectLedger:
    """写操作收据。占坑先提交，再交给调用方发送。"""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def get(self, key: str) -> Receipt | None:
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT * FROM effect_receipts WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return None if row is None else _receipt(row)

    def claim(
        self,
        key: str,
        *,
        run_id: str,
        call_id: str,
        name: str,
        arguments: str,
    ) -> tuple[Receipt, bool]:
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT * FROM effect_receipts WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                return _receipt(row), False
            self._store._db.execute(
                """
                INSERT INTO effect_receipts(
                  idempotency_key, run_id, call_id, name, arguments, status, ok, output
                ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL)
                """,
                (key, run_id, call_id, name, arguments),
            )
            self._store._db.commit()
            saved = self._store._db.execute(
                "SELECT * FROM effect_receipts WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return _receipt(saved), True

    def settle(self, key: str, *, ok: bool, output: str) -> None:
        self._update(key, status="settled", ok=1 if ok else 0, output=output)

    def mark_unknown(self, key: str, output: str) -> None:
        self._update(key, status="unknown", ok=0, output=output)

    def find_result(self, run_id: str, name: str, arguments: str) -> Receipt | None:
        with self._store._lock:
            rows = self._store._db.execute(
                """
                SELECT * FROM effect_receipts
                WHERE run_id = ? AND name = ? AND arguments = ? AND output IS NOT NULL
                ORDER BY rowid
                """,
                (run_id, name, arguments),
            ).fetchall()
        if not rows:
            return None
        return _receipt(rows[-1])

    def has_unknown(self, run_id: str) -> bool:
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT 1 FROM effect_receipts WHERE run_id = ? AND status = 'unknown'",
                (run_id,),
            ).fetchone()
        return row is not None

    def has_other_unknown(self, run_id: str, name: str, arguments: str) -> bool:
        with self._store._lock:
            row = self._store._db.execute(
                """
                SELECT 1 FROM effect_receipts
                WHERE run_id = ? AND name = ? AND status = 'unknown' AND arguments != ?
                """,
                (run_id, name, arguments),
            ).fetchone()
        return row is not None

    def _update(self, key: str, *, status: str, ok: int, output: str) -> None:
        with self._store._lock:
            self._store._db.execute(
                "UPDATE effect_receipts SET status = ?, ok = ?, output = ? WHERE idempotency_key = ?",
                (status, ok, output, key),
            )
            self._store._db.commit()


def _message(row: sqlite3.Row) -> dict[str, Any]:
    extra = json.loads(row["extra"] or "{}")
    return {"role": row["role"], "content": row["content"], **extra}


def _receipt(row: sqlite3.Row) -> Receipt:
    ok = None if row["ok"] is None else bool(row["ok"])
    return Receipt(
        key=row["idempotency_key"],
        run_id=row["run_id"],
        call_id=row["call_id"],
        name=row["name"],
        arguments=row["arguments"],
        status=row["status"],
        ok=ok,
        output=row["output"],
    )


def _dump(extra: dict[str, Any]) -> str:
    return json.dumps(extra, ensure_ascii=False, sort_keys=True)


def _placeholders(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)


def _dump_attempts(attempts: list[OutputAttempt]) -> str:
    payload = [
        {"attempt": item.attempt, "failed_field": item.failed_field, "code": item.code}
        for item in attempts
    ]
    return json.dumps(payload, ensure_ascii=False)


def _load_attempts(raw: str | None) -> list[OutputAttempt]:
    if not raw:
        return []
    return [
        OutputAttempt(
            attempt=int(item["attempt"]),
            failed_field=item["failed_field"],
            code=item["code"],
        )
        for item in json.loads(raw)
    ]


def _ensure_prompt_columns(db: sqlite3.Connection) -> None:
    present = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
    for name, declaration in (
        ("prompt_name", "TEXT"),
        ("prompt_version", "INTEGER"),
        ("prompt_hash", "TEXT"),
        ("prompt_variables", "TEXT"),
        ("repaired", "INTEGER NOT NULL DEFAULT 0"),
        ("degraded", "INTEGER NOT NULL DEFAULT 0"),
        ("degrade_reason", "TEXT"),
        ("output_attempts", "TEXT"),
    ):
        if name not in present:
            db.execute(f"ALTER TABLE runs ADD COLUMN {name} {declaration}")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_calls (
          run_id TEXT NOT NULL,
          trace_id TEXT NOT NULL,
          name TEXT NOT NULL,
          version INTEGER NOT NULL,
          content_hash TEXT NOT NULL,
          PRIMARY KEY (run_id, trace_id)
        )
        """
    )
