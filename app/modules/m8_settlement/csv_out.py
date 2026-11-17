"""结算 CSV：批次、到账、抵减明细、凭证余额、合计与守恒，单表分区输出。

与页面读同一个当前批次的落库行；凭证号、到账号逐字输出，不做数值转换。
"""
from __future__ import annotations

import csv
import io

HEADER = ["区块", "编号", "往来单位", "类别", "说明", "原金额", "本次抵减",
          "剩余", "日期", "状态", "原因"]

TOTAL_LABELS = (
    ("receipts_total", "到账合计"),
    ("cash_applied", "已抵现金"),
    ("cash_unapplied", "待核销现金"),
    ("recv_eligible", "合格应收"),
    ("recv_settled", "应收已抵"),
    ("recv_remaining", "应收剩余"),
    ("refund_eligible", "合格退款"),
    ("refund_consumed", "退款已抵"),
    ("refund_remaining", "退款剩余"),
    ("excluded_amount", "排除金额"),
)


def settlement_text(data: dict) -> str:
    batch = data["batch"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER)
    writer.writerow([
        "批次", batch["batch_id"], "", f"版本 {batch['version']}",
        batch["rule_version"], "", "", "", batch["created_at"], "", "",
    ])
    for row in data["receipts"]:
        writer.writerow([
            "到账", row["receipt_no"], row["counterparty"], "到账", "",
            row["amount"], row["applied"], row["unapplied"],
            row["received_at"], row["status_label"], "",
        ])
    for row in data["allocations"]:
        writer.writerow([
            "抵减", row["receipt_no"], row["counterparty"], row["source_label"],
            f"{row['source_no']} → {row['target_no']}", "", row["amount"],
            "", "", "", "",
        ])
    for row in data["vouchers"]:
        writer.writerow([
            "凭证", row["voucher_no"], row["counterparty"], row["kind_label"],
            row["memo"], row["original"], row["settled"], row["remaining"],
            row["due_date"], row["status_label"],
            row["exclude_reason"],
        ])
    for key, label in TOTAL_LABELS:
        writer.writerow(["合计", label, "", "", "", data["totals"][key],
                         "", "", "", "", ""])
    for item in data["identities"]:
        writer.writerow([
            "校验", item["label"], "", "", "", item["left"], item["right"],
            "", "", "平衡" if item["balanced"] else "不平", "",
        ])
    return "\ufeff" + buffer.getvalue()
