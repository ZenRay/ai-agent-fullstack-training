# Prompt 工程化管理

把提示词做成一次运行可追溯的工件：改动能比较，能回退，能从一次失败的 run 追到具体版本。独立的 Prompt 平台、热更新和线上 A/B 先不做。

`execute_run` 现在把线程里的历史消息和 `tools.definitions()` 直接交给模型。没有系统提示，也没有版本号。工具的 `description` 和 `input_schema` 已经在影响模型会不会调工具、参数怎么填，但它们和代码绑在一起。改一行描述就改变行为，运行日志里看不出来。

用量账本的既有约定是只记调用方、模型、Token、耗时和花费，不存 Prompt 和回答正文。见 `docs/01-设计说明.md` 与 `docs/05-模型网关设计.md`。缺的是另一样东西：这次 run 用的是哪一份指令。没有这个标识，后面的优化无法对照，坏了也无法只退提示词。

依据：

- [AWS：生成式 AI 的 Prompt 生命周期](https://docs.aws.amazon.com/prescriptive-guidance/latest/gen-ai-lifecycle-operational-excellence/dev-experimenting-prompt-lifecycle.html)
- [AWS：预生产阶段把 Prompt 收成版本化工件](https://docs.aws.amazon.com/prescriptive-guidance/latest/gen-ai-lifecycle-operational-excellence/preprod-hardening.html)
- [Langfuse：按规模组织、版本化和测试 Prompt](https://langfuse.com/resources/engineering/prompt-management-at-scale)
- [MLflow：评审一次 Prompt 变更](https://mlflow.org/articles/prompt-diffing/)
- [Databricks：比较 Prompt 版本](https://docs.databricks.com/aws/en/mlflow3/genai/prompt-version-mgmt/prompt-registry/evaluate-prompts)
- [Prompt 版本化：何时使用、何时不用](https://github.com/agentpatternscatalog/patterns/blob/main/patterns/prompt-versioning.md)

## 概念

在这个网关里，一份 Prompt 是下面四类内容的整体，不是一段系统消息字符串。

| 工件 | 现在在哪 | 要一起版本化的原因 |
| --- | --- | --- |
| 系统 / 开发者指令模板 | 还没有 | 这是以后优化的主体 |
| 工具的 `description` 和 `input_schema` | `ToolRegistry.definitions()` | 改描述会改变工具调用，效果和改系统提示一样 |
| 消息组装规则 | `store.messages(thread_id)` 原样送出 | 裁历史、工具结果怎么写回，都是提示词的一部分 |
| 绑在这份提示上的模型设置 | 运行上只有模型别名 | 换模型、温度、`tool_choice` 后，同一段文字不再是同一个工件 |

用户当轮输入留在线程消息里，不放进 Prompt 版本。密钥不写进提示词。工具能不能写、超时和幂等仍由注册表决定，提示词文本不能把写操作放宽。注册表见 `docs/07-AgentLoop与工具注册设计.md`。

业界把 Prompt 当成发布物。AWS 要求版本号同时代表这段模板，以及它在某一版评测集上的结果。Langfuse 把每次保存做成不可变版本，`production` 这类标签只是指向某一版的指针。MLflow 把评审分成文字 diff 和行为 diff：同一批输入分别跑旧版和新版。Git 里的文本历史只解决字改了；优化要回答行为有没有变好。

Git 里的固定文件，在「提示词随这次应用一起发布、只有一个写入者」时已经够用。运行时注册表要等回退必须快过一次应用发布，或非开发同学要独立改提示词，再做。

## 它解决什么

- 循环目前没有可命名的提示词。无法优化、比较或单独回退一段还不存在的指令。
- 行为已经散在系统提示（目前缺失）、工具描述和消息组装三处。这三处不一起版本化，改一处就会让一次 run 的行为对不上记录。
- 网关已经有 `run_id`、事件日志、用量 Token 和模型别名。缺的一环是这次运行出自哪一套指令。
- 只给字符串编号、不挂评测，得到的是文本历史。只做评测、评测集本身会变，新旧分数就不能比。

## 设计

### 身份

一个版本在写入后不再改。身份是模板、变量声明、工具契约和模型设置的整体，外加内容哈希。提交说明写成假设，例如「减少重复查天气」，方便以后对上评测结果。就地改第 3 版，会让已经挂在这一版上的分数和 trace 指向一段已经不存在的文字。

### 一次 run 只用一版

同一 `run_id` 里的每一轮模型调用，包括工具结果写回之后的下一轮，都用开跑时选定的那一版。用户下一问可以换版本，那是新的 `run_id`。中途换指令，会让已经发生的工具调用和后续消息对不上。

### 渲染失败发生在流打开之前

变量缺失、模板拼不出来，按现有接口约定返回 HTTP 4xx。流一旦打开，失败只能是 `run.failed`。见 `docs/08-创建运行与取消接口设计.md`。

### 评测集本身要版本化

数据集一变，新旧 Prompt 的分数就不能比。先做小而固定的用例，每条有编号、输入、期望结果，以及它为什么在集合里（正常路径、缺参、工具失败、不该调工具）。对比分三层：

1. 文本 diff。
2. 同一批输入的行为 diff。
3. 契约检查：工具名存在、参数过 schema、该结束时不再调工具。

通过线是：新版本不能弄坏当前版本已经通过的用例。质量分放在契约通过之后。Databricks 的建议是所有版本打同一份数据，并把边界用例放进集合；上线后继续看是否变差。

### 观测和账本分开

每次模型调用记下 prompt 名称、版本和哈希，挂在已有的 `run_id` 上，并和模型网关的 trace 用同一个 id。用量账本继续不存正文。需要复盘原文时，放到单独的调试或评测存储，并定保留期限。

网关已经在记输入、输出和缓存 Token。提示词一改，供应商的前缀缓存往往会失效。Token 和花费作为回归信号，和回答质量一起看。

### 回退是移动指针

线上用的名字指向某一不可变版本。回退是把指针移回上一版，并留下谁在何时因为什么移动了它。这个仓库目前是单进程、提示词随代码发布，用 Git 里的固定文件就能回退。运行时注册表、标签缓存和热更新，要等「回退必须快过一次应用发布」或「非开发同学要独立改提示词」出现后再做。

### 和现有流式约束对齐

已有正文之后不把半截回答拼进下一次提示。要求严格 JSON Schema 的任务保持非流式。带工具失败时，用原始消息重试。见 `docs/01-设计说明.md`、`docs/04-StreamingModel设计.md`、`docs/05-模型网关设计.md`。

结构化输出、修复、失败和降级见 `docs/14-结构化输出与失败路径.md`。规则与变量怎么拆、注入怎么限住后果，见 `docs/15-规则变量分离与注入防护.md`。

## 现在做和以后做

现在做：

- 把上述四类内容收成命名工件。
- 版本不可变，并带假设说明。
- 每次 run 记录名称、版本和哈希。
- 一份版本化的小评测集，加上契约检查和行为对比。
- 通过之后才替换当前使用的版本。

以后再做：

- `staging` / `production` 标签和缓存 TTL。
- 按流量灰度。
- 给非开发同学用的编辑界面。
- 大规模的 LLM 评审。
- 和应用程序发布分开的上线通道。
