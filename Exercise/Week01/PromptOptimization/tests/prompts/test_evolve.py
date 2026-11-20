from app.prompts.catalog import Catalog, build_version
from app.prompts.evaluate import CaseResult, EvalReport, Outcome
from app.prompts.evolve import format_gap, publish_candidate
from app.prompts.types import ModelSettings


def _version(number: int):
    return build_version(
        name="assistant",
        version=number,
        hypothesis=f"第 {number} 版假设",
        template="只回答天气。",
        model=ModelSettings(alias="chat-default", tool_choice="auto"),
    )


def _report(*pairs: tuple[str, bool]) -> EvalReport:
    stopped = Outcome(tool_calls=(), follow_calls=(), stopped=True)
    return EvalReport(
        results=tuple(
            CaseResult(
                case_id=case_id,
                contract_ok=ok,
                errors=(),
                expectation_met=ok,
                outcome=stopped,
            )
            for case_id, ok in pairs
        ),
        quality=None,
    )


def test_publish_candidate_does_not_move_the_pointer(tmp_path) -> None:
    catalog = Catalog(tmp_path)
    catalog.publish(_version(1))
    catalog.write_current("assistant", 1)
    publish_candidate(catalog, _version(2))
    assert catalog.current().version == 1
    assert catalog.load("assistant", 2).version == 2


def test_format_gap_keeps_the_current_version_until_holdout_improves() -> None:
    current = _report(("hold", False), ("seen", True))
    same = _report(("hold", False), ("seen", True))
    better = _report(("hold", True), ("seen", True))
    worse = _report(("hold", False), ("seen", False))
    assert format_gap(current, same, set()).worth_replacing is False
    unchanged = format_gap(current, same, {"hold"})
    assert unchanged.worth_replacing is False
    assert "没有变好" in unchanged.reason
    improved = format_gap(current, better, {"hold"})
    assert improved.worth_replacing is True
    assert "评审" in improved.reason
    regressed = format_gap(current, worse, {"seen"})
    assert regressed.worth_replacing is False
    assert "变差" in regressed.reason
