# RunEvent 设计

`RunEvent` 是一次运行里追加进去的一条不可变事实。序号由存储在写入时分配，客户端按 `type` 把这些事实拼成界面。模型供应商的原始数据块留在网关内部。

依据：

- [AG-UI 1.0 事件模型](https://docs.ag-ui.com/spec/1.0/basic)
- [OpenAI Responses 语义事件](https://developers.openai.com/api/docs/guides/streaming-responses)
- [Everruns：事件即主存储](https://everruns.com/docs/explanation/events/)
- [UseAgent：先落库再推送](https://useagent.org/blog/event-sourced-agent-runs)
- [Trouve 事件日志](https://github.com/jimsimon/trouve/blob/main/docs/design/event-log.md)

## 概念

一条 `RunEvent` 只描述已经发生的一件事，例如「运行开始了」「这段文字又多了几个字」「这个工具的参数收齐了」。它一旦写入就不再修改。要纠正，就再追加一条新事件，例如 `run.cancelled`。

公开协议把事件分成几种固定句式：

| 句式 | 含义 | 例子 |
| --- | --- | --- |
| 生命周期 | 一段过程打开或结束 | `RUN_STARTED`、`RUN_FINISHED`、`RUN_ERROR` |
| 开始—增量—结束 | 一段内容分多次到达 | 文本、工具参数 |
| 快照 | 某一时刻的完整状态 | `MESSAGES_SNAPSHOT` |

AG-UI 规定：所有事件共用一个信封，必填字段只有 `type`；`timestamp` 可以有，但不得用来排序，顺序就是到达顺序。OpenAI 的 `response.output_text.delta` 在每条事件上带 `sequence_number`，以及这段文字属于哪个 `item_id`。Everruns 的会话内单调序号在入库时分配，实时推送和断线续传读的是同一份日志。

`RunEvent` 是事实记录。SSE 只是把这份记录尾部写成 `id`、`event`、`data` 文本帧的方式。

## 它解决什么

一次 Agent 运行不是一个 HTTP 正文。中间会有文字、工具调用、工具结果，然后才是下一句文字。客户端、断线重连、测试和以后的审计如果各认一种模型厂商的数据块，协议会散掉。

`RunEvent` 把这四件事收成一种格式：

- 界面可以边到边画，不用等整段回答结束。
- 断线后凭序号补缺。UseAgent 的规则是先落库再推送，迟到的连接和实时连接读到的是同一批事件。
- 换模型供应商时，只改内部翻译，不改客户端。
- 工具参数还没收齐时不能执行。AG-UI 要求消费方在 `TOOL_CALL_END` 之前不得把参数当完整 JSON 使用。

## 设计

每条事件分成信封和按 `type` 闭合的正文。信封上的序号只表示顺序，时间戳只供人读。

```text
seq         这个 run 内从 1 递增，由 Store 写入时分配
type        事件种类
thread_id   这段对话
run_id      这一次发问
payload     该 type 允许的字段，多出来的字段拒绝
```

`seq` 按 `run_id` 编号，因为取消和续传都针对一次发问。以后如果一条连接要跨越同一对话里的多次发问，再增加对话级的投递序号。UseAgent 把这两种序号分开，避免改旧行导致已经看过的客户端永远看不到修订。

第一版采用这些类型。名字用小写加点，便于和 SSE 的 `event:` 对齐；形状跟随 AG-UI 的生命周期和「开始—增量—结束」。

| type | 正文 | 作用 |
| --- | --- | --- |
| `run.started` | 无额外字段 | 流的第一条 |
| `message.started` | `message_id`，`role` | 打开一条助手消息 |
| `message.delta` | `message_id`，`delta` | 只带新增加的文字 |
| `message.completed` | `message_id` | 这条消息结束 |
| `tool.started` | `call_id`，`name` | 开始一次工具调用，参数尚未收齐 |
| `tool.arguments.done` | `call_id`，`arguments` | 参数文本收齐；这里才允许执行 |
| `tool.result` | `call_id`，`ok`，`output` | 执行结果，不重新打开那次调用 |
| `run.completed` | `usage` | 正常结束 |
| `run.failed` | `code`，`message`，`retryable` | 失败 |
| `run.cancelled` | `reason` | 被取消 |

同一次工具调用始终共用一个 `call_id`，同一条助手消息始终共用一个 `message_id`。工具前后会各有一条助手消息，所以文字增量必须带 `message_id`。

「北京今天热吗」的日志是：

```text
1  run.started
2  tool.started            call_id=c1  name=get_weather
3  tool.arguments.done     call_id=c1  arguments={"city":"北京"}
4  tool.result             call_id=c1  ok=true
5  message.started         message_id=m1
6  message.delta           message_id=m1  delta=北京
7  message.delta           message_id=m1  delta=今天 31 度
8  message.completed       message_id=m1
9  run.completed           usage={...}
```

模型内部还可以有 `model.finished`、`model.usage`。它们用来给循环记账，不进入这份对外日志。厂商原始块最多放在调试字段里，界面不得靠它决定行为。这是 AG-UI 对 `rawEvent` 的规定。

流必须以 `run.started` 或直接以 `run.failed` 开始。AG-UI 允许一开始就是错误，因为运行可能在真正开始前就失败。`run.completed`、`run.failed`、`run.cancelled` 之后不再追加。空的 `message.delta` 不记入 TTFT。

## 实现方式

用带判别字段的联合类型表示事件，每种 `type` 一份闭合模型。接收未知字段时忽略，便于以后加可选字段；缺少该类型的必填字段则拒绝。

写入只有一个入口。循环交出还没有序号的事实，Store 在追加时分配 `seq`，然后再通知订阅者。这样实时连接和以后的 `since` 回放不会各有一套编号。

SSE 帧把 `seq` 放在 `id:`，把 `type` 放在 `event:`，整条事件放在 `data:`。客户端用 `message_id` 和 `call_id` 把增量拼起来，并用 `seq` 丢掉重复帧。心跳注释不占序号。

第一版可以等参数收齐后连续写入 `tool.started` 和 `tool.arguments.done`，先不对外发送参数增量。类型先留出 `tool.arguments.done`，执行点就固定在这一条，而不是固定在 `tool.started`。

持久化之后仍然先写日志再推送。旧的文字增量可以折成最终消息，但回放必须得到同样的正文和同样的工具顺序。

## 代码

类型用判别联合，避免一个宽松的 `dict` 混进不属于该事件的字段：

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field

class RunStarted(BaseModel):
    seq: int
    type: Literal["run.started"]
    thread_id: str
    run_id: str

class MessageDelta(BaseModel):
    seq: int
    type: Literal["message.delta"]
    thread_id: str
    run_id: str
    message_id: str
    delta: str

class ToolArgumentsDone(BaseModel):
    seq: int
    type: Literal["tool.arguments.done"]
    thread_id: str
    run_id: str
    call_id: str
    arguments: str

RunEvent = Annotated[
    Union[RunStarted, MessageDelta, ToolArgumentsDone],
    Field(discriminator="type"),
]
```

Store 分配序号，循环不自己编号：

```python
def append(self, fact: Fact) -> RunEvent:
    self.seq += 1
    event = fact.with_seq(self.seq)
    self.events.append(event)
    self.changed.notify_all()
    return event
```

编码只负责搬上网络：

```python
def encode(event: RunEvent) -> str:
    payload = event.model_dump_json()
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"
```

客户端按标识拼接，而不是按到达时刻：

```python
def reduce(state: dict, event: RunEvent) -> None:
    if event.type == "message.delta":
        state["messages"][event.message_id] += event.delta
    elif event.type == "tool.arguments.done":
        state["ready_calls"].add(event.call_id)
    elif event.type in {"run.completed", "run.failed", "run.cancelled"}:
        state["done"] = True
```

第一版先实现 `run.started`、一对消息事件、`tool.arguments.done`、`tool.result` 和三种结尾。参数增量和快照事件以后再加，不必改已经发出的字段。
