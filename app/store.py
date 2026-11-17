"""SQLite 存储：批次、流水行、候选、匹配。

约定：
- 金额与手续费一律存 Decimal 字符串，不落 float；
- 一次运行的所有行在同一个事务里写入，失败整体回滚；
- 同一 run_id 重跑先删后插，行数、合计与首跑一致；
- 大整数编号（流水号、凭证号）按字符串保形，不过 int()。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    bank_sha256 TEXT NOT NULL DEFAULT '',
    ledger_sha256 TEXT NOT NULL DEFAULT '',
    bank_file TEXT NOT NULL DEFAULT '',
    ledger_file TEXT NOT NULL DEFAULT '',
    bank_bytes INTEGER NOT NULL DEFAULT 0,
    ledger_bytes INTEGER NOT NULL DEFAULT 0,
    bank_raw INTEGER NOT NULL DEFAULT 0,
    ledger_raw INTEGER NOT NULL DEFAULT 0,
    bank_valid INTEGER NOT NULL DEFAULT 0,
    ledger_valid INTEGER NOT NULL DEFAULT 0,
    bank_quar INTEGER NOT NULL DEFAULT 0,
    ledger_quar INTEGER NOT NULL DEFAULT 0,
    segments INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'empty',
    input_states_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS records (
    run_id TEXT NOT NULL,
    side TEXT NOT NULL,
    seq INTEGER NOT NULL,
    row_no INTEGER NOT NULL,
    txn_no TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL DEFAULT '',
    direction INTEGER NOT NULL DEFAULT 0,
    amount TEXT NOT NULL DEFAULT '',
    fee TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'valid',
    reason TEXT NOT NULL DEFAULT '',
    flags_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '[]',
    raw_amount TEXT NOT NULL DEFAULT '',
    raw_date TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id, side, status);
CREATE TABLE IF NOT EXISTS candidates (
    run_id TEXT NOT NULL,
    cand_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    bank_sum TEXT NOT NULL,
    ledger_sum TEXT NOT NULL,
    diff TEXT NOT NULL,
    day_gap INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id, tier);
CREATE TABLE IF NOT EXISTS matches (
    run_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    bank_sum TEXT NOT NULL,
    ledger_sum TEXT NOT NULL,
    diff TEXT NOT NULL,
    day_gap INTEGER NOT NULL DEFAULT 0,
    fee_alloc TEXT NOT NULL DEFAULT '0',
    fee_actual TEXT NOT NULL DEFAULT '0',
    bank_signed TEXT NOT NULL DEFAULT '0',
    ledger_signed TEXT NOT NULL DEFAULT '0'
);
CREATE INDEX IF NOT EXISTS idx_matches_run ON matches(run_id, tier);
CREATE TABLE IF NOT EXISTS settle_inputs (
    side TEXT NOT NULL,
    ref TEXT NOT NULL,
    counterparty TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    amount TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL DEFAULT '',
    disputed INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (side, ref)
);
CREATE TABLE IF NOT EXISTS settle_batches (
    batch_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    rule_version TEXT NOT NULL DEFAULT '',
    vouchers_sha TEXT NOT NULL DEFAULT '',
    receipts_sha TEXT NOT NULL DEFAULT '',
    totals_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'ready'
);
CREATE TABLE IF NOT EXISTS settle_receipt_rows (
    batch_id TEXT NOT NULL,
    receipt_no TEXT NOT NULL,
    counterparty TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT '',
    amount TEXT NOT NULL DEFAULT '',
    applied TEXT NOT NULL DEFAULT '',
    unapplied TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_settle_receipts ON settle_receipt_rows(batch_id, receipt_no);
CREATE TABLE IF NOT EXISTS settle_result_rows (
    batch_id TEXT NOT NULL,
    voucher_no TEXT NOT NULL,
    counterparty TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL DEFAULT '',
    disputed INTEGER NOT NULL DEFAULT 0,
    original TEXT NOT NULL DEFAULT '',
    settled TEXT NOT NULL DEFAULT '',
    remaining TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    exclude_reason TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_settle_results ON settle_result_rows(batch_id, voucher_no);
CREATE TABLE IF NOT EXISTS settle_alloc_rows (
    batch_id TEXT NOT NULL,
    receipt_no TEXT NOT NULL,
    line_seq INTEGER NOT NULL,
    counterparty TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_no TEXT NOT NULL DEFAULT '',
    target_no TEXT NOT NULL DEFAULT '',
    amount TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_settle_allocs ON settle_alloc_rows(batch_id, receipt_no, line_seq);
CREATE TABLE IF NOT EXISTS settle_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_batch_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

RUN_COLUMNS = (
    "run_id", "created_at", "rule_version", "params_json", "bank_sha256", "ledger_sha256",
    "bank_file", "ledger_file", "bank_bytes", "ledger_bytes", "bank_raw", "ledger_raw",
    "bank_valid", "ledger_valid", "bank_quar", "ledger_quar", "segments", "status",
    "input_states_json", "duration_ms",
)


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ---------- 批次 ----------
    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["params"] = json.loads(data.pop("params_json") or "{}")
        data["input_states"] = json.loads(data.pop("input_states_json") or "{}")
        return data

    def latest_run(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return self._run_row(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def save_run(self, run: dict, records: list[dict], candidates: list[dict],
                 matches: list[dict]) -> None:
        params_json = json.dumps(run.get("params", {}), ensure_ascii=False, sort_keys=True)
        states_json = json.dumps(run.get("input_states", {}), ensure_ascii=False)
        with self._conn() as conn:
            run_id = run["run_id"]
            conn.execute("DELETE FROM records WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM candidates WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM matches WHERE run_id = ?", (run_id,))
            conn.execute(
                """INSERT OR REPLACE INTO runs (
                    run_id, created_at, rule_version, params_json, bank_sha256, ledger_sha256,
                    bank_file, ledger_file, bank_bytes, ledger_bytes, bank_raw, ledger_raw,
                    bank_valid, ledger_valid, bank_quar, ledger_quar, segments, status,
                    input_states_json, duration_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, run["created_at"], run["rule_version"], params_json,
                    run.get("bank_sha256", ""), run.get("ledger_sha256", ""),
                    run.get("bank_file", ""), run.get("ledger_file", ""),
                    run.get("bank_bytes", 0), run.get("ledger_bytes", 0),
                    run.get("bank_raw", 0), run.get("ledger_raw", 0),
                    run.get("bank_valid", 0), run.get("ledger_valid", 0),
                    run.get("bank_quar", 0), run.get("ledger_quar", 0),
                    run.get("segments", 0), run.get("status", "empty"),
                    states_json, run.get("duration_ms", 0),
                ),
            )
            conn.executemany(
                """INSERT INTO records (run_id, side, seq, row_no, txn_no, account, posted_at,
                    direction, amount, fee, memo, counterparty, status, reason, flags_json,
                    raw_json, raw_amount, raw_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, r["side"], r["seq"], r["row_no"], r["txn_no"], r.get("account", ""),
                        r.get("posted_at", ""), r.get("direction", 0), str(r.get("amount", "")),
                        str(r.get("fee", "")), r.get("memo", ""), r.get("counterparty", ""),
                        r.get("status", "valid"), r.get("reason", ""),
                        json.dumps(r.get("flags", []), ensure_ascii=False),
                        json.dumps(r.get("raw", []), ensure_ascii=False),
                        str(r.get("raw_amount", "")), str(r.get("raw_date", "")),
                    )
                    for r in records
                ],
            )
            conn.executemany(
                """INSERT INTO candidates (run_id, cand_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap) VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, c["cand_id"], c["tier"],
                        json.dumps(c["bank_ids"], ensure_ascii=False),
                        json.dumps(c["ledger_ids"], ensure_ascii=False),
                        str(c["bank_sum"]), str(c["ledger_sum"]), str(c["diff"]), c["day_gap"],
                    )
                    for c in candidates
                ],
            )
            conn.executemany(
                """INSERT INTO matches (run_id, match_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap, fee_alloc, fee_actual,
                    bank_signed, ledger_signed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, m["match_id"], m["tier"],
                        json.dumps(m["bank_ids"], ensure_ascii=False),
                        json.dumps(m["ledger_ids"], ensure_ascii=False),
                        str(m["bank_sum"]), str(m["ledger_sum"]), str(m["diff"]), m["day_gap"],
                        str(m.get("fee_alloc", "0")), str(m.get("fee_actual", "0")),
                        str(m.get("bank_signed", "0")), str(m.get("ledger_signed", "0")),
                    )
                    for m in matches
                ],
            )

    def set_status(self, run_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))

    # ---------- 行 ----------
    def records(self, run_id: str, side: str | None = None,
                status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM records WHERE run_id = ?"
        args: list = [run_id]
        if side:
            sql += " AND side = ?"
            args.append(side)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY side, row_no, seq"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["flags"] = json.loads(item.pop("flags_json") or "[]")
            item["raw"] = json.loads(item.pop("raw_json") or "[]")
            out.append(item)
        return out

    def quarantine(self, run_id: str) -> list[dict]:
        return self.records(run_id, status="quarantine")

    def candidates(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE run_id = ? ORDER BY tier, cand_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            out.append(item)
        return out

    def matches(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM matches WHERE run_id = ? ORDER BY tier, match_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            out.append(item)
        return out

    def replace_candidates(self, run_id: str, candidates: list[dict]) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM candidates WHERE run_id = ?", (run_id,))
            conn.executemany(
                """INSERT INTO candidates (run_id, cand_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap) VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, c["cand_id"], c["tier"],
                        json.dumps(c["bank_ids"], ensure_ascii=False),
                        json.dumps(c["ledger_ids"], ensure_ascii=False),
                        str(c["bank_sum"]), str(c["ledger_sum"]), str(c["diff"]), c["day_gap"],
                    )
                    for c in candidates
                ],
            )

    def replace_matches(self, run_id: str, matches: list[dict]) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM matches WHERE run_id = ?", (run_id,))
            conn.executemany(
                """INSERT INTO matches (run_id, match_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap, fee_alloc, fee_actual,
                    bank_signed, ledger_signed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, m["match_id"], m["tier"],
                        json.dumps(m["bank_ids"], ensure_ascii=False),
                        json.dumps(m["ledger_ids"], ensure_ascii=False),
                        str(m["bank_sum"]), str(m["ledger_sum"]), str(m["diff"]), m["day_gap"],
                        str(m.get("fee_alloc", "0")), str(m.get("fee_actual", "0")),
                        str(m.get("bank_signed", "0")), str(m.get("ledger_signed", "0")),
                    )
                    for m in matches
                ],
            )

    # ---------- 净额结算 ----------
    def replace_settle_inputs(self, side: str, rows: list[dict]) -> None:
        """同一侧（凭证/到账）整体替换，重导同一文件行数与内容不变。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM settle_inputs WHERE side = ?", (side,))
            conn.executemany(
                """INSERT INTO settle_inputs (side, ref, counterparty, kind, amount,
                    due_date, disputed, received_at, memo, seq)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        side, r["ref"], r.get("counterparty", ""), r.get("kind", ""),
                        str(r.get("amount", "")), r.get("due_date", ""),
                        int(bool(r.get("disputed", False))), r.get("received_at", ""),
                        r.get("memo", ""), r.get("seq", index),
                    )
                    for index, r in enumerate(rows)
                ],
            )

    def settle_inputs(self, side: str | None = None) -> list[dict]:
        sql = "SELECT * FROM settle_inputs"
        args: list = []
        if side:
            sql += " WHERE side = ?"
            args.append(side)
        sql += " ORDER BY side, seq, ref"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["disputed"] = bool(item["disputed"])
            out.append(item)
        return out

    def save_settle_batch(self, batch: dict, receipt_rows: list[dict],
                          result_rows: list[dict], alloc_rows: list[dict]) -> tuple[int, str]:
        """整批落库并翻转当前指针：同一事务内先删后插。

        同一 batch_id 重跑保留原版本号与首次生成时间；中断时整体回滚，
        旧版本与当前指针不受影响。
        """
        totals_json = json.dumps(
            {"totals": batch.get("totals", {}), "counts": batch.get("counts", {})},
            ensure_ascii=False, sort_keys=True,
        )
        with self._conn() as conn:
            batch_id = batch["batch_id"]
            for table in ("settle_receipt_rows", "settle_result_rows", "settle_alloc_rows"):
                conn.execute(f"DELETE FROM {table} WHERE batch_id = ?", (batch_id,))
            conn.executemany(
                """INSERT INTO settle_receipt_rows (batch_id, receipt_no, counterparty,
                    received_at, amount, applied, unapplied, status)
                VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (
                        batch_id, r["receipt_no"], r["counterparty"], r["received_at"],
                        str(r["amount"]), str(r["applied"]), str(r["unapplied"]), r["status"],
                    )
                    for r in receipt_rows
                ],
            )
            conn.executemany(
                """INSERT INTO settle_result_rows (batch_id, voucher_no, counterparty, kind,
                    due_date, disputed, original, settled, remaining, status,
                    exclude_reason, memo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        batch_id, r["voucher_no"], r["counterparty"], r["kind"],
                        r["due_date"], int(bool(r["disputed"])), str(r["original"]),
                        str(r["settled"]), str(r["remaining"]), r["status"],
                        r["exclude_reason"], r["memo"],
                    )
                    for r in result_rows
                ],
            )
            conn.executemany(
                """INSERT INTO settle_alloc_rows (batch_id, receipt_no, line_seq,
                    counterparty, source_kind, source_no, target_no, amount)
                VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (
                        batch_id, a["receipt_no"], a["line_seq"], a["counterparty"],
                        a["source_kind"], a["source_no"], a["target_no"], str(a["amount"]),
                    )
                    for a in alloc_rows
                ],
            )
            existing = conn.execute(
                "SELECT version, created_at FROM settle_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if existing:
                version, created_at = existing["version"], existing["created_at"]
                conn.execute(
                    """UPDATE settle_batches SET totals_json = ?, status = 'ready',
                        vouchers_sha = ?, receipts_sha = ? WHERE batch_id = ?""",
                    (totals_json, batch.get("vouchers_sha", ""),
                     batch.get("receipts_sha", ""), batch_id),
                )
            else:
                row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM settle_batches"
                ).fetchone()
                version = row["v"]
                created_at = batch["created_at"]
                conn.execute(
                    """INSERT INTO settle_batches (batch_id, version, created_at,
                        rule_version, vouchers_sha, receipts_sha, totals_json, status)
                    VALUES (?,?,?,?,?,?,?,'ready')""",
                    (
                        batch_id, version, created_at, batch.get("rule_version", ""),
                        batch.get("vouchers_sha", ""), batch.get("receipts_sha", ""),
                        totals_json,
                    ),
                )
            conn.execute(
                """INSERT OR REPLACE INTO settle_state (id, current_batch_id, updated_at)
                VALUES (1, ?, ?)""",
                (batch_id, batch.get("switched_at", created_at)),
            )
        return version, created_at

    @staticmethod
    def _settle_batch_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        stored = json.loads(data.pop("totals_json") or "{}")
        data["totals"] = stored.get("totals", {})
        data["counts"] = stored.get("counts", {})
        return data

    def current_settle_batch(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT b.* FROM settle_batches b
                JOIN settle_state s ON s.current_batch_id = b.batch_id
                WHERE s.id = 1"""
            ).fetchone()
        return self._settle_batch_row(row)

    def get_settle_batch(self, batch_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM settle_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return self._settle_batch_row(row)

    def list_settle_batches(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM settle_batches ORDER BY version DESC"
            ).fetchall()
            current = conn.execute(
                "SELECT current_batch_id FROM settle_state WHERE id = 1"
            ).fetchone()
        current_id = current["current_batch_id"] if current else ""
        out = []
        for row in rows:
            item = self._settle_batch_row(row)
            item["current"] = item["batch_id"] == current_id
            out.append(item)
        return out

    def settle_receipt_rows(self, batch_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM settle_receipt_rows WHERE batch_id = ?
                ORDER BY received_at, receipt_no""",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def settle_result_rows(self, batch_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM settle_result_rows WHERE batch_id = ?
                ORDER BY counterparty, kind, due_date, voucher_no""",
                (batch_id,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["disputed"] = bool(item["disputed"])
            out.append(item)
        return out

    def settle_alloc_rows(self, batch_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM settle_alloc_rows WHERE batch_id = ?
                ORDER BY receipt_no, line_seq""",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_current_settle(self, batch_id: str, switched_at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO settle_state (id, current_batch_id, updated_at)
                VALUES (1, ?, ?)""",
                (batch_id, switched_at),
            )
