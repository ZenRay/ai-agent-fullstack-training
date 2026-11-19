# Run Store 设计

Run Store 保存一段对话，以及这段对话里每一次执行。事实放在只追加的事件日志里。运行状态和界面上的 `View` 都是把这份日志按序号折出来的结果。

依据：

- [Feega：有序的线程事件日志](https://docs.feega.app/architecture/adr/0003-durable-thread-event-log/)
- [TanStack RunStore](https://tanstack.com/workflow/latest/docs/reference/interfaces/RunStore)
- [Stately：事件日志](https://stately.ai/docs/packages/agent/event-log)
- [Veetbot：日志、投影与检查点](https://docs.veetbot.com/adr/0003-event-log-and-projections/)
- [LangGraph：线程与检查点](https://docs.langchain.com/oss/python/langgraph/persistence)
- [DeerFlow：Redis Stream 桥](https://github.com/bytedance/deer-flow/blob/main/backend/docs/STREAMING.md)
- [Runtype：长运行的持久记录](https://www.runtype.com/guides/long-running-agents)
- [Toren：回放时的执行保证](https://toren.run/docs/concepts/durability)

## 概念

Store 里同时放三样东西，键不一样：

| 记录 | 键 | 里面有什么 |
| --- | --- | --- |
| 线程 | `thread_id` | 跨多次发问保留的消息列表 |
| 运行 | `run_id` | 状态、版本、取消标记、创建它的线程 |
| 事件日志 | `(run_id, seq)` | 这次执行追加的 `RunEvent`，序号从 1 递增 |

`thread_id` 是一段对话。同一次对话里的下一问复用它，并新建一个 `run_id`。`seq` 只在同一个 `run_id` 里递增，取消和断线补事件不会串到另一次发问。

日志是事实。运行状态和 `reduce` 得到的 `View` 都是投影，可以按序号重算。检查点是为了少重算而存的快照。丢了检查点只多花时间，不丢已经发生的事。

事件字段以 `docs/02-RunEvent设计.md` 为准。心跳是 SSE 注释，不进日志，也不占序号。

## 它解决什么

- HTTP 连接不是事实来源。客户端断开后，运行还在写；重连要能从上次看到的序号补上。
- 时间戳不能当顺序。顺序来自写入时分配的序号。
- 投递至少一次。同一条事件可能再送到，投影按 `seq` 丢掉重复的。
- 进程会在两次写入之间停掉。下一次模型调用或工具副作用发生之前，前面的事件已经落盘。
- 取消是一条命令。关掉连接只是没人在看。运行记录上有一个取消标记，循环在下一步边界读到它。
- 恢复会把同一步再执行。写操作如果每次用新的随机 id，下游会把它当成新动作。

## 设计

### 三条记录分开写

TanStack 把运行状态和事件日志分成两个面：状态是低频更新，日志只追加，并用期望中的下一个序号做乐观并发。Stately 的日志也是只追加，过期的序号直接冲突，两个写入者不能把同一次运行的事件交错写进去。

本服务里写事件的只有循环这一处。Store 在追加时分配 `seq`，调用方不自己填。同一次追加里写事件、推进状态、增加版本。终态事件（`run.completed`、`run.failed`、`run.cancelled`）写入后，这条 run 关闭，再追加就拒绝。流的第一条只能是 `run.started` 或 `run.failed`。

线程消息和运行日志不用同一个序号。用户消息按线程追加。一次 run 的事件按这次 run 追加。新 run 开始时读出线程上的消息，模型才能看见上一问。

### 读的时候用严格大于

重连的查询是 `seq > cursor`，按 `seq` 升序。客户端已经看见的那一条不再发。Feega 的做法是：实时通知只带最新序号，真正的事件以查询为准。通知丢了、重复了、进程停了，都靠再读 `seq > cursor` 补上。Veetbot 把数据库通知当成「可以早点去查」的信号，不把它当成送达保证。

游标落到保留窗口外面时，返回一个缺口，让客户端改取线程上的最终文本或整份快照。不要静默跳到末尾，也不要因为一个非法游标就把保留缓冲从头再放一遍。DeerFlow 用 `Last-Event-ID` 接着读 Redis Stream；非法 id 或超出保留尾部时要显式失败或报缺口。

### 状态与取消

运行记录上的状态保持闭合：`queued`、`running`、`waiting_for_approval`、`completed`、`failed`、`cancelled`。`waiting_for_approval` 先留在枚举里，前 10 步没有人工确认。

取消请求是记录上的一个标记。循环在下一步边界看见它，等当前工具返回或超时，写下工具结果，再追加 `run.cancelled`。重复取消不再追加事件，直接返回已经终态的记录。订阅断开不改这个标记。

### 副作用先占收据

模型调用在日志里记过，就不要再打一次。写操作在发出去之前，用确定性的键占一行收据：

```text
idempotency_key = hash(run_id + step_id + 规范化后的参数)
```

键不要在调用时用随机 uuid 生成。占住之后把下游的结果写回同一行。再次遇到这个键，读收据里的结果，不再发送。读操作可以重试。Toren 的区分是：有键的工具实际只生效一次；没有键的工具在崩溃窗口里可能执行两次。收据和持久化一起做，内存版先不建这张表。

### 检查点

LangGraph 在每个 superstep 结束后保存线程上的图状态，用来做人工确认和从上次成功的步骤恢复。Veetbot 的检查点是相对快照的增量，对话正文用事件引用。恢复时以日志为准。单机练习先不做检查点，留到第 10 步。表、回放和进程重启后的接法见 `docs/11-数据库与checkpoint设计.md`。

## 内存与持久化

两份实现共用同一组方法。循环和 `api/` 只依赖这些方法。第 1 到第 8 步使用 `app/store/memory.py` 里的 `MemoryStore`。第 10 步再增加持久化实现，不改调用方。

| | 内存 | 持久化 |
| --- | --- | --- |
| 数据放在哪 | 进程里的字典和列表 | SQLite 或 Postgres 的表。多进程的实时尾部以后再用 Redis Stream |
| 进程重启 | 线程、运行和事件都没了 | 记录还在，循环可以从最后一条事件接着看 |
| 序号怎么原子分配 | 一把 `asyncio.Condition` 包住「序号加一、追加、改状态、唤醒」 | 同一个数据库事务里做这几步，并给 `(run_id, seq)` 加唯一约束 |
| 谁能写同一次运行 | 只有这个进程里的循环 | 多个进程都可能写。事务和唯一约束挡住交错的序号 |
| 订阅者怎么知道有新事件 | `notify_all()`，同一个进程里的等待者被唤醒后再 `since` | 数据库不负责叫醒别的进程。用轮询，或数据库通知、Redis 通知作为「去查一下」的信号，结果仍以 `since` 为准 |
| 取消标记 | `RunState` 上的 `asyncio.Event`，循环在本进程里直接看见 | 表上的 `cancel_requested` 列。循环在下一步边界读这一列。本进程可以再配一个 `asyncio.Event` 少查几次，重启后以这一列为准 |
| 补事件 | `seq > cursor` 扫描内存列表，日志留到进程结束 | 同一条查询，靠 `(run_id, seq)` 索引。保留窗口裁掉旧事件时，游标落在窗口外要返回缺口 |
| 工具收据 | 第 10 步之前不建 | 用幂等键的唯一约束占一行 |

两边保持相同的规则：`seq` 从 1 起、由 Store 分配；终态之后拒绝再追加；`since` 用严格大于；心跳不入库；线程消息和某一次 run 的事件日志分开。

持久化时多出来的是崩溃恢复。发起下一次模型调用或工具副作用之前，这次 `append` 的事务先提交。写失败就停住这次运行，不再打模型。

保留策略分成两份。事件日志留够重连的窗口。完整回答另外存在线程的最终消息里。Redis Stream 如果加了长度上限，它只是实时尾部，权威日志仍在数据库里。

## 实现方式

对循环和接口暴露的方法：

| 方法 | 作用 |
| --- | --- |
| `ensure_thread` / `add_message` / `messages` | 按 `thread_id` 维护消息 |
| `create_run` / `get_run` | 创建和读取运行记录 |
| `append` | 分配 `seq`，写入事件，必要时改状态 |
| `since` | 返回 `seq` 大于游标的事件 |
| `request_cancel` | 只设置这个 `run_id` 的取消标记 |

当前内存实现里，`append` 接收事件字段组成的字典，补上 `seq`、`thread_id`、`run_id` 后再校验成 `RunEvent`。`request_cancel` 只设置 `asyncio.Event`，不写终态事件。终态由循环追加。

测试继续用内存实现，不依赖数据库。`main.py` 在组装时注入 Store。持久化实现出现之后，循环仍然调用同一组方法。

## 代码

内存版一次追加的形状：

```python
async def append(self, run_id: str, body: dict) -> RunEvent:
    async with self._changed:
        state = self.get_run(run_id)
        if state.events and state.events[-1].type in TERMINAL_TYPES:
            raise RuntimeError(f"运行已结束: {run_id}")
        state.seq += 1
        state.version += 1
        event = EVENT_ADAPTER.validate_python(
            {**body, "seq": state.seq, "thread_id": state.thread_id, "run_id": state.run_id}
        )
        state.status = STATUS_OF.get(event.type, state.status)
        state.events.append(event)
        self._changed.notify_all()          # 通知不是送达，订阅者再 since
        return event

def since(self, run_id: str, seq: int) -> list[RunEvent]:
    state = self.get_run(run_id)
    return [event for event in state.events if event.seq > seq]
```

持久化版把同一段逻辑放进一个事务。`seq` 的增加、事件插入和状态更新一起提交，`(run_id, seq)` 唯一。别的进程不会因为通知而直接拿到事件，醒来之后仍调用 `since`。取消列和事件插入也分开：`request_cancel` 只更新标记，`run.cancelled` 仍由循环在下一步边界写入。
