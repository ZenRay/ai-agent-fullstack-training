# Streaming Agent Gateway

本目录是一次 Agent 运行的网关。模型路由、供应商密钥、限流和花费记录留在 `course_code/week01/1-7/llm-gateway` 或 LiteLLM。完整设计见 `docs/01-设计说明.md`，实现时以该文件为准。

## 目录

每个模块是一个目录，目录内再拆文件。扩展时只在对应目录加文件。

```text
app/events/   RunEvent
app/sse/      编解码与心跳
app/model/    StreamingModel 与模型网关适配器
app/tools/    工具注册；每个工具一个文件
app/store/    线程、运行状态、事件日志、取消标记
app/loop/     一次运行的控制过程
app/api/      创建运行与取消
app/main.py   组装
cli/          第 8 步
tests/        与 app 下的目录对应
```

依赖单向：`events` 不认识网络；`sse` 只转换 `RunEvent` 与文本帧；`loop` 只向 `store` 追加对象；`api` 负责 HTTP。`sse` 的 `: keep-alive` 不写入 `RunEvent`，也不进 Store。

## 标识

- `thread_id` 是一段对话，保存消息列表，跨多次发问保留。
- `run_id` 是一次发问，拥有独立的事件序号、状态和取消标记。
- 同一线程的下一次发问复用 `thread_id`，新建 `run_id`。
- 取消一个 run 只停止该次执行，不删除线程和已有消息。

## 事件

对外事件以 `docs/02-RunEvent设计.md` 的全部主干类型为准。CLI 和 Web 共用同一套编码和 `reduce`。

模型内部事件是文字增量、完整的 `tool_call`、`model.finished`、`model.usage`。循环写成消息事件，以及 `tool.started`、`tool.arguments.done`、`tool.result`。同一次调用共用 `call_id`。执行工具只发生在 `tool.arguments.done` 之后。

一次运行以 `run.started` 开始，以 `run.completed`、`run.failed`、`run.cancelled` 之一结束。TTFT 停在第一个非空 `message.delta`。

## 请求

- `POST /v1/threads/{thread_id}/runs`：创建运行，响应直接是 SSE，同时把事件写入 Store。
- `POST /v1/runs/{run_id}/cancel`：取消该 run 和正在进行的模型调用。
- `GET /v1/runs/{run_id}/events?since=`：第 10 步才实现。在此之前，补事件只通过 Store 的 `since` 读取测试。

流打开前的参数错误返回 HTTP 4xx。响应头发出后，失败只能是 `run.failed`。

订阅断开只停止这一条 SSE，运行继续写 Store。只有取消标记才让 `StreamingModel` 关闭上游。慢客户端用有界缓冲断开，不在内存里无限排队。

## 模型调用

- 首个非空 `message.delta` 之前可以换路由。已经有正文之后只发 `run.failed`。
- 带工具的失败用原始消息重试，不把半截回答拼进下一次提示词。
- 每次模型调用带幂等键。
- 用量可放进 `run.completed`。独立账本、Prompt 和回答正文不写入用量记录。
- 流式输出不做发出后纠错。严格 JSON Schema 走非流式。

## 实现顺序

按 `docs/01-设计说明.md` 的 1 到 10 步进行，不跳过，也不提前做后续步骤的能力。

模块包已建立。`events`、`sse` 和内存 `store` 已按主干接好。模型流式适配、Agent Loop 和 HTTP 路由仍按后续步骤实现。第 1 到第 8 步使用内存 Store。SQLite、Redis、HTTP 回放、人工确认、多 Agent 路由和独立用量账本属于第 10 步或之后。

每一步都带测试。第 9 步集中验收：首字节前可重试、已有正文后只失败、取消会停上游、订阅断开不会停运行、Store 能按 `since` 读回。

## 修改原则

- 协议和规则以 `docs/01-设计说明.md` 为准。代码与文档冲突时，先改文档再改代码，或在同一次修改里让二者一致。
- 新增事件种类、接口或模块前，先更新 `docs/01-设计说明.md`。
- 供应商密钥不进入本服务。
- 回复和文档使用简体中文。
- 提交说明只写本次变更，不得加入 coworker。
