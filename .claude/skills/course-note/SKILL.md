---
name: course-note
description: 为本仓库的课程（course_code/weekNN/N-M 与 course_materials）生成或更新学习笔记，并维护笔记索引。用户说"整理 1-2""写这一讲的笔记""更新 LearningNote""把刚才的问答沉淀进笔记""重新整理笔记格式"时使用。
---

# 课程学习笔记（项目内 skill）

本 skill 只属于本仓库，自带脚本与配置，**不修改、也不依赖**用户全局的 `capturing-learning-notes`。frontmatter 字段与它兼容，将来需要合并笔记时不会冲突。

## 文件

| 文件 | 作用 |
| --- | --- |
| `config.json` | 笔记总目录 `storagePath`、各周的保存形式 `layouts`、源码目录模板等 |
| `templates/course-note.md` | 三部分骨架模板，含写作约定 |
| `scripts/course_note.py` | `check` 校验单篇笔记，`index` 生成或检查索引 |

## 流程

### 1. 确认总目录（只在首次）

读 `config.json` 的 `storagePath`。有值就直接用，不再询问；为空时先问用户，确认后写入 `config.json`。

### 2. 确认保存形式（每次开始新的一周或用户想换时）

**保存形式由用户决定，不要自己假定。** 先看 `config.json` 的 `layouts`：

- 该周已有记录，并且用户这次没有提出变化 → 沿用，并在回复里说明用了哪种形式。
- 该周没有记录，或用户想换 → 用 AskQuestion 让用户选：

| 形式 | 路径 | 适合 |
| --- | --- | --- |
| 一周一个文件 `per-week` | `{storagePath}/WeekNN/LearningNote.md` | 一周内容少，想整体通读 |
| 一讲一个文件 `per-lesson` | `{storagePath}/WeekNN/N-M-{主题}.md` | 每讲内容多，想单独引用 |
| 其他 | 用户指定 | |

选定后把结果写进 `config.json` 的 `layouts`（如 `"Week02": "per-lesson"`）。

### 3. 读材料

- 代码：`course_code/weekNN/N-M/`
- 课件：`course_materials/weekNN/`（pptx、pdf）
- 引用到的相邻周代码，只用来核对，不单独成章

### 4. 按模板写

骨架见 `templates/course-note.md`：**学习材料 / 疑问与解答 / 自检与作业**三部分。

- 解释放在第二部分，第一部分只放课件和代码里的事实与引用。
- 代码引用写成 `文件名:起-止`；文件地图表写 ``| `文件名` | 行数 |``。
- 章节用 A / Q / C 编号，正文引用写「见 Q4」，对应标题必须带同样编号。
- 外部结论标 ✅ 已核查 / ⚠️ 未验证，附来源链接与核查日期。
- 用户自己写的回答原样保留，再附核查，不要改写。
- 笔记里不得出现 `.env` 的任何值。

### 5. 校验与索引（保存后必须执行）

在任意目录运行均可：

```bash
python3 .claude/skills/course-note/scripts/course_note.py check privateNote/Week01/LearningNote.md
python3 .claude/skills/course-note/scripts/course_note.py index
```

`check` 有 ERROR 就先修正，复验通过后才算完成。`index` 只收笔记 `.md`，不收代码和练习目录，按 `WeekNN` 分组；`index --check` 只检查是否过期。

## 脚本不能替代的核对

- 行号在范围内，不代表摘录的代码和那几行内容一致，需要对照源文件。
- 外部结论是否准确，需要查来源。
- 课件原文的引用是否准确，需要对照课件。

## 常见错误

| 错误 | 正确做法 |
| --- | --- |
| 没问用户就决定一讲一文件还是一周一文件 | 按第 2 步确认 |
| 只写 `:99-126` 这样的裸行号 | 写成 `agent_loop_demo.py:99-126` |
| 在文件地图里填错行数 | 以 `wc -l` 为准，`check` 会报 |
| 把解释写进第一部分 | 解释放第二部分，第一部分只放事实 |
| 把 `.env` 里的 key 写进笔记 | 永远不写；`check` 会拦 |
