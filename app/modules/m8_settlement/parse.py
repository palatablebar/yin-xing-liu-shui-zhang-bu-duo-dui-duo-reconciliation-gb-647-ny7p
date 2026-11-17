"""结算输入解析：往来未清凭证与到账清单。

复用采信读取（编码链、分段、表头识别）与契约归一（金额、日期）。
与对账采信不同：结算直接动钱，任何一行不合格都整批拒绝并指明行号，
避免带病数据进入冲销。
"""
from __future__ import annotations

from decimal import Decimal

from ... import reasons
from ...errors import DomainError
from ..m1_ingest.reader import read_statement
from ..m2_contract import coerce

# 列别名：归一化小写表头 -> 规范列名
_COMMON = {
    "往来单位": "counterparty", "往来户": "counterparty", "对方单位": "counterparty",
    "客户": "counterparty", "counterparty": "counterparty",
    "备注": "memo", "memo": "memo",
}
VOUCHER_ALIASES = {
    "凭证号": "voucher_no", "凭证编号": "voucher_no", "单号": "voucher_no",
    "voucher_no": "voucher_no", "voucher": "voucher_no",
    "类型": "kind", "业务类型": "kind", "性质": "kind", "kind": "kind",
    "金额": "amount", "凭证金额": "amount", "amount": "amount",
    "到期日": "due_date", "到期日期": "due_date", "约定付款日": "due_date",
    "due_date": "due_date",
    "争议": "disputed", "是否争议": "disputed", "disputed": "disputed",
    **_COMMON,
}
RECEIPT_ALIASES = {
    "到账号": "receipt_no", "到账编号": "receipt_no", "汇款编号": "receipt_no",
    "流水号": "receipt_no", "receipt_no": "receipt_no", "receipt": "receipt_no",
    "金额": "amount", "到账金额": "amount", "amount": "amount",
    "到账日": "received_at", "到账日期": "received_at", "收款日": "received_at",
    "日期": "received_at", "received_at": "received_at",
    **_COMMON,
}

# 必填列 -> 中文名（报错用）
VOUCHER_REQUIRED = {
    "voucher_no": "凭证号", "counterparty": "往来单位", "kind": "类型",
    "amount": "金额", "due_date": "到期日",
}
RECEIPT_REQUIRED = {
    "receipt_no": "到账号", "counterparty": "往来单位",
    "amount": "金额", "received_at": "到账日",
}

KIND_ALIASES = {
    "应收": "receivable", "应收货款": "receivable", "receivable": "receivable",
    "ar": "receivable",
    "退款": "refund", "应付退款": "refund", "应付": "refund",
    "refund": "refund", "ap": "refund",
}
DISPUTED_TRUE = {"是", "y", "yes", "1", "true", "争议", "有争议"}
DISPUTED_FALSE = {"", "否", "n", "no", "0", "false", "无"}


def _read(settings, raw: bytes, aliases: dict, required: dict, side_label: str) -> list[dict]:
    outcome = read_statement(raw, aliases, settings.encodings)
    if outcome["state"]:
        raise DomainError(
            "E_SETTLE_FILE",
            f"{side_label}文件不可用：{reasons.label(outcome['state'])}",
            400,
        )
    missing = [label for key, label in required.items() if key not in outcome["header_map"]]
    if missing:
        raise DomainError(
            "E_SETTLE_HEADER", f"{side_label}缺少必需列：{'、'.join(missing)}", 400
        )
    return outcome["records"]


def _ref(cells: dict, key: str, label: str, row_no: int) -> str:
    ref = coerce.normalize_text(cells.get(key, ""))
    if not ref:
        raise DomainError("E_SETTLE_ROW", f"第 {row_no} 行{label}为空", 400)
    return ref


def _counterparty(cells: dict, row_no: int) -> str:
    name = coerce.normalize_text(cells.get("counterparty", ""))
    if not name:
        raise DomainError("E_SETTLE_ROW", f"第 {row_no} 行往来单位为空", 400)
    return name


def _amount(cells: dict, row_no: int, sentinels) -> Decimal:
    raw = cells.get("amount", "")
    try:
        value = coerce.parse_amount(raw, sentinels)
    except ValueError as exc:
        raise DomainError("E_SETTLE_ROW", f"第 {row_no} 行金额无法解析：{raw!r}", 400) from exc
    if value is None:
        raise DomainError("E_SETTLE_ROW", f"第 {row_no} 行金额为空值", 400)
    if value <= 0:
        raise DomainError("E_SETTLE_ROW", f"第 {row_no} 行金额必须大于 0：{raw!r}", 400)
    return value


def _day(cells: dict, key: str, label: str, row_no: int, tz_name: str) -> str:
    raw = cells.get(key, "")
    try:
        instant = coerce.parse_date(raw, tz_name)
    except ValueError as exc:
        raise DomainError(
            "E_SETTLE_ROW", f"第 {row_no} 行{label}无法解析：{raw!r}", 400
        ) from exc
    return instant.astimezone(coerce.tzinfo(tz_name)).date().isoformat()


def _check_dup(seen: dict, ref: str, row_no: int, label: str) -> None:
    if ref in seen:
        raise DomainError(
            "E_SETTLE_DUP",
            f"{label}重复：{ref}（第 {seen[ref]} 行与第 {row_no} 行）",
            400,
        )
    seen[ref] = row_no


def parse_vouchers(settings, raw: bytes) -> list[dict]:
    """往来未清凭证：应收货款与应付退款，金额一律为正。"""
    records = _read(settings, raw, VOUCHER_ALIASES, VOUCHER_REQUIRED, "往来凭证")
    seen: dict[str, int] = {}
    out = []
    for seq, record in enumerate(records):
        cells, row_no = record["cells"], record["row_no"]
        ref = _ref(cells, "voucher_no", "凭证号", row_no)
        _check_dup(seen, ref, row_no, "凭证号")
        kind_token = coerce.normalize_text(cells.get("kind", "")).lower()
        kind = KIND_ALIASES.get(kind_token)
        if not kind:
            raise DomainError(
                "E_SETTLE_ROW",
                f"第 {row_no} 行类型只能是应收或退款：{cells.get('kind', '')!r}",
                400,
            )
        disputed_token = coerce.normalize_text(cells.get("disputed", "")).lower()
        if disputed_token in DISPUTED_TRUE:
            disputed = True
        elif disputed_token in DISPUTED_FALSE:
            disputed = False
        else:
            raise DomainError(
                "E_SETTLE_ROW",
                f"第 {row_no} 行争议标记无法识别：{cells.get('disputed', '')!r}",
                400,
            )
        out.append({
            "ref": ref,
            "counterparty": _counterparty(cells, row_no),
            "kind": kind,
            "amount": _amount(cells, row_no, settings.sentinels),
            "due_date": _day(cells, "due_date", "到期日", row_no, settings.tz_name),
            "disputed": disputed,
            "memo": coerce.normalize_text(cells.get("memo", "")),
            "seq": seq,
        })
    return out


def parse_receipts(settings, raw: bytes) -> list[dict]:
    """到账清单：一行一笔净额到账。"""
    records = _read(settings, raw, RECEIPT_ALIASES, RECEIPT_REQUIRED, "到账清单")
    seen: dict[str, int] = {}
    out = []
    for seq, record in enumerate(records):
        cells, row_no = record["cells"], record["row_no"]
        ref = _ref(cells, "receipt_no", "到账号", row_no)
        _check_dup(seen, ref, row_no, "到账号")
        out.append({
            "ref": ref,
            "counterparty": _counterparty(cells, row_no),
            "amount": _amount(cells, row_no, settings.sentinels),
            "received_at": _day(cells, "received_at", "到账日", row_no, settings.tz_name),
            "memo": coerce.normalize_text(cells.get("memo", "")),
            "seq": seq,
        })
    return out
