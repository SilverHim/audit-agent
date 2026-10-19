"""Validate the synthetic dataset's accounting, scenario, PDF and determinism invariants."""

import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pdfplumber
from generate_demo import ROOT, generate, working


def check(root=ROOT / "demo", *, determinism=True):
    def rows(name):
        with (root / (name + ".csv")).open(newline="") as stream:
            return list(csv.DictReader(stream))

    a = rows("accounts")
    tb = rows("trial_balance")
    j = rows("journal_entries")
    inv = rows("sales_invoices")
    dn = rows("delivery_notes")
    b = rows("bank_transactions")
    counts = {
        "accounts": 23,
        "trial_balance": 46,
        "journal_entries": 2686,
        "sales_invoices": 624,
        "delivery_notes": 624,
        "bank_accounts": 2,
        "bank_transactions": 696,
    }
    for name, count in counts.items():
        data = rows(name)
        assert len(data) == count, (name, len(data))
        if "id" in data[0]:
            assert len({r["id"] for r in data}) == count, name
    balances = {
        p: {r["account_code"]: int(r["balance_cents"]) for r in tb if r["period"] == p} for p in ("current", "prior")
    }
    kinds = {r["code"]: r["type"] for r in a}
    for p, values in balances.items():
        assert sum(values.values()) == 0, p
        expected = (
            (1200000000, 800000000, 150000000, 105000000)
            if p == "current"
            else (1040000000, 702600000, 127400000, 89180000)
        )
        assert (
            -values["4100"],
            sum(v for c, v in values.items() if kinds[c] == "asset"),
            -sum(v for c, v in values.items() if kinds[c] in {"revenue", "expense"} and c != "8100"),
            -sum(v for c, v in values.items() if kinds[c] in {"revenue", "expense"}),
        ) == expected
    current, prior = balances["current"], balances["prior"]
    # PLN-07 A01: 资产负债类对期初 (留存收益加上期损益结转), 损益类 (两期等长) 对上期; 门槛为本期收入的 0.5%
    # 且不小于基数绝对值的 20%. 标准分类按名称归类 (导入时补): 3200 Retained earnings 为 retained_earnings,
    # 收入类只有 4100 Sales revenue 为 revenue (4200 Interest income 为 other_income)
    carried = sum(v for c, v in prior.items() if kinds[c] in {"revenue", "expense"})
    opening = {c: prior[c] + (carried if c == "3200" else 0) for c in current}
    threshold = 0.005 * -current["4100"]
    assert {
        c
        for c, v in current.items()
        if v != opening[c] and abs(v - opening[c]) >= threshold and abs(v - opening[c]) >= abs(opening[c]) * 0.2
    } == {"1100"}
    entries = defaultdict(list)
    movement = Counter()
    by_id = {r["id"]: r for r in j}
    for r in j:
        entries[r["entry_no"]].append(r)
        movement[r["account_code"]] += int(r["debit_cents"]) - int(r["credit_cents"])
        day = date.fromisoformat(r["posting_date"])
        assert date(2025, 7, 1) <= day <= date(2026, 6, 30)
        assert working(day) or r["entry_no"] == "SJ-2026-0612"
        if r["account_code"] == "4100":
            assert r["entry_no"].startswith("SJ-") and int(r["debit_cents"]) == 0
    for key, group in entries.items():
        assert len(group) == 2 and sum(int(r["debit_cents"]) - int(r["credit_cents"]) for r in group) == 0, key
    for code, value in current.items():
        opening = 0 if kinds[code] in {"revenue", "expense"} else prior[code]
        if code == "3200":
            opening = -416100000
        assert opening + movement[code] == value, (code, opening + movement[code], value)
    assert sum(int(r["amount_cents"]) for r in inv) == 1200000000
    assert [r["id"] for r in inv] == [f"SI-2026-{n:04d}" for n in range(1, 625)]
    assert [r["posting_date"] for r in inv] == sorted(r["posting_date"] for r in inv)
    month_sums = Counter()
    month_counts = Counter()
    for r in inv:
        month_sums[r["posting_date"][:7]] += int(r["amount_cents"])
        month_counts[r["posting_date"][:7]] += 1
    assert list(month_sums.values()) == [
        v * 100
        for v in (880000, 900000, 940000, 980000, 1040000, 860000, 800000, 940000, 1110000, 975000, 1075000, 1500000)
    ]
    assert list(month_counts.values()) == [46, 47, 49, 51, 54, 45, 42, 49, 58, 51, 55, 77]
    assert sum(int(r["amount_cents"]) for r in inv if r["posting_date"] >= "2026-05-18") == 207500000
    assert {r["id"] for r in inv if r["posting_date"] >= "2026-06-10" and int(r["amount_cents"]) >= 5000000} == {
        "SI-2026-0612",
        "SI-2026-0618",
    }
    fixed = {519, 520, 521, 522, 523, 524, 547, 612, 618}
    galah = [r for r in inv if r["contract_id"] == "CON-003"]
    assert len(galah) == 24 and sum(int(r["amount_cents"]) for r in galah) <= 60000000
    assert [r["id"] for r in inv if r["contract_id"] == "CON-001"] == ["SI-2026-0612"]
    assert [r["id"] for r in inv if r["contract_id"] == "CON-002"] == ["SI-2026-0618"]
    delivery = {r["invoice_id"]: r for r in dn}
    assert len(delivery) == 624
    collected = {r["reference"]: r for r in b if r["reference"].startswith("SI-2026-")}
    assert len(collected) == len([r for r in b if r["reference"].startswith("SI-2026-")])
    unpaid = 0
    for r in inv:
        num = int(r["id"][-4:])
        amount = int(r["amount_cents"])
        d = delivery[r["id"]]
        assert int(by_id[r["journal_entry_id"]]["credit_cents"]) == amount
        if num not in fixed:
            assert 200000 <= amount <= 4800000
        if num != 612:
            assert d["shipped_date"] == r["invoice_date"]
            assert 0 <= (date.fromisoformat(d["signed_date"]) - date.fromisoformat(r["invoice_date"])).days <= 5
            if r["invoice_date"] >= "2026-06-10":
                assert d["signed_date"] <= "2026-06-30"
        t = collected.get(r["id"])
        if t:
            assert int(t["amount_cents"]) == amount
        if num <= 518:
            assert t and t["txn_date"] <= "2026-06-26"
            assert entries["CR-" + r["id"]][0]["posting_date"] == t["txn_date"]
        elif num == 547:
            assert t["txn_date"] == "2026-07-02" and entries["CR-" + r["id"]][0]["posting_date"] == "2026-06-30"
        elif 519 <= num <= 524:
            assert t and t["txn_date"] > "2026-06-30" and "CR-" + r["id"] not in entries
        else:
            assert t is None
        if "CR-" + r["id"] not in entries:
            unpaid += amount
    assert unpaid == 202500000
    for account, opening, ending in [("BA-01", 115000000, 120840000), ("BA-02", 38600000, 40000000)]:
        running = opening
        end = None
        for r in sorted((r for r in b if r["bank_account_id"] == account), key=lambda r: r["id"]):
            running += int(r["amount_cents"])
            assert running == int(r["balance_after_cents"])
            assert working(date.fromisoformat(r["txn_date"]))
            if r["txn_date"] <= "2026-06-30":
                end = running
                if account == "BA-01":
                    assert running >= 10000000
                matches = [
                    v
                    for v in j
                    if v["account_code"] == ("1010" if account == "BA-01" else "1020")
                    and v["source_doc"] == r["reference"]
                    and v["posting_date"] == r["txn_date"]
                    and int(v["debit_cents"]) - int(v["credit_cents"]) == int(r["amount_cents"])
                ]
                assert len(matches) == 1, r["id"]
        assert end == ending
    june26 = [r for r in b if r["bank_account_id"] == "BA-01" and r["txn_date"] <= "2026-06-26"][-1]
    assert int(june26["balance_after_cents"]) == 159895000
    tail = [r for r in b if r["bank_account_id"] == "BA-01" and r["txn_date"] >= "2026-06-27"]
    assert len(tail) == 11
    assert [r["id"] for r in tail if r["txn_date"] > "2026-06-30" and abs(int(r["amount_cents"])) == 5000000] == [
        "BT-01-20260702-01"
    ]
    assert prior["1010"] + movement["1010"] == 125840000 and prior["1020"] + movement["1020"] == 40000000
    manifest = json.loads((root / "manifest.json").read_text())
    spec = json.loads((ROOT / "demo_spec.json").read_text())
    for item in manifest["files"]:
        assert hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert [d["page_count"] for d in manifest["documents"]] == [2, 2, 3, 3, 4]
    for item in manifest["documents"]:
        with pdfplumber.open(root / item["path"]) as pdf:
            texts = [p.extract_text() for p in pdf.pages]
            assert len(texts) == item["page_count"] and all(texts)
            name = Path(item["path"]).stem
            expected = (
                spec["contract1"]
                if name == "CON-001"
                else spec["contract3"]
                if name == "CON-003"
                else spec["confirmation1"]
                if name == "12345601"
                else None
            )
            if expected:
                for text, page in zip(texts, expected, strict=True):
                    import re

                    for token in re.findall(r"\b\d+\.\d+|\b\d{1,3}(?:,\d{3})+\.\d{2}", page):
                        assert token in text, (name, token)
    if determinism:
        with TemporaryDirectory() as directory:
            other = generate(Path(directory))
            for file in root.rglob("*"):
                if file.is_file():
                    assert file.read_bytes() == (other / file.relative_to(root)).read_bytes(), file


if __name__ == "__main__":
    check()
    print("All demo data checks passed (including deterministic regeneration).")
