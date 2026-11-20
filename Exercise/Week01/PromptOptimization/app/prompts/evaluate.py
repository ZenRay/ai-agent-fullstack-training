import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.prompts.assemble import assemble, tool_definitions
from app.prompts.catalog import Catalog
from app.prompts.hashing import identity_dict
from app.prompts.render import render
from app.prompts.types import EvalCase, EvalSet, ModelSettings, PromptVersion, ToolResultFixture
from app.prompts.untrusted import mark_untrusted
from app.tools.execute import canonical_arguments, parse_and_validate
from app.tools.registry import ToolRegistry

Completion = Callable[
    [list[dict[str, Any]], list[dict[str, Any]], ModelSettings],
    Awaitable["Turn"],
]


@dataclass(frozen=True)
class ToolUse:
    name: str
    arguments: str


@dataclass(frozen=True)
class Turn:
    tool_calls: tuple[ToolUse, ...] = ()


@dataclass(frozen=True)
class Outcome:
    tool_calls: tuple[ToolUse, ...]
    follow_calls: tuple[ToolUse, ...]
    stopped: bool


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    contract_ok: bool
    errors: tuple[str, ...]
    expectation_met: bool
    outcome: Outcome


@dataclass(frozen=True)
class EvalReport:
    results: tuple[CaseResult, ...]
    quality: float | None


@dataclass(frozen=True)
class BehaviorChange:
    case_id: str
    before: str
    after: str


async def evaluate(
    version: PromptVersion,
    eval_set: EvalSet,
    completion: Completion,
    registry: ToolRegistry,
) -> EvalReport:
    version_errors = [
        f"未注册的工具: {tool.name}" for tool in version.tools if registry.find(tool.name) is None
    ]
    results: list[CaseResult] = []
    for case in eval_set.cases:
        results.append(await _run_case(version, case, completion, registry, version_errors))
    quality = None
    if results and all(item.contract_ok for item in results):
        quality = sum(item.expectation_met for item in results) / len(results)
    return EvalReport(results=tuple(results), quality=quality)


def diff_text(left: PromptVersion, right: PromptVersion) -> list[str]:
    left_fields = identity_dict(left)
    right_fields = identity_dict(right)
    return [key for key in left_fields if left_fields[key] != right_fields[key]]


def diff_behavior(left: EvalReport, right: EvalReport) -> list[BehaviorChange]:
    after = {item.case_id: item for item in right.results}
    changes: list[BehaviorChange] = []
    for item in left.results:
        other = after.get(item.case_id)
        if other is None:
            changes.append(BehaviorChange(item.case_id, _signature(item.outcome), ""))
            continue
        if _signature(item.outcome) != _signature(other.outcome):
            changes.append(
                BehaviorChange(item.case_id, _signature(item.outcome), _signature(other.outcome))
            )
    return changes


def can_promote(current: EvalReport, candidate: EvalReport) -> bool:
    if any(not item.contract_ok for item in candidate.results):
        return False
    passed = {item.case_id: item for item in current.results}
    for item in candidate.results:
        previous = passed.get(item.case_id)
        if previous is not None and previous.expectation_met and not item.expectation_met:
            return False
    return True


async def promote(
    catalog: Catalog,
    name: str,
    version: int,
    eval_set: EvalSet,
    completion: Completion,
    registry: ToolRegistry,
) -> bool:
    """候选版本先发布成文件。契约通过且不弄坏当前已通过的用例之后，才移动指针。"""
    candidate = catalog.load(name, version)
    current = catalog.current()
    current_report = await evaluate(current, eval_set, completion, registry)
    candidate_report = await evaluate(candidate, eval_set, completion, registry)
    if not can_promote(current_report, candidate_report):
        return False
    catalog.write_current(name, version)
    return True


async def _run_case(
    version: PromptVersion,
    case: EvalCase,
    completion: Completion,
    registry: ToolRegistry,
    version_errors: list[str],
) -> CaseResult:
    system = render(version, case.variables)
    messages = assemble(system, [{"role": "user", "content": case.input}])
    tools = tool_definitions(version)
    first = await completion(messages, tools, version.model)
    follow: tuple[ToolUse, ...] = ()
    if case.tool_result is not None and first.tool_calls:
        follow_messages = _with_result(messages, first.tool_calls[0], case.tool_result)
        follow_turn = await completion(follow_messages, tools, version.model)
        follow = follow_turn.tool_calls
        stopped = not follow
    else:
        stopped = not first.tool_calls
    outcome = Outcome(tool_calls=first.tool_calls, follow_calls=follow, stopped=stopped)
    errors = list(version_errors)
    errors.extend(_call_errors(version, registry, first.tool_calls))
    errors.extend(_call_errors(version, registry, follow))
    if _should_stop(case) and _kept_calling(case, first.tool_calls, follow):
        errors.append("该结束时不再调工具")
    expectation_met = _expectation_met(case, first.tool_calls, follow)
    return CaseResult(
        case_id=case.id,
        contract_ok=not errors,
        errors=tuple(errors),
        expectation_met=expectation_met,
        outcome=outcome,
    )


def _should_stop(case: EvalCase) -> bool:
    return case.expect.no_tool or case.expect.then_stop


def _kept_calling(
    case: EvalCase,
    first: tuple[ToolUse, ...],
    follow: tuple[ToolUse, ...],
) -> bool:
    if case.expect.no_tool:
        return bool(first)
    if case.expect.then_stop:
        return bool(follow)
    return False


def _expectation_met(
    case: EvalCase,
    first: tuple[ToolUse, ...],
    follow: tuple[ToolUse, ...],
) -> bool:
    expect = case.expect
    if expect.no_tool:
        return not first
    if len(first) != 1 or first[0].name != expect.tool:
        return False
    expected = json.dumps(expect.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if canonical_arguments(first[0].arguments) != expected:
        return False
    if expect.then_stop and follow:
        return False
    return True


def _call_errors(
    version: PromptVersion,
    registry: ToolRegistry,
    calls: tuple[ToolUse, ...],
) -> list[str]:
    contracts = {tool.name: tool.input_schema for tool in version.tools}
    errors: list[str] = []
    for call in calls:
        if call.name not in contracts:
            errors.append(f"工具不在这一版契约里: {call.name}")
        registered = registry.find(call.name)
        if registered is None:
            errors.append(f"未注册的工具: {call.name}")
            continue
        if call.name in contracts and not _accepts(contracts[call.name], call.arguments):
            errors.append(f"参数不符合这一版 schema: {call.name}")
        if not _accepts(registered.input_schema, call.arguments):
            errors.append(f"参数不符合注册表 schema: {call.name}")
    return errors


def _accepts(schema: dict[str, Any], arguments: str) -> bool:
    parsed, error = parse_and_validate(schema, arguments)
    return error is None and parsed is not None


def _with_result(
    messages: list[dict[str, Any]],
    call: ToolUse,
    result: ToolResultFixture,
) -> list[dict[str, Any]]:
    return [
        *messages,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
            ],
        },
        {"role": "tool", "content": mark_untrusted("tool", result.output), "tool_call_id": "c1"},
    ]


def _signature(outcome: Outcome) -> str:
    def join(calls: tuple[ToolUse, ...]) -> str:
        return ",".join(f"{call.name}:{call.arguments}" for call in calls)

    return f"tools={join(outcome.tool_calls)};follow={join(outcome.follow_calls)};stopped={outcome.stopped}"
