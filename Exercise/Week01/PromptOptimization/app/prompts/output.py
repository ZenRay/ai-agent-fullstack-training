import json
import re
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.prompts.types import OutputContract

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_REQUIRED = re.compile(r"'([^']+)' is a required property")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    document: str
    failed_field: str | None
    missing: tuple[str, ...]
    code: str
    detail: str


def response_format(contract: OutputContract) -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {"name": "output", "schema": contract.json_schema, "strict": True},
    }


def local_repair(text: str) -> str:
    """去掉围栏和明显的尾逗号。修不好的内容原样留下，交给后面的校验。"""
    stripped = text.strip()
    fenced = _FENCE.search(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    return _TRAILING_COMMA.sub(r"\1", stripped)


def check_output(contract: OutputContract, text: str, sources: list[str]) -> Verdict:
    document = local_repair(text)
    required = _required_fields(contract)
    try:
        parsed = json.loads(document)
    except json.JSONDecodeError:
        return Verdict(False, document, None, required, "schema", "不是合法 JSON")
    if not isinstance(parsed, dict):
        return Verdict(False, document, None, required, "schema", "输出必须是 JSON 对象")
    errors = list(Draft202012Validator(contract.json_schema).iter_errors(parsed))
    if errors:
        return _schema_failure(document, parsed, required, errors[0])
    for rule in contract.rules:
        value = parsed.get(rule.field)
        if not _appears_in_source(value, sources):
            detail = f"{rule.field} 没有出现在工具结果里"
            return Verdict(False, document, rule.field, (rule.field,), "business_rule", detail)
    return Verdict(True, document, None, (), "accepted", "")


def degrade_text(fields: tuple[str, ...] | list[str]) -> str:
    if not fields:
        return "这不是可执行结构。"
    return f"这不是可执行结构。缺少：{'、'.join(fields)}。"


def repair_prompt(verdict: Verdict, bad: str) -> str:
    field = verdict.failed_field or "、".join(verdict.missing) or "输出"
    return (
        f"上一次输出没有通过校验。失败字段：{field}。原因：{verdict.detail}。"
        "只修改失败的字段，返回完整 JSON。不要解释。\n"
        f"{bad}"
    )


def _schema_failure(
    document: str,
    parsed: dict[str, object],
    required: tuple[str, ...],
    error: ValidationError,
) -> Verdict:
    missing = tuple(name for name in required if name not in parsed)
    field = _failed_field(error) or (missing[0] if missing else None)
    if field is not None and field not in missing and error.validator == "required":
        missing = (*missing, field)
    return Verdict(False, document, field, missing, "schema", error.message)


def _required_fields(contract: OutputContract) -> tuple[str, ...]:
    required = contract.json_schema.get("required") or []
    return tuple(name for name in required if isinstance(name, str))


def _failed_field(error: ValidationError) -> str | None:
    for part in error.path:
        if isinstance(part, str):
            return part
    matched = _REQUIRED.search(error.message)
    if matched:
        return matched.group(1)
    return None


def _appears_in_source(value: object, sources: list[str]) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        number = str(int(value)) if isinstance(value, int) or float(value).is_integer() else str(value)
        pattern = re.compile(rf"(?<!\d){re.escape(number)}(?!\d)")
        return any(pattern.search(source) for source in sources)
    text = str(value).strip()
    return bool(text) and any(text in source for source in sources)
