"""Deterministic synthetic FY2026 dataset; run with uv run python data/generate_demo.py."""

import argparse
import calendar
import csv
import hashlib
import json
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.pdfgen.canvas import Canvas

SEED = 20260630
ROOT = Path(__file__).resolve().parent
HOLIDAYS = {
    date.fromisoformat(d) for d in ("2025-12-25", "2025-12-26", "2026-01-01", "2026-01-26", "2026-04-03", "2026-04-06")
}


def working(day):
    return day.weekday() < 5 and day not in HOLIDAYS


def shift(day, direction=1):
    while not working(day):
        day += timedelta(days=direction)
    return day


def cents(value):
    return int(Decimal(value.replace(",", "").replace("−", "-")) * 100)


def generate(destination=ROOT / "demo"):
    rng = random.Random(SEED)
    spec = json.loads((ROOT / "demo_spec.json").read_text())
    destination.mkdir(parents=True, exist_ok=True)

    def write_json(name, value):
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for name, key in [
        ("client_profile", "profile"),
        ("registry/abn", "abn"),
        ("registry/asic", "asic"),
        ("planning/audit_objectives", "objectives"),
        ("planning/risk_register", "risks"),
    ]:
        write_json(name + ".json", spec[key])
    write_json(
        "confirmations/receipts.json", {n: {"receipt_channel": "direct_email"} for n in ("12345601", "23456702")}
    )
    tables = {}
    accounts = []
    tb = []
    for row in spec["tb"]:
        if not row[0]:
            continue
        code, name, current, prior, *_ = row
        kind = {"1": "asset", "2": "liability", "3": "equity", "4": "revenue"}.get(code[0], "expense")
        accounts.append(dict(code=code, name=name, type=kind))
        tb.extend(
            dict(period=p, account_code=code, balance_cents=cents(v))
            for p, v in [("current", current), ("prior", prior)]
        )
    tables["accounts"] = accounts
    tables["trial_balance"] = tb
    customers = {r[0]: r for r in spec["customers"]}
    pool = list(customers)[3:]
    fixed = {int(r[0][-4:]): r for r in spec["fixed"]}
    invoices = []
    monthly_totals = [
        880000,
        900000,
        940000,
        980000,
        1040000,
        860000,
        800000,
        940000,
        1110000,
        975000,
        1075000,
        1500000,
    ]
    segments = [
        (2025, 7, 1, 31, 46, 880000, 2),
        (2025, 8, 1, 31, 47, 900000, 2),
        (2025, 9, 1, 30, 49, 940000, 2),
        (2025, 10, 1, 31, 51, 980000, 2),
        (2025, 11, 1, 30, 54, 1040000, 2),
        (2025, 12, 1, 31, 45, 860000, 2),
        (2026, 1, 1, 31, 42, 800000, 2),
        (2026, 2, 1, 28, 49, 940000, 2),
        (2026, 3, 1, 31, 58, 1110000, 2),
        (2026, 4, 1, 30, 51, 975000, 2),
        (2026, 5, 1, 15, 26, 500000, 1),
        (2026, 5, 18, 29, 22, 575000 - 180500, 1),
        (2026, 6, 1, 26, 64, 0, 2),
        (2026, 6, 29, 29, 6, 0, 0),
        (2026, 6, 30, 30, 5, 0, 0),
    ]
    # June's 75 filler invoices share the residual after the two fixed cases.
    june_total = (1500000 - 142000 - 95000) * 100
    june_amounts = [june_total // 75] * 75
    june_amounts[-1] += june_total - sum(june_amounts)
    available = iter(i for i in range(1, 625) if i not in fixed)
    june_offset = 0
    for y, m, lo, hi, n, total, galah_count in segments:
        days = [date(y, m, d) for d in range(lo, hi + 1) if working(date(y, m, d))]
        dates = sorted(rng.choices(days, k=n))
        if m == 6:
            amounts = june_amounts[june_offset : june_offset + n]
            june_offset += n
        else:
            amounts = [total * 100 // n] * n
            amounts[-1] += total * 100 - sum(amounts)
        galah = set(rng.sample(range(n), galah_count))
        for index, (day, amount) in enumerate(zip(dates, amounts, strict=True)):
            num = next(available)
            customer = list(customers)[2] if index in galah else rng.choice(pool)
            invoices.append(
                dict(
                    id=f"SI-2026-{num:04d}",
                    invoice_no=f"SI-2026-{num:04d}",
                    customer=customer,
                    customer_abn=customers[customer][1],
                    contract_id="CON-003" if index in galah else None,
                    invoice_date=day,
                    posting_date=day,
                    amount_cents=amount,
                    journal_entry_id=f"SJ-2026-{num:04d}-2",
                )
            )
    for row in fixed.values():
        id_, customer, abn, contract, day, amount, journal = row
        invoices.append(
            dict(
                id=id_,
                invoice_no=id_,
                customer=customer,
                customer_abn=abn,
                contract_id=contract or None,
                invoice_date=date.fromisoformat(day),
                posting_date=date.fromisoformat(day),
                amount_cents=int(amount),
                journal_entry_id=journal,
            )
        )
    invoices.sort(key=lambda r: r["id"])
    deliveries = []
    for inv in invoices:
        num = int(inv["id"][-4:])
        day = inv["posting_date"]
        choices = [day + timedelta(days=i) for i in range(6) if working(day + timedelta(days=i))][:4]
        signed = rng.choice(choices)
        if day >= date(2026, 6, 10):
            signed = min(signed, date(2026, 6, 30))
        if num in range(519, 525):
            signed = date(2026, 5, 19)
        if num == 547:
            signed = date(2026, 6, 1)
        if num == 618:
            signed = date(2026, 6, 30)
        if num == 612:
            day = date(2026, 7, 1)
            signed = date(2026, 7, 3)
        id_ = f"DN-2026-{num:04d}"
        deliveries.append(
            dict(
                id=id_,
                delivery_no=id_,
                invoice_id=inv["id"],
                shipped_date=day,
                signed_date=signed,
                signed_by=customers[inv["customer"]][2],
            )
        )
    tables["sales_invoices"] = invoices
    tables["delivery_notes"] = deliveries
    journals = []
    transactions = []

    def journal(
        entry,
        day,
        debit,
        credit,
        amount,
        description,
        source=None,
        creator="fin.controller",
        approver="cfo",
        stamp=None,
    ):
        timestamp = stamp or datetime(
            day.year, day.month, day.day, rng.randrange(9, 17), rng.randrange(60), tzinfo=ZoneInfo("Australia/Sydney")
        ).astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
        for n, code, dr, cr in [(1, debit, amount, 0), (2, credit, 0, amount)]:
            journals.append(
                dict(
                    id=f"{entry}-{n}",
                    entry_no=entry,
                    posting_date=day,
                    created_at=timestamp,
                    created_by=creator,
                    approved_by=approver,
                    account_code=code,
                    debit_cents=dr,
                    credit_cents=cr,
                    description=description,
                    source_doc=source,
                    is_adjustment=False,
                )
            )

    def bank(day, amount, description, reference, account="BA-01", order=0):
        transactions.append(
            dict(
                bank_account_id=account,
                txn_date=day,
                amount_cents=amount,
                description=description,
                reference=reference,
                _order=order,
            )
        )

    def receipt(inv, day, book_day=None):
        bank(day, inv["amount_cents"], "EFT CREDIT " + inv["customer"].upper(), inv["id"])
        book_day = book_day or day
        if book_day <= date(2026, 6, 30):
            special = inv["id"] == "SI-2026-0547"
            journal(
                "CR-" + inv["id"],
                book_day,
                "1010",
                "1100",
                inv["amount_cents"],
                "Receipt "
                + inv["id"]
                + " - "
                + inv["customer"]
                + (" (remittance advice 30 June 2026)" if special else ""),
                inv["id"],
                "ar.clerk",
                "fin.controller",
                "2026-06-30T06:40:00Z" if special else None,
            )

    for inv in invoices:
        num = int(inv["id"][-4:])
        stamp = None
        if num == 612:
            stamp = "2026-06-28T11:14:00Z"
        elif num in [547, 618] or 519 <= num <= 524:
            stamp = inv["posting_date"].isoformat() + "T00:00:00Z"
        journal(
            "SJ-2026-" + inv["id"][-4:],
            inv["posting_date"],
            "1100",
            "4100",
            inv["amount_cents"],
            "Sales invoice " + inv["id"] + " - " + inv["customer"],
            inv["id"],
            "fin.controller" if num == 612 else "ar.clerk",
            "fin.controller",
            stamp,
        )
        if num <= 518:
            receipt(inv, shift(inv["posting_date"] + timedelta(days=rng.randint(20, 42))))
        elif num == 547:
            receipt(inv, date(2026, 7, 2), date(2026, 6, 30))
        elif 519 <= num <= 524:
            receipt(inv, date(2026, 7, [1, 3, 6, 8, 9, 10][num - 519]))
    old_days = [
        date(2025, 7, 1) + timedelta(days=i) for i in range(60) if working(date(2025, 7, 1) + timedelta(days=i))
    ]
    for i in range(80):
        receipt(
            dict(id=f"SI-2025-{541 + i:04d}", customer=rng.choice(pool), amount_cents=1875000),
            old_days[i * len(old_days) // 80],
        )
    months = [(2025, m) for m in range(7, 13)] + [(2026, m) for m in range(1, 7)]
    interest = [1150, 1160, 1160, 1170, 1160, 1170, 1170, 1160, 1170, 1160, 1180, 1190]

    def payment(day, amount, description, reference, debit, order):
        bank(day, -amount, description, reference, order=order)
        if day <= date(2026, 6, 30):
            journal("CP-" + reference, day, debit, "1010", amount, description, reference, "ap.clerk", "cfo")

    for i, (y, m) in enumerate(months):
        tag = f"{y}-{m:02d}"
        last = shift(date(y, m, calendar.monthrange(y, m)[1]), -1)
        pay = (95674 if i == 11 else 95666) * 100
        for day, amount, desc, ref, code, order in [
            (shift(date(y, m, 1)), 2600000, "EFT DEBIT EXAMPLE INDUSTRIAL ESTATES PTY LTD", "RENT-" + tag, "6200", 1),
            (
                shift(date(y, m, 15)),
                35500000,
                "EFT DEBIT KOALA SEMICONDUCTOR SUPPLY PTY LTD",
                "AP-" + tag + "-A",
                "2100",
                2,
            ),
            (shift(date(y, m, 20), -1), pay, "PAYROLL NUMBAT ELECTRONIC COMPONENTS", "PAY-" + tag, "6100", 3),
            (last, 35500000, "EFT DEBIT TASMAN PASSIVE COMPONENTS PTY LTD", "AP-" + tag + "-B", "2100", 4),
            (last, 2000000, "EFT DEBIT REDGUM FREIGHT PTY LTD", "FRT-" + tag, "6300", 5),
            (last, 1555000, "EFT DEBIT SUNDRY SUPPLIERS", "OPEX-" + tag, "6500", 6),
        ]:
            payment(day, amount, desc, ref, code, order)
        cogs = monthly_totals[i] * 70
        journal(f"GJ-{y}{m:02d}-COGS", last, "5100", "1200", cogs, "Cost of goods sold")
        journal(
            f"GJ-{y}{m:02d}-PUR", last, "1200", "2100", cogs + (2917400 if i == 11 else 2916600), "Inventory purchases"
        )
        journal(f"GJ-{y}{m:02d}-INT", last, "1020", "4200", interest[i] * 100, "Savings interest", "INT-" + tag)
        bank(last, interest[i] * 100, "CREDIT INTEREST", "INT-" + tag, "BA-02")
    for day, amount, ref, code, desc in [
        ("2025-10-28", 100000, "ATO-PAYG-Q1", "2400", "ATO PAYMENT"),
        ("2026-02-27", 130000, "ATO-FY25-BAL", "2400", "ATO PAYMENT"),
        ("2026-02-27", 100000, "ATO-PAYG-Q2", "2400", "ATO PAYMENT"),
        ("2026-04-28", 100000, "ATO-PAYG-Q3", "2400", "ATO PAYMENT"),
        ("2025-11-20", 400000, "DIV-2025-11", "3200", "EFT DEBIT DIVIDEND DISTRIBUTION"),
        ("2025-10-15", 130000, "PPE-2025-10", "1500", "EFT DEBIT IRONBARK RACKING PTY LTD"),
    ]:
        payment(date.fromisoformat(day), amount * 100, desc, ref, code, 7)
    payment(date(2026, 7, 1), 2600000, "EFT DEBIT EXAMPLE INDUSTRIAL ESTATES PTY LTD", "RENT-2026-07", "6200", 1)
    for code, dr, cr, amount, desc in [
        ("DEP", "6400", "1510", 160000, "Depreciation FY2026"),
        ("ACC", "6500", "2200", 42000, "Increase in accrued expenses"),
        ("PRE", "1300", "6500", 16600, "Increase in prepayments"),
        ("ECL", "6500", "1150", 10000, "Increase in ECL allowance"),
        ("LVE", "6100", "2300", 32000, "Increase in annual leave provision"),
        ("TAX", "8100", "2400", 450000, "Income tax expense FY2026"),
    ]:
        journal("GJ-202606-" + code, date(2026, 6, 30), dr, cr, amount * 100, desc)
    transactions.sort(key=lambda r: (r["bank_account_id"], r["txn_date"], r["_order"], r["reference"]))
    balances = {"BA-01": 115000000, "BA-02": 38600000}
    counters = {}
    for txn in transactions:
        account = txn["bank_account_id"]
        key = (account, txn["txn_date"])
        counters[key] = counters.get(key, 0) + 1
        txn["id"] = f"BT-{account[-2:]}-{txn['txn_date']:%Y%m%d}-{counters[key]:02d}"
        balances[account] += txn["amount_cents"]
        txn["balance_after_cents"] = balances[account]
        del txn["_order"]
    tables["bank_accounts"] = [
        dict(
            id="BA-01",
            bank_name="Harbourline Bank Limited",
            bsb="999-101",
            account_no="12345601",
            gl_account_code="1010",
        ),
        dict(
            id="BA-02",
            bank_name="Bluegum Savings Bank Limited",
            bsb="999-202",
            account_no="23456702",
            gl_account_code="1020",
        ),
    ]
    tables["bank_transactions"] = transactions
    tables["journal_entries"] = sorted(journals, key=lambda r: r["id"])
    from backend.contracts import records

    classes = {
        "accounts": records.Account,
        "trial_balance": records.TrialBalanceRow,
        "journal_entries": records.JournalEntry,
        "sales_invoices": records.SalesInvoice,
        "delivery_notes": records.DeliveryNote,
        "bank_accounts": records.BankAccount,
        "bank_transactions": records.BankTransaction,
    }
    for name, rows in tables.items():
        with (destination / (name + ".csv")).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(classes[name].model_fields), lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: str(v).lower() if isinstance(v, bool) else v for k, v in row.items()})

    def pdf(name, pages, footer):
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        canvas = Canvas(str(path), invariant=1, pagesize=(595, 842))
        for n, page in enumerate(pages, 1):
            text = canvas.beginText(40, 795)
            text.setFont("Courier", 9)
            text.setLeading(13)
            for line in page.strip().splitlines():
                text.textLine(line)
            canvas.drawText(text)
            canvas.setFont("Helvetica", 8)
            canvas.drawString(40, 35, footer + f" | Page {n} of {len(pages)}")
            canvas.showPage()
        canvas.save()

    c1 = spec["contract1"]
    c2 = list(c1)
    replacements = {
        "CON-001": "CON-002",
        "20 May 2026": "25 May 2026",
        "Bilby Robotics": "Quoll Automation",
        "11 901 043 383": "46 902 044 759",
        "15 Sample Road": "8 Demo Street",
        "Wetherill Park NSW 2164": "Dandenong South VIC 3175",
        "142,000.00": "95,000.00",
        "Servo drive controller module SDC-400        200            560.00    112,000.00": (
            "Industrial Ethernet switch module IES-8     250            280.00     70,000.00"
        ),
        "Encoder interface board EIB-12               300            100.00     30,000.00": (
            "Power supply unit PSU-24V                   500             50.00     25,000.00"
        ),
        "laws of New South Wales": "laws of Victoria",
        "Mei Tran, Director": "Oliver Grant, Managing Director",
    }
    for old, new in replacements.items():
        c2 = [p.replace(old, new) for p in c2]
    pdf("contracts/CON-001.pdf", c1, "Contract No. CON-001")
    pdf("contracts/CON-002.pdf", c2, "Contract No. CON-002")
    pdf("contracts/CON-003.pdf", spec["contract3"], "Contract No. CON-003")
    b1 = spec["confirmation1"]
    b2 = list(b1)
    replacements = {
        "HARBOURLINE BANK LIMITED": "BLUEGUM SAVINGS BANK LIMITED",
        "Harbourline Bank Limited": "Bluegum Savings Bank Limited",
        "82 920 069 414": "30 921 070 788",
        "HBL-AC-2026-004512": "BGS-AUD-26-00871",
        "999-101": "999-202",
        "12345601": "23456702",
        "1,208,400.00": "400,000.00",
        "Business Transaction Account": "Business Savings Account",
        "Numbat Electronic Components Pty Ltd - Operating Account": (
            "Numbat Electronic Components Pty Ltd - Business Saver"
        ),
    }
    for old, new in replacements.items():
        b2 = [p.replace(old, new) for p in b2]
    import re

    b2 = [re.sub(r"Authorised officer:.*", "Authorised officer: Marcus Hale, Confirmations Team", p) for p in b2]
    b2 = [
        re.sub(r"Audit Confirmations Unit,.*", "Confirmations Team, 55 Sample Lane, Melbourne VIC 3000", p) for p in b2
    ]
    b2 = [re.sub(r"Accrued interest.*", "Interest of AUD 1,190.00 was credited on 30 June 2026.", p) for p in b2]
    pdf("confirmations/12345601.pdf", b1, "Harbourline Bank Limited | Ref HBL-AC-2026-004512")
    pdf("confirmations/23456702.pdf", b2, "Bluegum Savings Bank Limited | Ref BGS-AUD-26-00871")
    import pdfplumber

    files = []
    documents = []
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        item = dict(path=path.relative_to(destination).as_posix(), sha256=sha)
        if path.suffix == ".csv":
            item["rows"] = len(tables[path.stem])
        files.append(item)
        if path.suffix == ".pdf":
            with pdfplumber.open(path) as doc:
                count = len(doc.pages)
            documents.append(
                dict(
                    **item,
                    type="contract" if path.parent.name == "contracts" else "confirmation",
                    page_count=count,
                    document_id="DOC-" + sha[:12],
                )
            )
    # Resolve A's deliberately synthetic test document IDs against the generated PDFs.
    aliases = {f"DOC-CON00{n}000000": f"contracts/CON-00{n}.pdf" for n in (1, 2, 3)}
    aliases.update({"DOC-RPLBA0100000": "confirmations/12345601.pdf", "DOC-RPLBA0200000": "confirmations/23456702.pdf"})
    document_ids = {d["path"]: d["document_id"] for d in documents}
    fixtures = ROOT.parent / "tests" / "fixtures" / "llm"
    for source in sorted(fixtures.rglob("*.json")):
        raw = source.read_text(encoding="utf-8")
        for alias, name in aliases.items():
            raw = raw.replace(alias, document_ids[name])
        target = destination / "llm" / source.relative_to(fixtures)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw, encoding="utf-8")
    # Include runtime fixtures in the manifest, without duplicate entries on regeneration.
    files = [f for f in files if not f["path"].startswith("llm/")]
    for target in sorted((destination / "llm").rglob("*.json")):
        files.append(
            dict(
                path=target.relative_to(destination).as_posix(), sha256=hashlib.sha256(target.read_bytes()).hexdigest()
            )
        )
    files.sort(key=lambda f: f["path"])
    write_json("manifest.json", dict(dataset="demo", seed=SEED, files=files, documents=documents))
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "demo")
    args = parser.parse_args()
    generate(args.output)
    from check_demo import check

    check(args.output)
    print(f"Demo dataset generated and verified: {args.output}")
