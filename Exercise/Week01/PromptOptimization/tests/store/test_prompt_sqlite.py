import json
import sqlite3

from app.loop.run import execute_run
from app.model.events import ModelFinished, TextDelta
from app.prompts.catalog import default_catalog
from app.prompts.render import ResolvedPrompt, render
from app.store.sqlite import SqliteStore
from app.tools.registry import ToolRegistry


class _Model:
    async def stream(
        self,
        messages,
        *,
        cancel,
        idempotency_key,
        tools=None,
        model=None,
        temperature=None,
        tool_choice=None,
    ):
        yield TextDelta(type="text.delta", delta="好")
        yield ModelFinished(type="model.finished", finish_reason="stop")


async def test_prompt_identity_survives_reopen_and_stays_out_of_usage(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    version = default_catalog().current()
    resolved = ResolvedPrompt(version=version, system=render(version, {}), variables={})
    store = SqliteStore(path)
    store.add_message("th", "user", "你好")
    store.create_run("th", "run_1")
    await execute_run(store, _Model(), ToolRegistry(), "run_1", prompt=resolved)
    store.close()

    again = SqliteStore(path)
    run = again.get_run("run_1")
    assert run.prompt_name == "assistant"
    assert run.prompt_version == 1
    assert run.prompt_hash == version.content_hash
    calls = again.prompt_calls("run_1")
    assert [call.trace_id for call in calls] == ["run_1:0"]
    assert calls[0].content_hash == version.content_hash
    usage = json.dumps(again.usage("run_1"), ensure_ascii=False)
    assert version.template not in usage
    bodies = " ".join(event.model_dump_json() for event in again.since("run_1", 0))
    assert version.template not in bodies
    again.close()


def test_old_database_gains_prompt_columns(tmp_path) -> None:
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY)")
    db.execute(
        """
        CREATE TABLE runs (
          run_id TEXT PRIMARY KEY,
          thread_id TEXT NOT NULL,
          status TEXT NOT NULL,
          seq INTEGER NOT NULL,
          version INTEGER NOT NULL,
          cancel_requested INTEGER NOT NULL,
          model_key TEXT,
          continue_used INTEGER NOT NULL,
          min_seq INTEGER NOT NULL
        )
        """
    )
    db.commit()
    db.close()
    store = SqliteStore(path)
    assert "prompt_hash" in store.columns("runs")
    assert "content_hash" in store.columns("prompt_calls")
    store.create_run("th", "run_1")
    store.bind_prompt("run_1", name="assistant", version=1, content_hash="a" * 64, variables={})
    store.close()
    again = SqliteStore(path)
    assert again.get_run("run_1").prompt_name == "assistant"
    again.close()
