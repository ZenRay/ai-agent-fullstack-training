# SSE 设计

SSE 只做一件事：把已经编号的 `RunEvent` 写成一条不会被代理攒住的 HTTP 响应。事件本身仍然由 Store 分配序号。SSE 不创造事实，也不因为客户端断开就取消运行。

依据：

- [WHATWG Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html)
- [MDN：使用 Server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)
- [AG-UI：HTTP + SSE](https://docs.ag-ui.com/spec/1.0/basic/transports/http-sse)
- [代理与 CDN 配置](https://www.server-sent-events.com/sse-protocol-fundamentals-architecture/proxy-and-cdn-configuration-for-sse/)
- [重连、代理与慢消费者](https://blog.oneenv.io/articles/server-sent-events-in-node-js-reconnection-proxies-and-scaling)

## 概念

SSE 是普通 HTTP 响应上的一种文本分帧。响应头是 `Content-Type: text/event-stream`，正文用 UTF-8，帧之间用一个空行隔开。规范承认四个字段：

| 字段 | 作用 |
| --- | --- |
| `id` | 记下这条事件的标识。使用 `EventSource` 自动重连时，会把它放进 `Last-Event-ID` |
| `event` | 事件名。没有这个字段时，浏览器把它当成默认的 `message` |
| `data` | 正文。连续多行 `data:` 会用换行拼起来，末尾那个换行会被去掉 |
| `retry` | 重连等待的毫秒数，必须是十进制整数 |

以冒号开头的行是注释。规范不把它派发给监听器，所以它适合做保活。空的 `data` 也不会派发事件。`id` 一旦被设置就一直留着，直到下一条带 `id` 的事件把它换掉；写一行空的 `id:` 才会清空。

心跳是一帧，但不是 `RunEvent`：

```text
: keep-alive

```

它没有 `data`，解析后得到空，不进入 `reduce`，也不占用 `seq`。

## 它解决什么

一次运行会连续发出很多条 `RunEvent`。如果等运行结束再返回一个 JSON，界面就无法逐字更新，工具步骤也无法中途出现。

只把连接打开还不够：

- 反向代理默认把响应攒满再转发，客户端会先沉默，然后一次收到一大段。
- 工具或思考阶段几十秒没有新事件，代理的空闲超时会把连接切断。常见空闲限制大约 60 秒。
- TCP 可以在一帧的半中间切开字节，也可以把多帧并进同一次读取。
- 浏览器自带的 `EventSource` 只能发 GET。创建运行需要 POST JSON，所以这条流不能交给 `EventSource`。AG-UI 的客户端用 `fetch` 读 `response.body`。

`Last-Event-ID` 只在使用 `EventSource` 自动重连时出现。自己用 `fetch` 读 POST 响应时，客户端要自己记住最后的 `id`。

## 事件帧、代理缓冲、写满的订阅者

这三件事都发生在 `RunEvent` 已经写进 Store 之后。

| | 是什么 | 存在哪里 | 满了或断了会怎样 |
| --- | --- | --- | --- |
| 事件帧 | 以空行结尾的一段 UTF-8 | 某一条 HTTP 响应里 | 这一帧没送到，Store 里的 `RunEvent` 还在 |
| 代理缓冲 | 反向代理把上游响应攒起来再转发 | 网关和客户端之间 | 帧被延迟成一批一批送达 |
| 订阅者队列 | 这一条连接上的有界缓冲 | 网关进程里 | 只断开这条连接，不取消 run |

一帧的样子是：

```text
id: 6
event: message.delta
data: {"seq":6,"type":"message.delta","thread_id":"th_1","run_id":"run_1","message_id":"m1","delta":"北京"}

```

最后那个空行表示这一帧结束。`id`、`event`、`data` 是帧里的字段。TCP 不认识这个空行，所以解析器先攒字节，看到 `\n\n` 才切出一帧。

代理缓冲的处理是让每一帧写出后就往下游送：

- 响应头带 `Cache-Control: no-cache, no-transform` 和 `X-Accel-Buffering: no`
- 这条路径关闭 `proxy_buffering` 和 gzip
- 读超时长于心跳间隔。没有新事件时，每 15 秒发一帧 `: keep-alive`

订阅者队列满了就断开这一条连接。运行继续往 Store 追加。这个订阅者下次带上最后收到的 `seq` 再连，只补它缺的帧。一个读得慢的连接因此不会把进程内存撑大，也不会拖住仍在写日志的运行。

## 设计

服务端只编码，不按 CLI 或 Web 分两套帧。

- 只用 LF（`\n`）。AG-UI 把这定成生产者必须遵守的写法。
- `id` 使用这个 run 的 `seq`，十进制字符串。随机 UUID 不能回答「这个序号之后还有什么」。
- `event` 等于 `RunEvent.type`。`data` 是这一条事件的一行 JSON，不美化打印。JSON 转义会把正文里的换行变成 `\n`，因此一行 `data:` 不会被 SSE 拆开。
- 每条 `data` 里只放一个 JSON 对象。
- 流开始时写一次 `retry: 3000`。`fetch` 客户端要自己实现等待和抖动。
- 参数错误在写出响应头之前用 HTTP 4xx 返回。头一旦发出，失败只能是一条 `run.failed`，然后关闭正文。HTTP 200 只说明流打开了。
- 某个订阅断开，只停止向这条响应写字节。取消仍是单独的请求。
- 重连时只补 `seq` 大于游标的事件，再接上实时尾部。游标已经不在保留窗口里时，要明确告诉客户端重新同步。

AG-UI 的基础绑定要求消费方忽略 `id` 和 `event`，并且不做断线续传。这里仍然写这两个字段，因为 CLI、日志和 GET 回放都要靠它们。POST 这条流的客户端必须自己解析。

## 实现方式

`sse` 接收已经有 `seq` 的 `RunEvent`，输出字符串。它不分配序号，不写 Store，也不把心跳放进日志。

写的一侧每产生一帧就刷新。客户端断开时结束这个生成器，停掉心跳任务，不取消 run。

读的一侧是一个有状态的缓冲：

1. 把新字节用可以跨块续上的 UTF-8 解码器解出来。
2. 按 `\n\n` 切开，最后一段不完整的文字留在缓冲里。
3. 以 `:` 开头的行忽略。
4. 解析 `id`、`event`、`data`、`retry`。多行 `data` 用换行接上，并去掉末尾换行。
5. `data` 为空就跳过。否则把 JSON 校验成 `RunEvent`，再交给同一个 `reduce`。
6. `reduce` 再用 `seq` 丢掉重复帧。投递是至少一次。

CLI 和 Web 共用这个解析器和 `reduce`。CLI 把投影打到标准输出；Web 按 `message_id` 和 `call_id` 改节点。

## 代码

编码只负责整条事件。心跳和重连间隔单独生成，不经过 `RunEvent`：

```python
def encode(event: RunEvent) -> str:
    payload = event.model_dump_json()
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


def encode_heartbeat() -> str:
    return ": keep-alive\n\n"


def encode_retry(milliseconds: int = 3000) -> str:
    return f"retry: {milliseconds}\n\n"
```

解析器要能吃下被拆开的字节。完整帧交出来，半帧留到下一次：

```python
class FrameBuffer:
    def __init__(self) -> None:
        self._text = ""

    def feed(self, chunk: str) -> list[str]:
        self._text += chunk
        frames: list[str] = []
        while "\n\n" in self._text:
            frame, self._text = self._text.split("\n\n", 1)
            frames.append(frame)
        return frames


def parse_frame(frame: str) -> RunEvent | None:
    data_lines: list[str] = []
    for raw in frame.split("\n"):
        if raw.startswith(":"):
            continue
        if raw.startswith("data:"):
            value = raw.removeprefix("data:")
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if not data_lines:
        return None
    return EVENT_ADAPTER.validate_json("\n".join(data_lines))
```

调用顺序是 `feed`，再对每个完整帧 `parse_frame`。注释帧得到 `None`。校验通过后再 `reduce`。

响应头：

```python
headers = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}
```

生成循环在有事件时写 `encode(event)`，空闲 15 秒写 `encode_heartbeat()`。发现客户端已经断开，就退出这个生成器。Store 里的运行继续追加，直到它自己的终止事件。
