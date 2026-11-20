from dataclasses import dataclass
from pathlib import Path

from app.prompts.catalog import Catalog
from app.prompts.evaluate import EvalReport
from app.prompts.types import PromptVersion


@dataclass(frozen=True)
class FormatGap:
    worth_replacing: bool
    reason: str


def publish_candidate(catalog: Catalog, version: PromptVersion) -> Path:
    """只把胜出的候选写成不可变文件。替换当前指针仍要走评测后的 promote。"""
    return catalog.publish(version)


def format_gap(current: EvalReport, candidate: EvalReport, holdout_ids: set[str]) -> FormatGap:
    """保留集没有变好时，不把这一版当成值得替换的候选。这里不移动指针。"""
    if not holdout_ids:
        return FormatGap(False, "没有保留集，不值得替换当前版本")
    before = {item.case_id: item for item in current.results}
    after = {item.case_id: item for item in candidate.results}
    improved = False
    for case_id in sorted(holdout_ids):
        previous = before.get(case_id)
        nxt = after.get(case_id)
        previous_ok = previous is not None and previous.expectation_met and previous.contract_ok
        next_ok = nxt is not None and nxt.expectation_met and nxt.contract_ok
        if previous_ok and not next_ok:
            return FormatGap(False, "保留集变差，不值得替换当前版本")
        if next_ok and not previous_ok:
            improved = True
    if not improved:
        return FormatGap(False, "保留集没有变好，不值得替换当前版本")
    return FormatGap(True, "保留集上的格式结果变好，仍要经评审后才替换")
