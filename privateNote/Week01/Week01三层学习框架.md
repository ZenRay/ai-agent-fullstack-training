---
title: "Week01 三层学习框架：概念地图、渐进实践与自我检验"
create_time: 2026-11-12
category: learning
subcategory: agent-engineering
purpose: study-guide
status: active
tags: [AI-Agent, Harness, LLM-Gateway, Structured-Output, Streaming, Evaluation]
project: ai-agent-fullstack-training
---

# Week01 三层学习框架：概念地图、渐进实践与自我检验

> **适用范围**：`course_materials/week01/`、`course_code/week01/`。
>
> **课程证据边界**：本文依据课件、README 与示例代码组织学习；其中的“应验证”是学习建议，不等同于本机已完成真实模型调用或生产环境验证。

## 怎样使用这份文档

Week01 不宜仅按 `1-1` 到 `1-7` 的顺序记忆。章节是授课顺序；概念簇才是检索、DeepSearch、设计和排障时需要的结构。

本文的三层依次回答三个问题：

```text
第一层：这些概念分别是什么，又如何组成 Agent Harness？
第二层：怎样亲手制造并修复各类失败？
第三层：离开示例后，怎样确认自己能判断、设计并举证？
```

贯穿案例使用“报销制度问答 Agent”：当资料不足时检索；资料充分时回答；模型的每个决定都需要被程序校验；全过程应可见、可停、可追踪。

---

# 第一层：按概念簇建立完整地图

## 1. 总地图：从概率模型到可治理 Agent

```text
业务目标与验收
  → Prompt / Context（模型应该看到什么、遵守什么）
  → Model Runtime（如何发起受控调用）
  → Structured Decision（模型建议是否符合协议）
  → Tool Runtime / Sandbox（是否允许把建议变成动作）
  → Agent Loop（如何观察、执行、回写、停止）
  → Streaming / Event Store（过程怎样可见、可停、可恢复）
  → LLM Gateway（模型调用怎样统一路由、治理和审计）
  → Eval / Trace（怎样证明它真的有效）
```

课程在 1-1 提出能力演进：`Prompt → Context → Harness → Loop → Graph`。这些层是叠加关系，不是后者替代前者。Week01 的主要工作是给 Prompt、Harness 与 Loop 打基础；Graph 是后续多 Agent 协作与分支控制的方向。

## 2. 概念簇 A：Agent 工程全景与职责

**解决的问题**：为什么“会调用模型”不等于“能交付 Agent”？

| 概念 | 本课程中的含义 | 相关概念 / 检索词 |
| --- | --- | --- |
| 长程任务 | Agent 在多步任务中保持目标、处理失败并最终结束 | `long-horizon agent`, task decomposition, state management |
| Harness | 围绕模型的运行外壳：管理上下文、工具、状态、权限与失败边界 | `agent harness`, runtime, execution boundary |
| Agent Loop | 模型决策、工具执行、结果回写、再决策的控制循环 | `agent loop`, observe plan act, termination condition |
| Evaluation | 用案例、测试与 Trace 判断改动是否真的改进 | `agent evaluation`, regression set, trace-based evaluation |
| 工程师职责 | 定义目标、上下文、约束、权限、预算、失败边界和验收 | requirements, acceptance criteria, guardrails |

课程的核心判断是：模型生成候选结果；工程师使候选结果可验收、可执行、可回滚。

## 3. 概念簇 B：模型运行时、消息与上下文

**解决的问题**：模型在一次调用中究竟看见什么，又输出了什么？

| 概念 | 关键理解 | 相关概念 / 检索词 |
| --- | --- | --- |
| Chat Completions | 以 `messages` 为中心的兼容性基线 | `chat completions`, OpenAI compatible API |
| Responses API | 面向输出项的接口，适合承载消息、推理、拒答和工具调用等丰富语义 | `Responses API`, output items, reasoning item |
| 消息角色 | `system` 是全局契约；`user` 是用户目标；`assistant` 是既有决策；`tool` 是可追溯的外部观察 | message roles, conversation state, tool result |
| Context Window / Token | 上下文容量与计费、延迟、裁剪策略相关；不是无限历史记录 | context window, token budget, context compression |
| 输出项 | Agent 应消费“下一步事件”，而不只打印文本 | message, function call, refusal, tool call |

**检索提醒**：搜索 Context 时同时带上来源追踪、裁剪、摘要、检索与预算；不要只搜索“上下文窗口大小”。

## 4. 概念簇 C：生成控制与模型接入

**解决的问题**：如何把概率性的模型调用限制在明确边界内，并在不改 Loop 的情况下换模型？

| 概念 | 关键理解 | 相关概念 / 检索词 |
| --- | --- | --- |
| `temperature` | 控制随机性，不控制事实正确性或任务理解 | sampling temperature, reproducibility |
| `top_p` | 控制候选 token 的概率质量范围；通常与 temperature 选择一个作为主要调节项 | nucleus sampling, top-p |
| `max_tokens` | 输出预算与截断边界；被截断的结构化输出不能当完整结果消费 | output budget, finish reason length |
| Model Adapter | 将统一请求翻译成供应商协议，将响应与错误翻译回统一协议 | model adapter, provider abstraction |
| 能力声明 | 模型是否支持流式、结构化输出、工具调用等，不能只按名称猜测 | capability registry, model capability |
| 错误分类 | 超时、限流、认证、参数非法、截断、拒答等应有不同策略 | error taxonomy, retryable errors |

**关系**：Adapter 隔离供应商；能力声明限制 Router 的选择；参数与 Token 预算保证调用可复现、可解释。

## 5. 概念簇 D：行为契约与数据契约

**解决的问题**：如何让模型既遵守行为边界，又输出能被程序安全消费的数据？

| 子簇 | 概念 | 要点 |
| --- | --- | --- |
| 行为契约 | System Prompt、角色、目标、边界、流程、输出格式 | Prompt 是行为契约，不是“让模型更聪明”的魔法文本 |
| 行为校准 | Few-shot、任务拆解 | 示例校准行为，不授予权限；示例偏差会放大误判 |
| Prompt 工程化 | 模板、严格变量、版本、回归测试 | 模板是最小工程接口；缺变量或超预算应在请求前失败 |
| 注入防护 | Prompt Injection、不可信输入 | 用户资料、网页、工具输出不能升级为系统指令 |
| 数据契约 | JSON、JSON Schema、Pydantic、Structured Output | “像 JSON”不足以让程序安全分支 |
| 业务校验 | 跨字段规则、`additionalProperties: false` | 类型正确不代表业务组合正确，例如 `finish` 必须同时给出答案 |
| 有界修复 | 本地校验、有限修复、失败降级 | 失败要明确返回，不能无限重试或静默猜测 |

**检索词组合**：`prompt injection untrusted data tool authorization`；`structured output json schema pydantic local validation repair loop`；`cross-field validation business rules`。

## 6. 概念簇 E：Agent Loop、工具与受控执行

**解决的问题**：模型的建议如何变成安全、有限、可解释的外部动作？

| 概念 | 关键理解 | 相关概念 / 检索词 |
| --- | --- | --- |
| Agent Decision | 模型提出的下一步，例如 `search` 或 `finish`；不是自动授权 | decision schema, action selection |
| Tool Definition / Call / Result | 工具输入、执行请求与事实结果必须各自有协议和来源 | tool schema, function calling, tool result |
| Tool Runtime | 统一执行参数校验、授权、超时、重试、审计与 Trace | tool runtime, tool governance |
| Sandbox | 对文件、网络、命令等副作用设置隔离边界 | sandbox, least privilege |
| 退出条件与最大轮次 | 防止循环发散、重复工具调用和不可控成本 | loop guard, max steps, termination condition |
| 副作用与幂等性 | 写入、付款、删除等动作不能因重试而重复发生 | idempotency, approval, side effects |

**关键边界**：Prompt 可以建议“不要删除文件”，但真正阻止删除的是工具权限、参数校验、Sandbox 或审批机制。

## 7. 概念簇 F：Streaming、事件与恢复

**解决的问题**：长时间 Agent Run 如何让用户看得见、能够取消、并在故障后恢复？

| 概念 | 关键理解 | 相关概念 / 检索词 |
| --- | --- | --- |
| Streaming | Agent Runtime 到 Web/CLI/IDE 的实时事件通道，不是纯视觉打字效果 | streaming agent runtime, event channel |
| SSE / Chunk | 传输协议与上游片段；不等于业务事件模型 | Server-Sent Events, SSE chunk |
| Harness Event | 如 `run.started`、`content.delta`、`tool.started`、`run.completed`；对外有稳定语义 | typed event protocol, event lifecycle |
| TTFT | 第一个有业务意义的 delta 到达时间，不是连接建立事件 | time to first token, TTFT |
| Cancel | 取消应从客户端贯穿 Loop、模型和工具 | cooperative cancellation, abort propagation |
| Event Store / Replay | 保存事实事件、序号和去重信息，用于断线续传和审计 | event replay, resume cursor, deduplication |
| Checkpoint | 保存可恢复状态或增量；须同时设计存储、权限与保留策略 | checkpointing, recovery policy |

**重要边界**：已经向客户端发送内容后，不能用另一个模型透明接续；这会导致重复或语义断裂。此时应发出单一错误终态，而非拼接两段模型回答。

## 8. 概念簇 G：韧性与失败语义

**解决的问题**：失败后什么时候该重试、切换、修复、停止或交给用户？

| 阶段 | 常见失败 | 基本处置方向 |
| --- | --- | --- |
| 认证 / 请求校验 | 无效 Key、字段非法、Schema 缺失 | 不重试；修正请求或配置 |
| Prompt / 路由 | 缺变量、超预算、无兼容模型 | 修正输入或配置后再试 |
| 连接 / 首 Token 前 | 网络抖动、短暂 5xx、429、超时 | 有上限的重试或 Fallback |
| 已开始 Streaming | 中断或上游错误 | 不盲目重新生成；发送错误终态或走恢复协议 |
| 输出校验 | JSON/Schema 不合规 | 有限修复；失败后显式降级 |
| 内容拒答 | 安全策略拒绝 | 不用换模型绕过安全边界 |

相关概念包括重试预算、指数退避、jitter、Fallback、Circuit Breaker、超时预算和稳定错误码。

## 9. 概念簇 H：LLM Gateway 与平台治理

**解决的问题**：多个 Agent 与应用如何以统一方式、安全地使用多家模型？

```text
Client / Agent
  → OpenAI-compatible API
  → 请求规范化、鉴权、限流、Prompt 解析
  → Router（能力、优先级、成本、健康状态）
  → Provider Adapters
  → 统一结果 / SSE / Schema 校验
  → Trace、Usage、Cost Ledger
```

| 概念 | 关键理解 | 相关概念 / 检索词 |
| --- | --- | --- |
| Gateway 边界 | Loop 只依赖模型端口；供应商密钥和具体协议留在 Gateway 内 | LLM gateway, stable boundary |
| OpenAI Compatible API | 是外部兼容层，不应迫使内部领域模型跟随某供应商变化 | API normalization, anti-corruption layer |
| Router | 按能力、优先级、成本和健康状态选择路线，不是“永远挑最大模型” | capability routing, weighted round robin |
| Prompt Bundle | Prompt 模板、变量 Schema、示例、输出 Schema、工具版本、预算与 Eval 门槛组成的可发布资产 | prompt bundle, prompt versioning |
| 安全与资源治理 | Gateway Key、限流、健康检查、密钥隔离、数据保留 | API authentication, rate limiting, secrets management |
| 调用账本 | Token、Cost、TTFT、Latency、重试、Fallback、错误与模型决策 | usage ledger, LLM observability |

调用链关联至少要能区分 `trace_id`、`run_id`、`step_id` 与 `call_id`：一个 Agent Run 可含多个步骤，一个步骤又可含多次模型调用。

## 10. 概念簇 I：测试、评测与证据

**解决的问题**：如何证明系统不是“恰好跑通一次”？

| 证据类型 | 用途 | 不能证明什么 |
| --- | --- | --- |
| 单元测试 / Mock Transport | 验证协议转换、错误分支、路由、修复与事件序列 | 真实供应商在当前环境一定可用 |
| Eval Cases / 回归集 | 比较版本并防止行为退化 | 覆盖了全部真实用户分布 |
| Trace / Usage | 回放单次运行的路由、成本、延迟和失败 | 输出本身一定正确或业务目标已达成 |
| 真实集成测试 | 验证密钥、网络、供应商与真实返回 | 长期生产稳定性 |

**学习原则**：接口 `200`、Demo 成功、README 声称支持，均不等于真实场景已经被验证。记录“课程设计的预期”和“本次本机实际结果”时必须分开写。

---

# 第二层：用渐进实践把概念变成能力

## 1. 统一实践方法

不要照抄最终工程。每次实践遵循同一循环：

```text
任务与失败场景
  → 运行最小坏版本，保留现象 / Trace
  → 只加入一个概念对应的边界
  → 验证正常路径和反例
  → 复盘：它解决了什么，仍不能解决什么
```

每次提交应至少留四类材料：目标、坏版本或失败复现、最小改进、验证证据。

## 2. Week01 递进式 Mini Harness

| 阶段 | 对应概念簇 | 动手任务 | 最小验收证据 |
| --- | --- | --- | --- |
| P1 | A、E、I | 实现 `search` / `finish` 的最小 Loop，设置最大轮次和退出条件 | 成功、无资料、工具失败三类用例；循环不会无限执行 |
| P2 | B、C、G | 将模型调用收敛为统一接口，先接 Fake Adapter，再接真实 Adapter | 不改 Loop 即能替换 Adapter；超时、限流、截断有不同结果 |
| P3 | F | 将 Run 输出为类型化 SSE 事件，客户端按事件展示 | 事件顺序正确；取消后只有一个终态；记录 TTFT |
| P4 | D | 将 Prompt 改成角色、目标、边界、流程、输出格式，并使用严格模板变量 | 缺变量被拒绝；不可信资料不能覆盖系统规则；约束有回归用例 |
| P5 | D、E、G | 为 `AgentDecision` 写 Schema 与跨字段校验，失败后有限修复 | 非法 JSON、缺字段、非法组合均不会触发工具；修复次数可见且有上限 |
| P6 | H | 将模型调用改为走 Gateway，接入模型别名、Trace 与主备 Fallback | Agent 不持有供应商 Key；可查询实际模型、尝试次数、成本和延迟 |
| P7 | I | 把前述故障用例固定成 Eval / 自动化测试 | 改 Prompt、模型或路由后可回归；证据能定位失败处 |

## 3. 实践中必须保持的边界

- 先用 Fake Provider、Mock Transport 或本地桩验证协议和失败分支；真实调用只验证实际连接与供应商行为。
- 任何有副作用的工具先限制为只读或 Sandbox；写入、删除、付款等动作需要显式权限与审批设计。
- 一次只改变一个变量：例如只改一个采样参数、一个 Prompt 版本或一条路由规则，才能解释评测变化。
- 流式正文一旦输出，不把备用模型结果拼接到同一响应中。
- 不把真实 Key 写入代码、笔记或 Trace；Trace 默认不记录 Prompt 和模型正文，除非明确设计了数据治理策略。

---

# 第三层：离开示例后的自我学习检验

## 1. 四级检验标准

| 等级 | 要检验的能力 | 通过的表现 |
| --- | --- | --- |
| 复述 | 能否用自己的话说明术语 | 能解释 Harness、Adapter、TTFT、Schema 的作用与边界 |
| 判断 | 能否发现错误设计 | 能从代码、输出或 Trace 找到越权、不可恢复或不一致之处 |
| 设计 | 能否在约束下做取舍 | 能写出协议、事件、重试策略或最小测试集 |
| 举证 | 能否证明设计真的有效 | 能给出失败复现、测试、事件序列或 Trace，而非只说“应该可以” |

## 2. 按概念簇自检

### A / E / I：Loop、Harness、Eval

- 不看代码，画出 `search → tool result → finish` 的消息、状态与退出条件。
- 判断：模型直接执行 Shell、没有最大轮次、没有工具结果记录，分别缺少什么边界？
- 设计：为一个新任务定义工具、最大轮次、成功标准，以及成功/无资料/工具失败三条 Eval Case。

### B / C：模型运行时、参数与 Adapter

- 解释 `temperature`、`top_p`、`max_tokens` 的边界；说明答案质量差为何不能优先“调高温度”。
- 判断“429 限流”“输出长度截断”“请求字段非法”各自应否重试。
- 设计一个 `Model Adapter` 最小接口，并说明它为什么不应泄露供应商 SDK 类型给 Loop。

### D：Prompt 与 Structured Output

- 将“你是专业客服”改写成含角色、目标、边界、流程、信息不足处理和输出格式的行为契约。
- 给出用户上传的一段“忽略系统规则”的文本，说明它为何是不可信数据，怎样隔离。
- 给四份输出：非法 JSON、缺字段、跨字段规则错误、完全合法；说明每一份在哪一层被接受或拒绝。
- 设计一个决策 Schema，至少含两条跨字段业务规则与一条失败降级策略。

### F / G：Streaming、恢复与失败语义

- 区分 TTFT、生成耗时与端到端耗时。
- 判断事件序列是否错误：`run.completed` 后又出现 `tool.started`；或在已有多个 delta 后切换备用模型。
- 设计最小事件协议，说明取消、失败、断线重连、事件去重各如何表达。
- 判断哪些失败可在首 Token 前有限重试，哪些必须结束流或等待用户修正。

### H：Gateway 与治理

- 画出 Loop、Gateway、Router、Provider Adapter、供应商的责任边界。
- 解释为什么 Agent 不应保存多个供应商 Key，为什么外部 OpenAI Compatible API 不应直接成为内部领域模型。
- 给出一条调用 Trace，解释 requested/resolved model、retry、fallback、TTFT、Token、Cost 与稳定错误码各用于什么判断。
- 设计“主模型连接失败、备用模型成功”的验收场景：响应、Trace 和用量记录中必须出现哪些事实？

## 3. Week01 综合诊断题

> 一个报销问答 Agent 偶尔越权调用工具；用户看不到运行进展；偶尔返回无法解析的结果；团队也无法确认它实际使用的模型和成本。请定位问题，给出最小改进方案，以及每项改进的验证证据。

完成后用下表自评：

| 现象 | 应关联的概念簇 | 最小改进 | 需要的证据 |
| --- | --- | --- | --- |
| 越权工具调用 | D、E | Schema + Tool Runtime 权限 / Sandbox | 非法或未审批输入未执行的测试与 Trace |
| 过程不可见 | F | 类型化 SSE 事件 | 客户端事件序列、TTFT、单一终态测试 |
| 输出不可解析 | D、G | JSON Schema + Pydantic + 有界修复 | 四类无效输出的校验和失败降级用例 |
| 模型与成本不可追踪 | H、I | Gateway Trace / Usage Ledger | requested/resolved model、attempt、Token、Cost、Latency 记录 |

## 4. 学会的判定

完成 Week01 不应以“所有 Demo 都运行过”为标准，而应同时满足：

1. 能用概念簇定位一个 Agent 问题属于上下文、协议、工具执行、流式、模型调用还是治理。
2. 能只增加一项可解释的工程机制来处理该问题。
3. 能区分课程示例、Mock 测试与真实模型调用各自能证明什么。
4. 能用测试、事件或 Trace 给出证据，而不以“感觉输出不错”作为结论。

---

## 课程材料与代码索引

| 内容 | 主要材料 |
| --- | --- |
| 课程地图、最小 Loop、Sandbox、Eval | `course_code/week01/1-1/`；`course_materials/week01/1-1 开营直播.pptx` |
| 模型 API、参数、Adapter、重试 | `course_code/week01/1-2/`；`course_materials/week01/1-2.pdf` |
| Streaming、SSE、取消、重放 | `course_code/week01/1-3/`；`course_materials/week01/1-3.pdf` |
| Prompt 行为契约与模板 | `course_code/week01/1-4/`；`course_materials/week01/1-4.pdf` |
| Structured Output 与修复循环 | `course_code/week01/1-5/`；`course_materials/week01/1-5.pdf` |
| Gateway 原型 | `course_code/week01/1-6/`；`course_materials/week01/1-6.pdf` |
| 可部署 Gateway | `course_code/week01/1-7/llm-gateway/`；`course_materials/week01/1-7.pdf` |

