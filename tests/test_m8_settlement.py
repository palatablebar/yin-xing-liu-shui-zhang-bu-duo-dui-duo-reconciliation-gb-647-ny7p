"""M8 净额结算：合格性、抵减顺序、守恒、幂等重试、版本恢复、页面与 CSV 同批。"""
from decimal import Decimal

import pytest

from app.modules.m8_settlement import settle

DEMO_BATCH_TOTALS = {
    "receipts_total": "19500.00",
    "cash_applied": "19000.00",
    "cash_unapplied": "500.00",
    "recv_eligible": "30500.00",
    "recv_settled": "24500.00",
    "recv_remaining": "6000.00",
    "refund_eligible": "5500.00",
    "refund_consumed": "5500.00",
    "refund_remaining": "0.00",
    "excluded_amount": "6500.00",
}


@pytest.fixture()
def settle_demo(client):
    response = client.post("/api/settlements/demo")
    assert response.status_code == 200, response.text
    return response.json()


def _voucher(rows, ref):
    return next(row for row in rows if row["voucher_no"] == ref)


def _import(client, side, text, filename="input.csv"):
    return client.post("/api/settlements/import",
                       data={"side": side},
                       files={"file": (filename, text.encode("utf-8"))})


def test_demo_settlement_totals_and_identities(client, settle_demo):
    assert settle_demo["totals"] == DEMO_BATCH_TOTALS
    assert settle_demo["counts"]["allocations"] == 5
    assert all(item["balanced"] for item in settle_demo["identities"])


def test_disputed_and_not_due_excluded(client, settle_demo):
    rows = settle_demo["vouchers"]
    disputed = _voucher(rows, "RF-2002")
    not_due = _voucher(rows, "AR-1003")
    assert disputed["status"] == "excluded" and disputed["exclude_reason"] == "SETTLE_DISPUTED"
    assert not_due["status"] == "excluded" and not_due["exclude_reason"] == "SETTLE_NOT_DUE"
    # 排除的凭证一分钱都没被冲掉
    assert disputed["settled"] == "0.00" and disputed["remaining"] == "1500.00"
    assert not_due["settled"] == "0.00" and not_due["remaining"] == "5000.00"


def test_allocation_refund_first_then_cash(client, settle_demo):
    allocs = settle_demo["allocations"]
    first = allocs[0]
    assert (first["source_kind"], first["source_no"], first["target_no"],
            first["amount"]) == ("refund", "RF-2001", "AR-1001", "3000.00")
    # 退款用尽后才用到账现金，且应收按到期日升序受偿
    assert [(a["source_kind"], a["target_no"]) for a in allocs[1:3]] == [
        ("cash", "AR-1001"), ("cash", "AR-1002"),
    ]


def test_partial_settlement_keeps_remaining(client, settle_demo):
    row = _voucher(settle_demo["vouchers"], "AR-1002")
    assert row["status"] == "partial"
    assert Decimal(row["settled"]) + Decimal(row["remaining"]) == Decimal(row["original"])
    assert row["remaining"] == "1500.00"


def test_unapplied_cash_stays_suspense(client, settle_demo):
    receipt = next(row for row in settle_demo["receipts"] if row["receipt_no"] == "RC-9002")
    assert receipt["status"] == "partial"
    assert receipt["unapplied"] == "500.00"
    assert settle_demo["totals"]["cash_unapplied"] == "500.00"


def test_retry_same_receipt_no_duplicate(client, settle_demo):
    first_id = settle_demo["batch"]["batch_id"]
    again = client.post("/api/settlements/apply").json()
    assert again["batch"]["batch_id"] == first_id
    assert again["batch"]["version"] == 1
    assert len(again["versions"]) == 1
    assert again["counts"]["allocations"] == 5
    assert again["totals"] == settle_demo["totals"]
    # 重新导入同一到账清单再执行，仍然命中同一批次
    receipts = open("sample_data/settle_receipts.csv", "rb").read()
    client.post("/api/settlements/import",
                data={"side": "receipts"},
                files={"file": ("settle_receipts.csv", receipts)})
    third = client.post("/api/settlements/apply").json()
    assert third["batch"]["batch_id"] == first_id
    assert len(third["versions"]) == 1
    assert third["counts"]["allocations"] == 5


def test_new_inputs_create_new_version(client, settle_demo):
    first_id = settle_demo["batch"]["batch_id"]
    text = open("sample_data/settle_vouchers.csv", encoding="utf-8").read()
    text += "AR-1004,华东联采,应收,1000.00,2026-11-01,否,补票\n"
    response = _import(client, "vouchers", text)
    assert response.status_code == 200
    applied = client.post("/api/settlements/apply").json()
    assert applied["batch"]["batch_id"] != first_id
    assert applied["batch"]["version"] == 2
    assert [(v["version"], v["current"]) for v in applied["versions"]] == [
        (2, True), (1, False)]


def test_restore_old_version(client, settle_demo):
    first_id = settle_demo["batch"]["batch_id"]
    text = open("sample_data/settle_vouchers.csv", encoding="utf-8").read()
    text += "AR-1004,华东联采,应收,1000.00,2026-11-01,否,补票\n"
    _import(client, "vouchers", text)
    client.post("/api/settlements/apply")
    restored = client.post(f"/api/settlements/restore/{first_id}").json()
    assert restored["batch"]["batch_id"] == first_id
    assert restored["batch"]["version"] == 1
    assert restored["totals"] == DEMO_BATCH_TOTALS
    current = [v for v in restored["versions"] if v["current"]]
    assert len(current) == 1 and current[0]["batch_id"] == first_id


def test_interrupted_apply_keeps_current(client, settle_demo, monkeypatch):
    first_id = settle_demo["batch"]["batch_id"]
    store = client.app.state.store

    def boom(*args, **kwargs):
        raise RuntimeError("模拟写库中断")

    monkeypatch.setattr(store, "save_settle_batch", boom)
    with pytest.raises(RuntimeError):
        client.post("/api/settlements/apply")
    after = client.get("/api/settlements").json()
    assert after["batch"]["batch_id"] == first_id
    assert after["totals"] == DEMO_BATCH_TOTALS
    assert len(after["versions"]) == 1


def test_page_and_csv_same_batch(client, settle_demo):
    batch_id = settle_demo["batch"]["batch_id"]
    page = client.get("/settlements")
    assert page.status_code == 200
    assert batch_id in page.text
    csv_text = client.get("/api/settlements/export.csv").text
    assert batch_id in csv_text
    # 页面与下载稿读同一批次：合计数字一致
    for key, value in DEMO_BATCH_TOTALS.items():
        assert value in csv_text, key
    assert "抵减构成守恒" in csv_text


def test_apply_without_inputs_409(client):
    response = client.post("/api/settlements/apply")
    assert response.status_code == 409
    assert response.json()["error"] == "E_NO_INPUT"


def test_restore_unknown_version_404(client, settle_demo):
    response = client.post("/api/settlements/restore/sb-nonexistent")
    assert response.status_code == 404
    assert response.json()["error"] == "E_NO_BATCH"


def test_csv_requires_batch_404(client):
    assert client.get("/api/settlements/export.csv").status_code == 404


def test_settlement_page_200_without_data(client):
    response = client.get("/settlements")
    assert response.status_code == 200
    assert "净额结算" in response.text


def test_strict_import_rejects_bad_amount(client):
    text = "凭证号,往来单位,类型,金额,到期日,争议,备注\nAR-1,某单位,应收,abc,2026-11-01,否,\n"
    response = _import(client, "vouchers", text)
    assert response.status_code == 400
    assert response.json()["error"] == "E_SETTLE_ROW"


def test_strict_import_rejects_duplicate_ref(client):
    text = (
        "凭证号,往来单位,类型,金额,到期日,争议,备注\n"
        "AR-1,某单位,应收,100.00,2026-11-01,否,\n"
        "AR-1,某单位,应收,200.00,2026-11-02,否,\n"
    )
    response = _import(client, "vouchers", text)
    assert response.status_code == 400
    assert response.json()["error"] == "E_SETTLE_DUP"


def test_strict_import_rejects_missing_column(client):
    text = "凭证号,往来单位,类型,金额\nAR-1,某单位,应收,100.00\n"
    response = _import(client, "vouchers", text)
    assert response.status_code == 400
    assert response.json()["error"] == "E_SETTLE_HEADER"


def test_strict_import_rejects_empty_file(client):
    response = _import(client, "vouchers", "")
    assert response.status_code == 400
    assert response.json()["error"] == "E_SETTLE_FILE"


def test_import_replaces_side_wholesale(client, settle_demo):
    text = "凭证号,往来单位,类型,金额,到期日,争议,备注\nAR-9,某单位,应收,100.00,2026-11-01,否,\n"
    _import(client, "vouchers", text)
    data = client.get("/api/settlements").json()
    assert data["inputs"] == {"vouchers": 1, "receipts": 2}
    assert data["stale"] is True


def test_big_voucher_no_kept_verbatim(client):
    big = "9007199254740993"
    vouchers = (
        "凭证号,往来单位,类型,金额,到期日,争议,备注\n"
        f"{big},某单位,应收,100.00,2026-11-01,否,\n"
    )
    receipts = "到账号,往来单位,金额,到账日,备注\nRC-1,某单位,100.00,2026-11-02,\n"
    _import(client, "vouchers", vouchers)
    _import(client, "receipts", receipts)
    client.post("/api/settlements/apply")
    data = client.get("/api/settlements").json()
    assert data["vouchers"][0]["voucher_no"] == big
    csv_text = client.get("/api/settlements/export.csv").text
    assert big in csv_text
    assert "9.007" not in csv_text.replace("9007199254740993", "")


def test_multi_receipt_same_counterparty_carryover():
    vouchers = [
        {"ref": "AR-1", "counterparty": "甲", "kind": "receivable",
         "amount": Decimal("1000.00"), "due_date": "2026-11-01",
         "disputed": False, "memo": ""},
    ]
    receipts = [
        {"ref": "R-2", "counterparty": "甲", "amount": Decimal("700.00"),
         "received_at": "2026-11-06", "memo": ""},
        {"ref": "R-1", "counterparty": "甲", "amount": Decimal("400.00"),
         "received_at": "2026-11-05", "memo": ""},
    ]
    result = settle.settle(vouchers, receipts)
    rows = {row["receipt_no"]: row for row in result["receipts"]}
    # 按到账日升序处理：R-1 先抵 400，R-2 抵剩余 600 后挂 100 待核销
    assert rows["R-1"]["applied"] == Decimal("400.00")
    assert rows["R-2"]["applied"] == Decimal("600.00")
    assert rows["R-2"]["unapplied"] == Decimal("100.00")
    voucher = result["vouchers"][0]
    assert voucher["status"] == "settled" and voucher["remaining"] == Decimal("0.00")


def test_due_on_receipt_date_is_eligible():
    vouchers = [
        {"ref": "AR-1", "counterparty": "甲", "kind": "receivable",
         "amount": Decimal("500.00"), "due_date": "2026-11-10",
         "disputed": False, "memo": ""},
    ]
    receipts = [
        {"ref": "R-1", "counterparty": "甲", "amount": Decimal("500.00"),
         "received_at": "2026-11-10", "memo": ""},
    ]
    result = settle.settle(vouchers, receipts)
    assert result["vouchers"][0]["status"] == "settled"
    assert result["totals"]["cash_unapplied"] == Decimal("0.00")


def test_settle_result_order_independent():
    vouchers = [
        {"ref": "AR-2", "counterparty": "甲", "kind": "receivable",
         "amount": Decimal("300.00"), "due_date": "2026-11-02",
         "disputed": False, "memo": ""},
        {"ref": "AR-1", "counterparty": "甲", "kind": "receivable",
         "amount": Decimal("200.00"), "due_date": "2026-11-01",
         "disputed": False, "memo": ""},
        {"ref": "RF-1", "counterparty": "甲", "kind": "refund",
         "amount": Decimal("100.00"), "due_date": "2026-10-30",
         "disputed": False, "memo": ""},
    ]
    receipts = [
        {"ref": "R-1", "counterparty": "甲", "amount": Decimal("400.00"),
         "received_at": "2026-11-05", "memo": ""},
    ]
    forward = settle.settle(vouchers, receipts)
    backward = settle.settle(list(reversed(vouchers)), list(reversed(receipts)))
    assert [(a["source_no"], a["target_no"], a["amount"]) for a in forward["allocations"]] == [
        (a["source_no"], a["target_no"], a["amount"]) for a in backward["allocations"]
    ]
    assert forward["totals"] == backward["totals"]
