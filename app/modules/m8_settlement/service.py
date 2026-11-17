"""净额结算编排：导入 → 执行 → 当前批次查询 → 版本恢复 → CSV。

- batch_id 由规则版本 + 凭证指纹 + 到账指纹决定：同一到账重试命中同一批次，
  事务内先删后插，不会重复冲销；
- 每次执行生成一个版本并原子翻转为当前版本；中断时旧版本保持当前；
- 页面与 CSV 都只读当前批次的落库行，保证同一批次为准。
"""
from __future__ import annotations

import hashlib

from ... import reasons
from ...config import dec
from ...errors import DomainError
from ...manifest import sha256_hex, utc_now_iso
from ..m1_ingest.service import resolve_sample
from . import csv_out, parse, settle

SAMPLE_VOUCHERS = "settle_vouchers.csv"
SAMPLE_RECEIPTS = "settle_receipts.csv"

_SIDES = ("vouchers", "receipts")


def _fingerprint(rows: list[dict], fields: tuple[str, ...]) -> str:
    lines = []
    for row in sorted(rows, key=lambda item: item["ref"]):
        cells = []
        for field in fields:
            value = row.get(field, "")
            if isinstance(value, bool):
                value = "1" if value else "0"
            cells.append(str(value))
        lines.append("|".join(cells))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _input_fingerprints(store) -> tuple[str, str]:
    vouchers = store.settle_inputs("vouchers")
    receipts = store.settle_inputs("receipts")
    voucher_fp = _fingerprint(
        vouchers, ("ref", "counterparty", "kind", "amount", "due_date", "disputed", "memo")
    )
    receipt_fp = _fingerprint(
        receipts, ("ref", "counterparty", "amount", "received_at", "memo")
    )
    return voucher_fp, receipt_fp


def _make_batch_id(rule_version: str, voucher_fp: str, receipt_fp: str) -> str:
    payload = "|".join([rule_version, voucher_fp, receipt_fp])
    return "sb-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def import_side(settings, store, side: str, filename: str, raw: bytes) -> dict:
    if side not in _SIDES:
        raise DomainError("E_SIDE", "side 只能是 vouchers 或 receipts", 400)
    rows = (
        parse.parse_vouchers(settings, raw)
        if side == "vouchers"
        else parse.parse_receipts(settings, raw)
    )
    store.replace_settle_inputs(side, rows)
    return {
        "side": side,
        "filename": filename,
        "count": len(rows),
        "sha256": sha256_hex(raw),
    }


def _load_inputs(store) -> tuple[list[dict], list[dict]]:
    vouchers = store.settle_inputs("vouchers")
    receipts = store.settle_inputs("receipts")
    for row in vouchers + receipts:
        row["amount"] = dec(row["amount"])
    return vouchers, receipts


def apply(settings, store) -> dict:
    vouchers, receipts = _load_inputs(store)
    if not vouchers or not receipts:
        raise DomainError("E_NO_INPUT", "先导入往来凭证与到账清单，再执行结算", 409)
    result = settle.settle(vouchers, receipts)
    voucher_fp, receipt_fp = _input_fingerprints(store)
    batch = {
        "batch_id": _make_batch_id(settings.rule_version, voucher_fp, receipt_fp),
        "created_at": utc_now_iso(),
        "switched_at": utc_now_iso(),
        "rule_version": settings.rule_version,
        "vouchers_sha": voucher_fp,
        "receipts_sha": receipt_fp,
        "totals": {key: format(value, "f") for key, value in result["totals"].items()},
        "counts": result["counts"],
    }
    version, created_at = store.save_settle_batch(
        batch, result["receipts"], result["vouchers"], result["allocations"]
    )
    data = payload(store)
    data["applied"] = {"batch_id": batch["batch_id"], "version": version,
                       "created_at": created_at}
    return data


def demo(settings, store) -> dict:
    voucher_file, voucher_raw = resolve_sample(settings, SAMPLE_VOUCHERS)
    receipt_file, receipt_raw = resolve_sample(settings, SAMPLE_RECEIPTS)
    import_side(settings, store, "vouchers", voucher_file, voucher_raw)
    import_side(settings, store, "receipts", receipt_file, receipt_raw)
    return apply(settings, store)


def restore(store, batch_id: str) -> dict:
    batch = store.get_settle_batch(batch_id)
    if not batch:
        raise DomainError("E_NO_BATCH", f"结算版本不存在：{batch_id}", 404)
    store.set_current_settle(batch_id, utc_now_iso())
    return payload(store)


def _money_rows(rows: list[dict], fields: tuple[str, ...]) -> list[dict]:
    out = []
    for row in rows:
        item = dict(row)
        for field in fields:
            item[field] = format(dec(item[field]), "f")
        out.append(item)
    return out


def payload(store) -> dict:
    vouchers_input = store.settle_inputs("vouchers")
    receipts_input = store.settle_inputs("receipts")
    batch = store.current_settle_batch()
    base = {
        "batch": None,
        "stale": bool(vouchers_input or receipts_input),
        "inputs": {"vouchers": len(vouchers_input), "receipts": len(receipts_input)},
        "receipts": [],
        "allocations": [],
        "vouchers": [],
        "totals": {},
        "counts": {},
        "identities": [],
        "versions": [
            {
                "batch_id": item["batch_id"],
                "version": item["version"],
                "created_at": item["created_at"],
                "current": item["current"],
            }
            for item in store.list_settle_batches()
        ],
    }
    if not batch:
        return base
    voucher_fp, receipt_fp = _input_fingerprints(store)
    receipts = _money_rows(
        store.settle_receipt_rows(batch["batch_id"]),
        ("amount", "applied", "unapplied"),
    )
    for row in receipts:
        row["status_label"] = settle.RECEIPT_STATUS_LABELS.get(row["status"], row["status"])
    vouchers = _money_rows(
        store.settle_result_rows(batch["batch_id"]),
        ("original", "settled", "remaining"),
    )
    for row in vouchers:
        row["kind_label"] = settle.KIND_LABELS.get(row["kind"], row["kind"])
        row["status_label"] = settle.STATUS_LABELS.get(row["status"], row["status"])
        row["exclude_label"] = (
            reasons.label(row["exclude_reason"]) if row["exclude_reason"] else ""
        )
    allocations = _money_rows(store.settle_alloc_rows(batch["batch_id"]), ("amount",))
    for row in allocations:
        row["source_label"] = "退款冲抵" if row["source_kind"] == "refund" else "现金核销"
    totals = {key: format(dec(value), "f") for key, value in batch["totals"].items()}
    counts = batch["counts"]
    identities = _rebuild_identities(totals, counts)
    base.update({
        "batch": {
            "batch_id": batch["batch_id"],
            "version": batch["version"],
            "created_at": batch["created_at"],
            "rule_version": batch["rule_version"],
            "vouchers_sha": batch["vouchers_sha"],
            "receipts_sha": batch["receipts_sha"],
        },
        "stale": voucher_fp != batch["vouchers_sha"] or receipt_fp != batch["receipts_sha"],
        "receipts": receipts,
        "allocations": allocations,
        "vouchers": vouchers,
        "totals": totals,
        "counts": counts,
        "identities": identities,
    })
    return base


def _rebuild_identities(totals: dict, counts: dict) -> list[dict]:
    """从落库合计重建守恒校验，页面与 CSV 共用同一批数字。"""
    t = {key: dec(value) for key, value in totals.items()}
    identities = [
        ("receipt_cash", "到账资金守恒", "元", t["receipts_total"],
         t["cash_applied"] + t["cash_unapplied"]),
        ("recv_conservation", "合格应收守恒", "元", t["recv_eligible"],
         t["recv_settled"] + t["recv_remaining"]),
        ("refund_conservation", "合格退款守恒", "元", t["refund_eligible"],
         t["refund_consumed"] + t["refund_remaining"]),
        ("offset_composition", "抵减构成守恒", "元", t["recv_settled"],
         t["refund_consumed"] + t["cash_applied"]),
        ("voucher_count", "凭证笔数守恒", "笔", counts.get("vouchers", 0),
         counts.get("settled", 0) + counts.get("partial", 0)
         + counts.get("open", 0) + counts.get("excluded", 0)),
    ]
    return [
        {
            "key": key,
            "label": label,
            "unit": unit,
            "left": format(dec(left), "f") if unit == "元" else left,
            "right": format(dec(right), "f") if unit == "元" else right,
            "balanced": dec(left) == dec(right) if unit == "元" else left == right,
        }
        for key, label, unit, left, right in identities
    ]


def csv_text(store) -> str:
    batch = store.current_settle_batch()
    if not batch:
        raise DomainError("E_NO_BATCH", "还没有结算批次，先执行结算", 404)
    data = payload(store)
    return csv_out.settlement_text(data)
