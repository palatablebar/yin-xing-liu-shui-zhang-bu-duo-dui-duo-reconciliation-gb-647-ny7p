"""净额结算核心：互欠先抵、现金补差、余款挂账。

规则（全部确定性，输入相同则结果相同）：
- 合格性：争议凭证、到期日晚于到账日的款项不参与冲销，进排除清单并带原因码；
- 到账按（到账日, 到账号）升序处理，凭证余额在同一批内逐笔结转；
- 每笔到账对其往来单位：合格应收按（到期日, 凭证号）升序受偿，
  先用合格应付退款互抵（同序），再用到账现金补差；
- 现金抵不完的挂「待核销」，应收抵不完的保留剩余金额继续追索。

守恒（页面、CSV、接口同一套数）：
- 到账合计 = 已抵现金 + 待核销现金
- 合格应收 = 应收已抵 + 应收剩余；合格退款 = 退款已抵 + 退款剩余
- 应收已抵 = 退款已抵 + 已抵现金
"""
from __future__ import annotations

from decimal import Decimal

ZERO = Decimal("0.00")

STATUS_LABELS = {
    "settled": "已结清",
    "partial": "部分结算",
    "open": "未结算",
    "excluded": "已排除",
}
RECEIPT_STATUS_LABELS = {
    "applied": "已核销",
    "partial": "部分核销",
    "idle": "未核销",
}
KIND_LABELS = {"receivable": "应收货款", "refund": "应付退款"}


def _exclude_reason(voucher: dict, as_of: str) -> str:
    if voucher["disputed"]:
        return "SETTLE_DISPUTED"
    if voucher["due_date"] > as_of:
        return "SETTLE_NOT_DUE"
    return ""


def _sum(items, key) -> Decimal:
    return sum((item[key] for item in items), ZERO)


def settle(vouchers: list[dict], receipts: list[dict]) -> dict:
    """vouchers/receipts 的 amount 为 Decimal；返回抵减明细、凭证结果与守恒合计。"""
    remaining = {voucher["ref"]: voucher["amount"] for voucher in vouchers}
    by_cp: dict[str, list[dict]] = {}
    for voucher in vouchers:
        by_cp.setdefault(voucher["counterparty"], []).append(voucher)
    receipt_dates: dict[str, list[str]] = {}
    for receipt in receipts:
        receipt_dates.setdefault(receipt["counterparty"], []).append(receipt["received_at"])

    allocations: list[dict] = []
    receipt_rows: list[dict] = []
    for receipt in sorted(receipts, key=lambda r: (r["received_at"], r["ref"])):
        cash = receipt["amount"]
        applied = ZERO
        as_of = receipt["received_at"]
        pool = by_cp.get(receipt["counterparty"], [])
        targets = sorted(
            (
                v for v in pool
                if v["kind"] == "receivable" and not _exclude_reason(v, as_of)
                and remaining[v["ref"]] > ZERO
            ),
            key=lambda v: (v["due_date"], v["ref"]),
        )
        sources = sorted(
            (
                v for v in pool
                if v["kind"] == "refund" and not _exclude_reason(v, as_of)
                and remaining[v["ref"]] > ZERO
            ),
            key=lambda v: (v["due_date"], v["ref"]),
        )
        seq = 0
        for target in targets:
            need = remaining[target["ref"]]
            for source in sources:
                if need <= ZERO:
                    break
                avail = remaining[source["ref"]]
                if avail <= ZERO:
                    continue
                take = min(avail, need)
                remaining[source["ref"]] -= take
                remaining[target["ref"]] -= take
                need -= take
                seq += 1
                allocations.append({
                    "receipt_no": receipt["ref"],
                    "line_seq": seq,
                    "counterparty": receipt["counterparty"],
                    "source_kind": "refund",
                    "source_no": source["ref"],
                    "target_no": target["ref"],
                    "amount": take,
                })
            if need > ZERO and cash > ZERO:
                take = min(need, cash)
                cash -= take
                remaining[target["ref"]] -= take
                applied += take
                seq += 1
                allocations.append({
                    "receipt_no": receipt["ref"],
                    "line_seq": seq,
                    "counterparty": receipt["counterparty"],
                    "source_kind": "cash",
                    "source_no": receipt["ref"],
                    "target_no": target["ref"],
                    "amount": take,
                })
        receipt_rows.append({
            "receipt_no": receipt["ref"],
            "counterparty": receipt["counterparty"],
            "received_at": receipt["received_at"],
            "amount": receipt["amount"],
            "applied": applied,
            "unapplied": cash,
            "status": "applied" if cash == ZERO else ("idle" if applied == ZERO else "partial"),
        })

    voucher_rows: list[dict] = []
    for voucher in sorted(
        vouchers, key=lambda v: (v["counterparty"], v["kind"], v["due_date"], v["ref"])
    ):
        dates = receipt_dates.get(voucher["counterparty"], [])
        reason = ""
        if voucher["disputed"]:
            reason = "SETTLE_DISPUTED"
        elif dates and voucher["due_date"] > max(dates):
            reason = "SETTLE_NOT_DUE"
        settled_amt = voucher["amount"] - remaining[voucher["ref"]]
        if reason:
            status = "excluded"
        elif remaining[voucher["ref"]] == ZERO:
            status = "settled"
        elif settled_amt > ZERO:
            status = "partial"
        else:
            status = "open"
        voucher_rows.append({
            "voucher_no": voucher["ref"],
            "counterparty": voucher["counterparty"],
            "kind": voucher["kind"],
            "due_date": voucher["due_date"],
            "disputed": voucher["disputed"],
            "original": voucher["amount"],
            "settled": settled_amt,
            "remaining": remaining[voucher["ref"]],
            "status": status,
            "exclude_reason": reason,
            "memo": voucher.get("memo", ""),
        })

    recv_all = [row for row in voucher_rows if row["kind"] == "receivable"]
    refund_all = [row for row in voucher_rows if row["kind"] == "refund"]
    recv_ok = [row for row in recv_all if not row["exclude_reason"]]
    refund_ok = [row for row in refund_all if not row["exclude_reason"]]
    excluded = [row for row in voucher_rows if row["exclude_reason"]]

    totals = {
        "receipts_total": _sum(receipt_rows, "amount"),
        "cash_applied": _sum(receipt_rows, "applied"),
        "cash_unapplied": _sum(receipt_rows, "unapplied"),
        "recv_eligible": _sum(recv_ok, "original"),
        "recv_settled": _sum(recv_ok, "settled"),
        "recv_remaining": _sum(recv_ok, "remaining"),
        "refund_eligible": _sum(refund_ok, "original"),
        "refund_consumed": _sum(refund_ok, "settled"),
        "refund_remaining": _sum(refund_ok, "remaining"),
        "excluded_amount": _sum(excluded, "original"),
    }
    counts = {
        "vouchers": len(voucher_rows),
        "receipts": len(receipt_rows),
        "allocations": len(allocations),
        "settled": sum(1 for row in voucher_rows if row["status"] == "settled"),
        "partial": sum(1 for row in voucher_rows if row["status"] == "partial"),
        "open": sum(1 for row in voucher_rows if row["status"] == "open"),
        "excluded": len(excluded),
    }
    identities = [
        {
            "key": "receipt_cash",
            "label": "到账资金守恒",
            "unit": "元",
            "left": totals["receipts_total"],
            "right": totals["cash_applied"] + totals["cash_unapplied"],
            "balanced": totals["receipts_total"]
            == totals["cash_applied"] + totals["cash_unapplied"],
        },
        {
            "key": "recv_conservation",
            "label": "合格应收守恒",
            "unit": "元",
            "left": totals["recv_eligible"],
            "right": totals["recv_settled"] + totals["recv_remaining"],
            "balanced": totals["recv_eligible"]
            == totals["recv_settled"] + totals["recv_remaining"],
        },
        {
            "key": "refund_conservation",
            "label": "合格退款守恒",
            "unit": "元",
            "left": totals["refund_eligible"],
            "right": totals["refund_consumed"] + totals["refund_remaining"],
            "balanced": totals["refund_eligible"]
            == totals["refund_consumed"] + totals["refund_remaining"],
        },
        {
            "key": "offset_composition",
            "label": "抵减构成守恒",
            "unit": "元",
            "left": totals["recv_settled"],
            "right": totals["refund_consumed"] + totals["cash_applied"],
            "balanced": totals["recv_settled"]
            == totals["refund_consumed"] + totals["cash_applied"],
        },
        {
            "key": "voucher_count",
            "label": "凭证笔数守恒",
            "unit": "笔",
            "left": counts["vouchers"],
            "right": counts["settled"] + counts["partial"] + counts["open"] + counts["excluded"],
            "balanced": counts["vouchers"]
            == counts["settled"] + counts["partial"] + counts["open"] + counts["excluded"],
        },
    ]
    return {
        "allocations": allocations,
        "receipts": receipt_rows,
        "vouchers": voucher_rows,
        "totals": totals,
        "counts": counts,
        "identities": identities,
    }
