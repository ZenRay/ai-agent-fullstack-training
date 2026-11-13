#!/usr/bin/env python3
"""course_note.py —— course-note skill 的校验与索引脚本（仅 python3 标准库，自成一体）

不依赖任何全局 skill。无论从哪个目录运行，路径都以项目根目录为基准
（项目根 = 本脚本向上三级：scripts → course-note → skills → .claude → 项目根）。

  python3 .claude/skills/course-note/scripts/course_note.py check <笔记.md> [--strict]
      检查：
        - frontmatter 必填字段、时间格式、tags 数量、status
        - 代码围栏是否配对
        - 三部分骨架（学习材料 / 疑问 / 自检）是否齐全
        - 「文件名:行号」引用是否落在源文件的真实行数内
        - 「| 文件名 | 行数 |」表格里的行数是否和源文件一致
        - 正文里的 A1.3 / Q4 / C2 这类章节引用是否都有对应标题
        - 笔记里有没有混入 secretFiles（默认 .env）里的值

  python3 .claude/skills/course-note/scripts/course_note.py index [--check]
      扫描 storagePath 下的笔记，按目录（如 Week01）分组生成 index.md，
      保留 <!-- manual-start --> 与 <!-- manual-end --> 之间的手写内容。
      --check 只比对、不写入，索引过期时退出码为 1。

退出码: 0 = 通过（可含警告）; 1 = 有错误（或 --strict 下有警告）; 2 = 用法或配置错误
"""
import argparse
import json
import os
import re
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.abspath(os.path.join(SKILL_DIR, "..", "..", ".."))

DEFAULTS = {
    "codeRoot": "course_code",
    "weekDirTemplate": "course_code/week{week}",
    "lessonDirTemplate": "course_code/week{week}/{lesson}",
    "secretFiles": [".env"],
    "requiredParts": ["学习材料", "疑问", "自检"],
    "requiredFields": ["title", "create_time", "update_time", "category", "tags", "project-description"],
    "statusOptions": ["active", "outdated", "superseded", "archived"],
}
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
SRC_EXT = r"(?:py|json|ts|tsx|js|jsx|md|toml|txt|sh|yaml|yml|ini|cfg)"
LINE_REF_RE = re.compile(r"([A-Za-z0-9_\-./]+\.%s):(\d+)(?:-(\d+))?" % SRC_EXT)
COUNT_ROW_RE = re.compile(r"^\|\s*`?([A-Za-z0-9_\-./]+\.%s)`?\s*\|\s*(\d+)\s*\|" % SRC_EXT)
SECTION_REF_RE = re.compile(r"(?<![A-Za-z0-9_.])([ACQ]\d+(?:\.\d+)?)(?![A-Za-z0-9_])")
SECTION_HEAD_RE = re.compile(r"^#{1,4}\s+([ACQ]\d+(?:\.\d+)?)(?![A-Za-z0-9_])")
SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9]{20,}")


def die(msg):
    print(msg)
    sys.exit(2)


def load_cfg():
    os.chdir(PROJECT_ROOT)
    path = os.path.join(SKILL_DIR, "config.json")
    cfg = dict(DEFAULTS)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    storage = cfg.get("storagePath")
    if not storage:
        die("config.json 里没有 storagePath。请先向用户确认笔记总目录，再写入 %s。" % path)
    storage = os.path.abspath(storage)
    if not os.path.isdir(storage):
        die("笔记总目录不存在: %s" % storage)
    return cfg, storage


def parse_frontmatter(text):
    """最小 YAML frontmatter 解析（扁平 key: value 与 [list]，不支持嵌套）。"""
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).split("\n"):
        if not line.strip() or line.strip().startswith("#"):
            continue
        km = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not km:
            continue
        k, v = km.group(1), km.group(2).strip()
        if v.startswith("[") and v.endswith("]"):
            fm[k] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
        else:
            if not (v.startswith('"') or v.startswith("'")):
                v = re.split(r"\s+#", v)[0].strip()
            fm[k] = v.strip("\"'")
    return fm


# ---------------------------------------------------------------- check

def check_frontmatter(rel, text, cfg):
    errors, warns = [], []
    fm = parse_frontmatter(text)
    if fm is None:
        return [rel + ": frontmatter 缺失或无法解析"], []
    for k in cfg["requiredFields"]:
        if k not in fm or fm[k] in ("", []):
            errors.append("%s: 必填字段缺失或为空: %s" % (rel, k))
    for k in ("create_time", "update_time"):
        if k in fm and not TIME_RE.match(str(fm[k])):
            errors.append("%s: %s 格式应为 YYYY-MM-DD HH:mm:ss（当前: %s）" % (rel, k, fm[k]))
    if "create_time" in fm and "update_time" in fm and TIME_RE.match(str(fm["create_time"])) \
            and TIME_RE.match(str(fm["update_time"])) and fm["update_time"] < fm["create_time"]:
        errors.append("%s: update_time 早于 create_time" % rel)
    tags = fm.get("tags")
    if isinstance(tags, list) and not (3 <= len(tags) <= 7):
        warns.append("%s: tags 建议 3-7 个（当前 %d）" % (rel, len(tags)))
    status = fm.get("status", "active")
    if status not in cfg["statusOptions"]:
        errors.append("%s: status \"%s\" 不在 %s" % (rel, status, cfg["statusOptions"]))
    return errors, warns


def split_note(text):
    """返回 (正文行列表[(行号, 文本, 是否在代码围栏内)], 围栏是否配对)。frontmatter 被跳过。"""
    lines = text.split("\n")
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    out, in_fence = [], False
    for i in range(start, len(lines)):
        line = lines[i]
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            out.append((i + 1, line, True))
        else:
            out.append((i + 1, line, in_fence))
    return out, not in_fence


def infer_scope(note_path, cfg):
    """返回用来查找源文件的目录：优先「一讲」目录，其次「一周」目录，最后整个 codeRoot。"""
    name = os.path.basename(note_path)
    m = re.match(r"^(\d+)-(\d+)", name)
    if m:
        d = os.path.abspath(cfg["lessonDirTemplate"].format(week="%02d" % int(m.group(1)), lesson="%s-%s" % (m.group(1), m.group(2))))
        if os.path.isdir(d):
            return d, "lesson"
    m = re.match(r"^week(\d+)$", os.path.basename(os.path.dirname(note_path)), re.I)
    if m:
        d = os.path.abspath(cfg["weekDirTemplate"].format(week="%02d" % int(m.group(1))))
        if os.path.isdir(d):
            return d, "week"
    return os.path.abspath(cfg["codeRoot"]), "all"


def find_unique(root, name):
    hits = []
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        if name in files:
            hits.append(os.path.join(r, name))
    return hits[0] if len(hits) == 1 else None


def make_resolver(scope, cfg):
    cache = {}

    def resolve(ref):
        if ref not in cache:
            found = None
            if "/" in ref:
                p = os.path.abspath(ref)
                found = p if os.path.isfile(p) else None
            else:
                found = find_unique(scope, ref) if os.path.isdir(scope) else None
                if not found:
                    found = find_unique(os.path.abspath(cfg["codeRoot"]), ref)
            cache[ref] = found
        return cache[ref]

    return resolve


def count_lines(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        data = f.read()
    n = data.count("\n")
    if data and not data.endswith("\n"):
        n += 1
    return n


def load_secret_values(cfg):
    secrets = []
    for name in cfg.get("secretFiles", []):
        if not os.path.isfile(name):
            continue
        with open(name, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, value = line.partition("=")
                value = value.strip().strip("\"'")
                if len(value) >= 8:
                    secrets.append((name, key.strip(), value))
    return secrets


def cmd_check(args):
    cfg, storage = load_cfg()
    note = os.path.abspath(args.note)
    if not os.path.isfile(note):
        die("笔记不存在: %s" % args.note)
    rel = os.path.relpath(note, storage)
    errors, warns = [], []

    if not note.startswith(storage + os.sep):
        warns.append("%s: 笔记不在 storagePath（%s）下，不会进入索引" % (rel, os.path.relpath(storage)))

    with open(note, encoding="utf-8") as f:
        text = f.read()

    e, w = check_frontmatter(rel, text, cfg)
    errors += e
    warns += w

    lines, balanced = split_note(text)
    if not balanced:
        errors.append("%s: 代码围栏（```）数量为奇数，有一个没有闭合" % rel)

    h1 = [t for _, t, fenced in lines if not fenced and t.startswith("# ")]
    for part in cfg["requiredParts"]:
        if not any(part in h for h in h1):
            errors.append("%s: 缺少一级标题「%s」（三部分骨架：%s）" % (rel, part, " / ".join(cfg["requiredParts"])))

    scope, kind = infer_scope(note, cfg)
    if kind == "all":
        warns.append("%s: 无法从文件名（N-M 开头）或所在目录（WeekNN）推断源码范围，将在整个 %s 下按文件名查找" % (rel, cfg["codeRoot"]))
    resolve = make_resolver(scope, cfg)
    unresolved, checked_refs = set(), 0
    for no, text_line, fenced in lines:
        if fenced:
            continue
        for m in LINE_REF_RE.finditer(text_line):
            name, a = m.group(1), int(m.group(2))
            b = int(m.group(3)) if m.group(3) else a
            path = resolve(name)
            if not path:
                unresolved.add(name)
                continue
            total = count_lines(path)
            checked_refs += 1
            if a < 1 or b < a or b > total:
                shown = m.group(2) + ("-" + m.group(3) if m.group(3) else "")
                errors.append("%s:%d: 引用 %s:%s 超出源文件范围（共 %d 行）" % (rel, no, name, shown, total))
        mc = COUNT_ROW_RE.match(text_line.strip())
        if mc:
            path = resolve(mc.group(1))
            if path:
                claimed, total = int(mc.group(2)), count_lines(path)
                # 编辑器通常把末尾换行后的空行也算一行，因此允许多 1
                if claimed not in (total, total + 1):
                    errors.append("%s:%d: 表格写 %s 共 %d 行，源文件实际为 %d 行" % (rel, no, mc.group(1), claimed, total))
    for name in sorted(unresolved):
        warns.append("%s: 引用了 %s，但找不到唯一匹配的源文件，已跳过行号校验" % (rel, name))

    defined = set()
    for _, t, fenced in lines:
        if not fenced:
            m = SECTION_HEAD_RE.match(t)
            if m:
                defined.add(m.group(1))
    for no, text_line, fenced in lines:
        if fenced or SECTION_HEAD_RE.match(text_line):
            continue
        for m in SECTION_REF_RE.finditer(text_line):
            if m.group(1) not in defined:
                errors.append("%s:%d: 引用了不存在的章节 %s" % (rel, no, m.group(1)))

    if SECRET_RE.search(text):
        errors.append("%s: 正文里出现了形如 sk-... 的密钥" % rel)
    for fname, key, value in load_secret_values(cfg):
        if value in text:
            errors.append("%s: 正文里含有 %s 中 %s 的值（已隐去）" % (rel, fname, key))

    mark = "❌" if errors else ("⚠️ " if warns else "✅")
    print("%s %s" % (mark, rel))
    for x in errors:
        print("   ERROR: %s" % x)
    for x in warns:
        print("   WARN : %s" % x)
    print("\n已核对 %d 处「文件名:行号」引用，%d 个章节标题。" % (checked_refs, len(defined)))
    print("汇总: %d 错误 / %d 警告" % (len(errors), len(warns)))
    sys.exit(1 if errors or (args.strict and warns) else 0)


# ---------------------------------------------------------------- index

def natural_key(s):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)]


def collect_notes(storage):
    notes = []
    for root, dirs, files in os.walk(storage):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        for f in sorted(files):
            if f.endswith(".md") and f != "index.md":
                notes.append(os.path.join(root, f))
    return notes


def build_index(storage, old_text):
    groups, archived, notes = {}, [], collect_notes(storage)
    for n in notes:
        with open(n, encoding="utf-8") as f:
            fm = parse_frontmatter(f.read()) or {}
        rel = os.path.relpath(n, storage).replace(os.sep, "/")
        title = fm.get("title") or os.path.splitext(os.path.basename(n))[0]
        status = fm.get("status", "active")
        tail = " · ".join(x for x in [str(fm.get("create_time", ""))[:10], fm.get("purpose", "")] if x)
        entry = "- [%s](%s)%s" % (title, rel, (" · " + tail) if tail else "")
        top = rel.split("/")[0] if "/" in rel else "其他"
        if status != "active":
            archived.append((rel, "%s · %s" % (entry, status)))
        else:
            groups.setdefault(top, []).append((rel, entry))

    out = ["# 学习笔记索引", ""]
    for top in sorted(groups, key=natural_key):
        out += ["## %s" % top, ""]
        for _, entry in sorted(groups[top], key=lambda x: natural_key(os.path.basename(x[0]))):
            out.append(entry)
        out.append("")
    if archived:
        out += ["## 已归档 / 已取代", ""]
        out += [entry for _, entry in sorted(archived)]
        out.append("")
    manual = re.search(r"<!-- manual-start -->.*?<!-- manual-end -->", old_text or "", re.S)
    out.append(manual.group(0) if manual else "<!-- manual-start -->\n<!-- manual-end -->")
    return "\n".join(out) + "\n", notes


def cmd_index(args):
    cfg, storage = load_cfg()
    idx_path = os.path.join(storage, "index.md")
    old = ""
    if os.path.isfile(idx_path):
        with open(idx_path, encoding="utf-8") as f:
            old = f.read()
    new, notes = build_index(storage, old)
    rel_idx = os.path.relpath(idx_path)
    if args.check:
        if new != old:
            print("❌ %s 已过期，请运行 index 子命令重新生成" % rel_idx)
            sys.exit(1)
        print("✅ %s 是最新的（%d 篇笔记）" % (rel_idx, len(notes)))
    elif new == old:
        print("✅ %s 无需更新（%d 篇笔记）" % (rel_idx, len(notes)))
    else:
        with open(idx_path, "w", encoding="utf-8") as f:
            f.write(new)
        print("✅ 已更新 %s（%d 篇笔记）" % (rel_idx, len(notes)))
    bad = [l for l in re.findall(r"\]\(([^)]+?\.md)\)", new) if not os.path.exists(os.path.join(storage, l))]
    for l in bad:
        print("   ERROR: 索引链接无效: %s" % l)
    sys.exit(1 if bad else 0)


def main():
    parser = argparse.ArgumentParser(description="course-note skill 的校验与索引脚本")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="校验单篇课程笔记")
    p.add_argument("note")
    p.add_argument("--strict", action="store_true", help="警告也视为失败")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("index", help="生成或检查 index.md")
    p.add_argument("--check", action="store_true", help="只检查是否过期，不写入")
    p.set_defaults(func=cmd_index)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
