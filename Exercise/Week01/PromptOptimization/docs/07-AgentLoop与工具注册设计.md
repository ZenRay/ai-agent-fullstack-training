# Agent Loop 与工具注册

Agent Loop 负责一次运行里的来回：调用模型，等这一轮工具参数都齐了再执行，把结果送回去，直到模型给出最终回答或碰到停止条件。工具注册表是它唯一允许调用的工具目录。循环不选供应商，也不编 SSE。

依据：

- [OpenAI Agents：运行循环](https://developers.openai.com/api/docs/guides/agents/running-agents)
- [Anthropic：交回工具结果](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)
- [Prompty：Agent Loop](https://prompty.ai/specification/agent-loop/)
- [72Technologies：流式工具调用](https://www.72technologies.com/blog/streaming-tool-calls-agent-responsiveness)
- [工具契约：读写、超时与幂等](https://thomasthelliez.com/blog/tool-contracts-enterprise-ai-agents/)

## 概念

一次用户发问对应一次 run。循环从线程里读出已有消息，然后重复三步：

1. 调用 `StreamingModel`，把内部事件写成 `RunEvent`。
2. 如果这次生成以工具调用结束，按 `call_id` 执行注册表里的工具，把 `tool.result` 写进日志，并把结果放回消息。
3. 再调一次模型，直到模型给出不含工具调用的最终文字，或碰到停止条件。

注册表里的每一项是一份契约。模型只能提出调用。名字是否存在、参数是否合法、读还是写、超时和幂等，都由注册表在执行前检查。

事件字段以 `docs/02-RunEvent设计.md` 为准。模型内部事件以 `docs/04-StreamingModel设计.md` 为准。

## 它解决什么

- 模型一次生成往往停在工具调用上，不会自己把接口跑完。没有循环，客户端只能看到半截「我去查一下」。
- 工具参数是流式碎片。半截 JSON 不能执行，也不能靠括号配对猜出还没到的字段。
- 同一轮可能有多个调用。漏回其中一个 `tool.result`，下一次模型请求就对不上 `call_id`。
- 工具失败如果直接让整个运行抛掉，前面已经生成的文字和已经完成的调用都留不下来。模型需要看见失败说明，才能改口或换一个工具。
- 写操作在超时后可能已经生效。没有幂等键和「先对账再重试」，循环的下一次尝试会做第二遍。
- 没有轮数、时间和取消这些边界，一个一直要求再查一次的模型会把同一次 run 跑完。

## 设计

### 循环的一轮

`StreamingModel` 只在参数完整时交出 `tool_call`。循环先把这一轮的完整调用记下来，等这次模型调用结束、结束原因是 `tool_use` 之后再执行。先到的调用不提前执行，因为后面还可能再来一个。

| 循环看见的事件 | 写入 Store | 接着做什么 |
| --- | --- | --- |
| 文字增量 | `message.started`，然后若干 `message.delta` | 继续读流 |
| 完整的 `tool_call` | `tool.started`，然后 `tool.arguments.done` | 先收齐这一轮，不执行 |
| `model.finished`，原因是 `stop` | `message.completed`，然后 `run.completed` | 结束这次 run |
| `model.finished`，原因是 `tool_use` | 这一轮每个调用的 `tool.result` | 把助手消息和全部结果放回线程，开始下一次模型调用 |
| `model.failed` | `run.failed` | 结束。已经写下的 `message.delta` 保留 |
| 取消标记 | 当前工具若已发出，写下它的 `tool.result`，然后 `run.cancelled` | 结束。不再开下一次模型调用 |

助手侧的工具调用先写入日志，再写入 `tool.result`。每个结果带原来的 `call_id`。`tool.arguments.done` 里的 `arguments` 是完整参数文本。注册表解析并校验之后才调用处理函数。

`length` 表示输出被截断，`content_filter` 表示被过滤。两者都结束这次 run，写成 `run.failed`，不再把半截内容交给下一轮。过滤不再重试。

### 停止条件

「模型说做完了」只是其中一条。一轮 run 同时受这些边界约束：

| 条件 | 结果 |
| --- | --- |
| 最终文字，且这一轮没有工具调用 | `run.completed` |
| 轮数达到上限 | `run.failed`，`retryable` 为假，已有事件保留 |
| 取消标记 | `run.cancelled` |
| 模型调用失败，且已有可见内容，或错误不可重试 | `run.failed` |
| 同一调用用相同参数反复出现，没有新结果 | `run.failed` |

轮数上限按模型调用次数计。天气这类流程用一个小的上限，例如 8。它和模型网关内部的重试不是同一层。可见内容之前的换路由仍由 `StreamingModel` 完成，不增加这里的轮数。每一轮模型调用使用一把新的幂等键。

人工确认先不放进前 10 步。注册表以后可以给写操作标上需要批准。那时候循环在 `tool.arguments.done` 之后停住，状态改为 `waiting_for_approval`，不执行工具。

### 注册表

当前 `app/tools/registry.py` 已有 `name`、`kind`、`handler`、`timeout_seconds`。实现循环时补上 `description` 和 `input_schema`。权限分级、补偿和批准留到后面。

| 字段 | 作用 |
| --- | --- |
| `name` | 稳定名字。模型调用这个名字，注册表用它查找 |
| `description` | 给模型看的用途、前置条件和副作用 |
| `input_schema` | JSON Schema。执行前校验，不合法就不调用处理函数 |
| `kind` | `read` 或 `write` |
| `timeout_seconds` | 单次执行的上限，默认 30 秒 |
| `handler` | 真正的实现，返回文本 |

校验失败、未注册的名字、处理函数抛出的异常，都写成 `tool.result`，`ok` 为假，`output` 是可供模型阅读的说明。循环继续，把这个结果送回下一次模型调用。工具失败不是这次 run 的终态。说明要写出发生了什么，以及模型可以怎么改，而不是只写「失败」。

读和写分开处理：

| 种类 | 超时或失败之后 |
| --- | --- |
| `read` | 可在同一轮的超时预算内再试一次。结果直接作为 `tool.result` |
| `write` | 用 `hash(run_id + call_id + 规范化参数)` 做幂等键。发送前先占收据。超时后先对账；结果不明就不再自动打第二次，把「结果不明」写进 `tool.result`。步骤见 `docs/12-收尾设计.md` |

取消发生在执行之前，工具不会发出去。取消发生在执行之中，等这次调用返回或超时，写下 `tool.result`，再写 `run.cancelled`。循环不把取消当成副作用已经撤回。

同一轮有多个调用时，读操作可以并行，写操作按顺序执行。全部结果到齐后才开始下一次模型调用。模型是否会在一次生成里发出多个调用，由模型侧决定。注册表决定这些调用如何执行。

幂等收据和持久化一起做。内存版在第 10 步之前可以先把键放在进程里，重启之后写操作的对账能力随之消失。这和 `docs/06-RunStore设计.md` 里的收据约定是同一件事。

## 实现方式

循环放在 `app/loop/`，注册表放在 `app/tools/`。每个工具一个文件，向注册表登记自己。`execute_run(store, model, tools, run_id)` 是循环的入口。当前这个函数还未实现。

依赖保持单向。循环向 Store 追加事件，向 `StreamingModel` 要流，向注册表要工具。注册表不认识 HTTP，也不写 SSE。`api/` 只负责启动这次运行和设置取消标记。

一次运行开始时，循环从 Store 读出该 `thread_id` 的消息，追加 `run.started`。工具结果除了写入 `tool.result`，也追加到线程消息里，下一轮模型才能看见。

未知工具由注册表返回 `ok` 为假的结果，不把 `KeyError` 抛到循环外面。现有 `get` 在名字不存在时抛出 `KeyError`，接到循环里时要收成 `tool.result`。

## 代码

注册表只做查找、校验和超时。读写差异放在执行前的策略里。

```python
async def execute_tool(tools, run_id, call_id, name, arguments):
    tool = tools.find(name)
    if tool is None:
        return {"ok": False, "output": f"未注册的工具: {name}"}
    parsed = parse_and_validate(tool.input_schema, arguments)
    if parsed is None:
        return {"ok": False, "output": "参数不符合 schema"}
    if tool.kind == "write":
        key = hash_key(run_id, call_id, parsed)
        cached = await effects.get(key)
        if cached is not None:
            return cached
    try:
        output = await asyncio.wait_for(tool.handler(**parsed), tool.timeout_seconds)
        result = {"ok": True, "output": output}
    except TimeoutError:
        text = "超时，结果不明" if tool.kind == "write" else "超时"
        result = {"ok": False, "output": text}
    if tool.kind == "write" and result["ok"]:
        await effects.put(key, result)
    return result
```

循环只处理一轮模型流和一轮工具执行。停止条件包在外层。

```python
async def execute_run(store, model, tools, run_id, max_turns=8):
    await store.append(run_id, {"type": "run.started"})
    run = store.get_run(run_id)
    messages = store.messages(run.thread_id)
    for turn in range(max_turns):
        if run.cancel_event.is_set():
            await store.append(run_id, {"type": "run.cancelled", "reason": "client"})
            return
        calls = []
        async for event in model.stream(messages, cancel=run.cancel_event, idempotency_key=f"{run_id}:{turn}"):
            if event.type == "text.delta":
                await append_message_delta(store, run_id, event)
            elif event.type == "tool_call":
                calls.append(event)
                await store.append(run_id, {"type": "tool.started", "call_id": event.call_id, "name": event.name})
                await store.append(run_id, {"type": "tool.arguments.done", "call_id": event.call_id, "arguments": event.arguments})
            elif event.type == "model.finished" and event.finish_reason == "stop":
                await finish_message(store, run_id)
                await store.append(run_id, {"type": "run.completed", "usage": event.usage})
                return
            elif event.type == "model.failed":
                await store.append(run_id, {"type": "run.failed", "code": event.code, "message": event.message, "retryable": event.retryable})
                return
        for call in calls:
            result = await execute_tool(tools, run_id, call.call_id, call.name, call.arguments)
            await store.append(run_id, {"type": "tool.result", "call_id": call.call_id, **result})
            messages.append(tool_message(call, result))
    await store.append(run_id, {"type": "run.failed", "code": "max_turns", "message": "超过轮数上限", "retryable": False})
```

对应关系：

- `tool.started` 和 `tool.arguments.done` 出现在执行之前，界面可以先显示「正在调用」。处理函数只在这一轮模型流结束之后运行。
- 参数完整才进入 `calls`。半截参数留在 `StreamingModel` 里，到不了注册表。
- `execute_tool` 的失败变成 `tool.result`。循环把消息送回模型，不把这次 run 标成失败。
- 写操作先查幂等键。同一次 `call_id` 的重试读到同一份结果。
- `max_turns` 和取消标记是循环自己的边界。模型网关的重试发生在单次 `stream` 内部。
