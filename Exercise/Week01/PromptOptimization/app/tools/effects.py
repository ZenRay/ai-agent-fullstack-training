from dataclasses import dataclass


@dataclass
class ReconcileResult:
    found: bool = False
    absent: bool = False
    output: str = ""


@dataclass
class Receipt:
    key: str
    run_id: str
    call_id: str
    name: str
    arguments: str
    status: str = "pending"
    ok: bool | None = None
    output: str | None = None


class EffectLedger:
    """进程内的写操作收据。重启后消失，持久化表留到后面。"""

    def __init__(self) -> None:
        self._rows: dict[str, Receipt] = {}

    def get(self, key: str) -> Receipt | None:
        return self._rows.get(key)

    def claim(
        self,
        key: str,
        *,
        run_id: str,
        call_id: str,
        name: str,
        arguments: str,
    ) -> tuple[Receipt, bool]:
        existing = self._rows.get(key)
        if existing is not None:
            return existing, False
        receipt = Receipt(
            key=key,
            run_id=run_id,
            call_id=call_id,
            name=name,
            arguments=arguments,
        )
        self._rows[key] = receipt
        return receipt, True

    def settle(self, key: str, *, ok: bool, output: str) -> None:
        receipt = self._rows[key]
        receipt.status = "settled"
        receipt.ok = ok
        receipt.output = output

    def mark_unknown(self, key: str, output: str) -> None:
        receipt = self._rows[key]
        receipt.status = "unknown"
        receipt.ok = False
        receipt.output = output

    def find_result(self, run_id: str, name: str, arguments: str) -> Receipt | None:
        found: Receipt | None = None
        for row in self._rows.values():
            if (
                row.run_id == run_id
                and row.name == name
                and row.arguments == arguments
                and row.output is not None
            ):
                found = row
        return found

    def has_unknown(self, run_id: str) -> bool:
        return any(row.run_id == run_id and row.status == "unknown" for row in self._rows.values())

    def has_other_unknown(self, run_id: str, name: str, arguments: str) -> bool:
        return any(
            row.run_id == run_id
            and row.name == name
            and row.status == "unknown"
            and row.arguments != arguments
            for row in self._rows.values()
        )
