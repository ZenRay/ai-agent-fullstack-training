# 数据库与 checkpoint 设计

第 10 步给 Run Store 加一份持久化实现，并补上 `GET /events?since=`。数据库里的事件日志是事实。检查点是某条序号上的快照，丢了就顺着日志重算。Redis 只在事务提交之后叫醒别的进程，不保存另一份历史。

依据：

- [Tanay Shah：数据库里的只追加对话日志](https://tanayshah.dev/blog/postgres-append-only-chat-events/)
- [LangGraph：检查点与耐久性](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- [LangGraph：检查点先于下一步落盘](https://github.com/langchain-ai/langgraph/issues/8039)
- [Halter：日志是事实，检查点带序号](https://docs.rs/halter/latest/halter/session/fold/index.html)
- [Redis：事务提交后再投递](https://dev.to/redis/building-reliable-agents-with-the-transactional-outbox-pattern-and-redis-streams-45e6)

方法集合仍是 `docs/06-RunStore设计.md` 里的那一组。循环和 `api/` 不改调用方式。`MemoryStore` 继续给测试用。

## 概念

持久化实现和内存实现共用 `ensure_thread`、`add_message`、`messages`、`create_run`、`get_run`、`append`、`since`、`request_cancel`。差别在存放、事务和进程重启之后怎么接。

单机用 SQLite，打开 WAL，一个写进程。多个工人同时写时换 Postgres。两种库用同一套表。SQLite 上不要开多个写进程。

| 记录 | 表 | 键 |
| --- | --- | --- |
| 线程 | `threads` | `thread_id` |
| 线程消息 | `messages` | `thread_id` + 线程内顺序 |
| 运行 | `runs` | `run_id` |
| 事件日志 | `run_events` | `(run_id, seq)` |
| 写工具收据 | `effect_receipts` | 幂等键 |
| 检查点 | `checkpoints` | 一次 run 上最新一条 |
| 用量 | `usage` | `run_id` |

一个线程同时只有一次未结束的 run：对 `queued`、`running`、`waiting_for_approval` 建部分唯一索引。

## 它解决什么

- 内存 Store 随进程消失。重启后要能看见已经写入的线程消息、运行状态和事件，并接着未结束的那次 run。
- 序号若用全局自增，所有对话的写入挤在同一条计数器上，客户端序号也不再按一次 run 从 1 连续。每条 run 自己的计数器，和 `(run_id, seq)` 唯一约束放在同一个事务里。
- 先写 Redis、后写数据库，会在提交前把还没发生的事件推出去，或在进程停掉时丢掉已提交的事件。通知放在提交之后，内容仍从数据库读。Redis 自己的长度上限会裁历史，不适合当权威日志。
- 检查点如果记在工具已经发出、收据还没落盘的窗口里，恢复会把写操作再做一次。节点输出没先落盘就写入检查点时，恢复会重跑该节点。
- 增量事件长期全留，库会随 token 涨。留够重连窗口即可。完整回答在线程消息里另有一份。
- 用量如果和正文放在同一张账本里，记账查询就会带上 Prompt。账本只记调用方、模型、Token、耗时和费用。

## 设计

### 每张表写什么

`messages` 保存用户消息、工具结果和助手全文，给下一问的模型读。`run_events` 保存 `RunEvent` 的 JSON，只插入。心跳不入库。

`runs` 上有 `thread_id`、状态、`seq`、`version`、`cancel_requested`、当前模型调用的幂等键，以及本条日志还保留的最小序号。

`effect_receipts` 的键是 `hash(run_id + call_id + 规范化参数)`。`call_id` 就是 `docs/06-RunStore设计.md` 里的 `step_id`。占坑时先插入一行并提交，再发送。同一键再次出现时读这行的结果。读工具不占这张表。

`usage` 在终态事件带着 usage 时写入：模型名、输入和输出 Token、TTFT、费用、是否为估算。缺用量就标成估算。这张表不参与重连，也不存正文。

`run_events` 不靠更新单行来改历史。裁旧数据是按保留窗口整段删掉已经折进线程消息的增量，并抬高 `runs` 上的最小序号。

### 一次追加

一个事务里做完这些事：拒绝已终态的 run；`seq` 加一；插入事件；按事件类型改状态和版本；若是 `message.completed` 或 `tool.result`，同时写入线程消息；提交。提交成功之后，本进程唤醒等待者，或往 Redis 发「`run_id` 有新序号」的提示。别的进程被叫醒后仍执行 `since`。

下一次模型调用或工具发送之前，这次事务已经提交。事务失败就停住这次 run，不再打模型。

`request_cancel` 只把 `cancel_requested` 设为真，单独提交，不写 `run.cancelled`。终态仍由循环在下一步边界追加。

### 检查点

检查点记「日志已经完整到哪一条」。它和这一条事件进同一个事务，写在边界上：`run.started` 之后、一轮工具结果都写完之后、`message.completed` 之后、终态之后。工具请求已经发出、结果还没写回时不写检查点。

行里有 `run_id`、`through_seq`、状态。线程消息已经在同一事务里，检查点不必再抄一份正文。恢复时从 `through_seq` 之后折事件。没有这行就从序号 0 折，结果相同，只是多读。

检查点不含供应商那边的缓存，也代替不了收据。恢复出来的是本服务已经提交的状态。

### 进程停了之后怎么接

启动时找出状态仍是 `running` 或 `queued` 的 run，按日志判断：

| 日志停在哪 | 怎么接 |
| --- | --- |
| 已有终态事件，状态列没改上 | 以事件为准改状态，不再执行 |
| 只有 `run.started`，还没有文字和工具参数 | 用运行记录上的原幂等键再打这一次模型调用 |
| 已有 `message.delta`，没有收尾 | 按 `docs/04-StreamingModel设计.md`：保留增量。没有写工具且失败可重试时，最多一次新调用、新幂等键；否则写 `run.failed` |
| `tool.arguments.done` 且收据里已有结果 | 把结果追加成 `tool.result`，不再发送 |
| 收据已占上、结果为空 | 先向下游对账，再决定补结果还是重试 |
| 写工具还没有收据 | 视为没发送过，占坑后再执行一次 |
| `cancel_requested` 已设置 | 把标记留着，循环在下一步边界写 `run.cancelled` |

已经交出去的文字不用原调用再生成一遍。写操作在没有收据时不再发一次。

### 回放接口

`GET /v1/runs/{run_id}/events?since=` 和 Store 的 `since` 是同一条查询：`seq` 严格大于游标，升序。先把已提交的事件编码成 SSE，再接上提交之后的新事件。实时和补洞没有第二套数据。

游标小于该 run 的最小保留序号时，返回缺口，并带上 `thread_id`，让客户端改读线程上的最终消息。不把尾部当成这段缺口的替代。

这条 GET 不创建 run，也不设置取消标记。帧格式、心跳和代理头沿用 `docs/03-SSE设计.md`。创建和取消的状态码见 `docs/08-创建运行与取消接口设计.md`。

## 实现方式

`app/store/` 增加 SQLite 实现，方法签名与 `MemoryStore` 相同。`main.py` 在组装时注入。测试仍用内存实现。Postgres 只在要多个写进程时换驱动，表结构不动。

Redis 在第 10 步可以先不接。单进程靠提交后的进程内通知。接上时，流用近似长度上限只留最近的提示，字段是 `run_id` 和 `seq`。订阅者用这个序号再查数据库。Redis 空了就轮询 `since`。

保留窗口按时间或条数裁 `run_events` 里已折进 `messages` 的增量，同时抬高最小序号。终态 run 的 `usage` 行留下。

回放路由放在 `api/`。循环不直接知道 HTTP。

第 10 步的完成标准是：进程重启后线程消息和事件还在；未结束的 run 按上表接上，写工具不重复发送；`GET /events?since=` 用和 Store 相同的严格大于读回；游标落到窗口外返回缺口；用量行里没有正文。

## 代码

表的核心约束：

```sql
CREATE TABLE run_events (
  run_id TEXT NOT NULL,
  seq    INTEGER NOT NULL,
  body   TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE effect_receipts (
  idempotency_key TEXT PRIMARY KEY,
  run_id   TEXT NOT NULL,
  call_id  TEXT NOT NULL,
  output   TEXT
);
```

追加和内存版是同一段判断，包在一个事务里。`seq` 来自 `runs` 上的计数器，不由调用方传入。

```python
def append(self, run_id: str, body: dict) -> RunEvent:
    with self._db:
        run = self._lock_run(run_id)
        self._reject_if_closed(run, body)
        run.seq += 1
        event = EVENT_ADAPTER.validate_python(
            {**body, "seq": run.seq, "thread_id": run.thread_id, "run_id": run_id}
        )
        self._insert_event(event)
        self._apply_status_and_thread_message(run, event)
        if event.type in BOUNDARY_TYPES:
            self._write_checkpoint(run_id, through_seq=run.seq, status=run.status)
    self._hint(run_id, event.seq)  # 提交之后才叫醒
    return event
```

恢复只补日志里还没有的那一步。收据里已经有结果时，不再调用工具：

```python
def resume(self, run_id: str) -> None:
    events = self.since(run_id, 0)
    pending = tool_calls_without_result(events)
    for call in pending:
        receipt = self.get_receipt(run_id, call.call_id, call.arguments)
        if receipt and receipt.output is not None:
            self.append(
                run_id,
                {"type": "tool.result", "call_id": call.call_id, "ok": True, "output": receipt.output},
            )
        elif receipt is None and call.kind == "write":
            self.claim_and_execute(call)
    if needs_another_model_call(events):
        execute_run(self, model, tools, run_id)
```

对应关系：

- `with self._db` 提交成功之后才调用 `_hint`。提示里只有 `run_id` 和 `seq`，订阅者再 `since`。
- `BOUNDARY_TYPES` 是 `run.started`、一轮工具都已有 `tool.result`、`message.completed` 和三种终态。半截工具调用不在其中。
- 回放先比较游标和最小保留序号。落在窗口外就返回缺口。窗口内的事件用和创建运行相同的 SSE 编码。
