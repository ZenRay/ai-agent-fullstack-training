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

`seq` 按 `run_id` 编号。取消和续传都针对一次发问，因此主干只使用这一套序号，不另设对话级投递序号。UseAgent 把两种序号分开，是为了避免改旧行后已读客户端看不到修订；本设计用只追加、不修改来满足这一点。

主干类型如下。名字用小写加点，便于和 SSE 的 `event:` 对齐；形状跟随 AG-UI 的生命周期和「开始—增量—结束」。

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

工具参数以完整文本写在 `tool.arguments.done`。执行只允许发生在这条事件之后。主干不另设参数增量事件。

持久化之后仍然先写日志再推送。旧的文字增量可以折成最终消息，但回放必须得到同样的正文和同样的工具顺序。

## 编码与客户端

服务端只有一套编码。CLI、Web 和其他客户端收到的帧相同，不为某种客户端增加事件种类或改字段。

差异在打开连接和呈现，拼接规则相同：

| | CLI | Web |
| --- | --- | --- |
| 打开运行流 | 读取 `POST /runs` 的响应体 | 用 `fetch` 读取同一条响应体。浏览器的 `EventSource` 只能发 GET，不能提交创建运行的 JSON，因此这条流不用 `EventSource` |
| 解码 | 跳过 `:` 注释，按空行分帧，用 `seq` 去重 | 与 CLI 相同 |
| 拼接 | 调用同一个 `reduce` | 调用同一个 `reduce` |
| 呈现 | 把 `delta` 追加到标准输出，把工具打成一行 | 按 `message_id` 更新对应节点，按 `call_id` 更新工具行 |
| 连接断开 | 结束本次读取，不取消运行。取消是单独的命令 | 关闭页面或流也不取消运行。取消是单独的请求 |
| 续传 | 自己带上最后的 `seq` | `POST` 流同样自己带 `seq`。只有以后的 GET 回放才适合交给 `EventSource` 自动发送 `Last-Event-ID` |

`reduce` 放在 CLI 和 Web 共用的模块里。各自只负责把投影画出来。

## 代码

主干类型一次定义完整。判别联合拒绝某个 `type` 不该有的字段；未知的额外字段忽略。

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    input_tokens: int
    output_tokens: int


class RunEventBase(BaseModel):
    model_config = ConfigDict(extra="ignore")
    seq: int
    thread_id: str
    run_id: str


class RunStarted(RunEventBase):
    type: Literal["run.started"]


class MessageStarted(RunEventBase):
    type: Literal["message.started"]
    message_id: str
    role: Literal["assistant"]


class MessageDelta(RunEventBase):
    type: Literal["message.delta"]
    message_id: str
    delta: str


class MessageCompleted(RunEventBase):
    type: Literal["message.completed"]
    message_id: str


class ToolStarted(RunEventBase):
    type: Literal["tool.started"]
    call_id: str
    name: str


class ToolArgumentsDone(RunEventBase):
    type: Literal["tool.arguments.done"]
    call_id: str
    arguments: str


class ToolResult(RunEventBase):
    type: Literal["tool.result"]
    call_id: str
    ok: bool
    output: str


class RunCompleted(RunEventBase):
    type: Literal["run.completed"]
    usage: Usage


class RunFailed(RunEventBase):
    type: Literal["run.failed"]
    code: str
    message: str
    retryable: bool


class RunCancelled(RunEventBase):
    type: Literal["run.cancelled"]
    reason: str


RunEvent = Annotated[
    Union[
        RunStarted,
        MessageStarted,
        MessageDelta,
        MessageCompleted,
        ToolStarted,
        ToolArgumentsDone,
        ToolResult,
        RunCompleted,
        RunFailed,
        RunCancelled,
    ],
    Field(discriminator="type"),
]
EVENT_ADAPTER = TypeAdapter(RunEvent)
```

Store 分配序号，循环不自己编号：

```python
def append(self, body: dict) -> RunEvent:
    self.seq += 1
    event = EVENT_ADAPTER.validate_python(
        {
            **body,
            "seq": self.seq,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
        }
    )
    self.events.append(event)
    self.changed.notify_all()
    return event
```

编码只负责把已编号的事件写成帧。注释帧不经过 `RunEvent`：

```python
def encode(event: RunEvent) -> str:
    payload = event.model_dump_json()
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


def encode_heartbeat() -> str:
    return ": keep-alive\n\n"
```

解码和投影也只有一套。CLI 把投影写成文本，Web 把投影画进节点，二者都先得到同一个 `View`：

```python
from dataclasses import dataclass, field


@dataclass
class View:
    status: str = "idle"
    messages: dict[str, str] = field(default_factory=dict)
    closed_messages: set[str] = field(default_factory=set)
    tools: dict[str, dict] = field(default_factory=dict)
    usage: Usage | None = None
    error: str | None = None
    done: bool = False
    last_seq: int = 0


def decode(frame: str) -> RunEvent | None:
    if frame.startswith(":"):
        return None
    data = next(
        line.removeprefix("data:").strip()
        for line in frame.splitlines()
        if line.startswith("data:")
    )
    return EVENT_ADAPTER.validate_json(data)


def reduce(view: View, event: RunEvent) -> None:
    if event.seq <= view.last_seq:
        return
    view.last_seq = event.seq
    if event.type == "run.started":
        view.status = "running"
    elif event.type == "message.started":
        view.messages[event.message_id] = ""
    elif event.type == "message.delta":
        view.messages[event.message_id] += event.delta
    elif event.type == "message.completed":
        view.closed_messages.add(event.message_id)
    elif event.type == "tool.started":
        view.tools[event.call_id] = {"name": event.name, "output": ""}
    elif event.type == "tool.arguments.done":
        view.tools[event.call_id]["arguments"] = event.arguments
    elif event.type == "tool.result":
        view.tools[event.call_id]["ok"] = event.ok
        view.tools[event.call_id]["output"] = event.output
    elif event.type == "run.completed":
        view.status = "completed"
        view.usage = event.usage
        view.done = True
    elif event.type == "run.failed":
        view.status = "failed"
        view.error = event.message
        view.done = True
    elif event.type == "run.cancelled":
        view.status = "cancelled"
        view.error = event.reason
        view.done = True
```

`message.completed` 不改变已拼好的正文，它标记这条 `message_id` 不再有增量。CLI 可以据此换行，Web 可以据此结束光标。
