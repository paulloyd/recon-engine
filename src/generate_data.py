"""Generate a synthetic, deliberately messy dataset for the reconciliation demo.

Everything here is FAKE. The shapes mimic what a small e-commerce reseller exports:
  * card_statement.csv  - raw credit-card lines with cryptic merchant descriptors
  * qb_ledger.csv       - expense entries as a bookkeeper would key them into QuickBooks
  * orders.csv          - order-management export (SellerCloud-style columns)
  * qb_invoices.csv     - invoices as they exist in the accounting system
  * truth.json          - the planted defects, used ONLY to score the engine honestly
"""
import argparse
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

# (clean ledger name, descriptor templates, category, (min, max), weight)
VENDORS = [
    ("Amazon", ["AMZN MKTP US*{r}", "AMAZON.COM*{r}"], "Inventory Purchases", (20, 900), 14),
    ("Alibaba", ["ALIBABA.COM {n}"], "Inventory Purchases", (150, 4000), 5),
    ("UPS", ["UPS*{r}", "THE UPS STORE #{n}"], "Shipping & Postage", (12, 400), 12),
    ("FedEx", ["FEDEX {n}"], "Shipping & Postage", (15, 350), 8),
    ("USPS", ["USPS PO {n}"], "Shipping & Postage", (5, 120), 6),
    ("Meta Ads", ["FACEBK *{r}"], "Advertising", (50, 1500), 10),
    ("Google Ads", ["GOOGLE *ADS{n}"], "Advertising", (40, 1200), 8),
    ("Staples", ["STAPLES 0{n}"], "Office Supplies", (8, 250), 4),
    ("Uline", ["ULINE *SHIP SUPPLY"], "Packaging Supplies", (60, 1100), 7),
    ("Uber", ["UBER *TRIP {r}"], "Travel & Meals", (8, 60), 3),
    ("Canva", ["CANVA* {r}"], "Software & Subscriptions", (12, 120), 2),
    ("Zoom", ["ZOOM.US {n}"], "Software & Subscriptions", (15, 50), 2),
    ("Kens Supply Co", ["SQ *KENS SUPPLY CO"], "Packaging Supplies", (30, 500), 2),
    ("JHande via PayPal", ["PAYPAL *JHANDE"], "Inventory Purchases", (100, 1500), 2),
]
VARIANTS = {"Amazon": "Amazon Inc.", "UPS": "UPS Store", "Meta Ads": "Facebook Ads", "Google Ads": "Google Adwords",
            "Uline": "U-line Supply", "FedEx": "Federal Express", "Staples": "Staples Business"}
RECURRING = [  # (clean name, descriptor, category, amount, day of month)
    ("QuickBooks", "INTUIT *QBOOKS {r}", "Software & Subscriptions", 90.00, 5),
    ("Google Workspace", "GOOGLE *GSUITE_{r}", "Software & Subscriptions", 144.00, 12),
]
LEDGER_ONLY = [("Rent", 2400.00), ("Payroll Transfer", 6200.00), ("Owner Draw", 1500.00),
               ("Bank Fee", 35.00), ("Sales Tax Payment", 880.00)]


def _rid(rng):
    # always starts with a digit so it behaves like a real random reference code
    return rng.choice("23456789") + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))


def _desc(rng, tpl):
    return tpl.format(r=_rid(rng), n=rng.randint(1000, 9999))


def build_card_and_ledger(rng, n_txn=300, start=date(2026, 7, 1), days=92):
    txns, ledger, truth = [], [], {"entries": {}, "category": {}, "amount_diff": [],
                                   "duplicate_txns": [], "missing_txns": [], "ledger_only": []}
    raw = []
    weights = [v[4] for v in VENDORS]
    for _ in range(n_txn):
        v = rng.choices(VENDORS, weights=weights)[0]
        d = start + timedelta(days=rng.randint(0, days - 1))
        amt = round(rng.uniform(*v[3]), 2)
        raw.append((d, _desc(rng, rng.choice(v[1])), amt, v[0], v[2]))
    for m in range(3):
        for name, tpl, cat, amt, dom in RECURRING:
            raw.append((date(2026, 7 + m, dom), _desc(rng, tpl), amt, name, cat))
    raw.sort(key=lambda r: (r[0], r[1]))

    eid = 1000
    for i, (d, desc, amt, name, cat) in enumerate(raw, 1):
        tid = f"CC-{i:04d}"
        txns.append({"txn_id": tid, "post_date": d.isoformat(), "description": desc, "amount": f"{amt:.2f}"})
        truth["category"][tid] = cat
        roll = rng.random()
        if roll >= 0.92:  # never entered in the books
            truth["missing_txns"].append(tid)
            truth["entries"][tid] = []
            continue
        ledger_amt = amt
        if 0.84 <= roll < 0.89:  # fx fee / tip / rounding: entered with a slightly different amount
            ledger_amt = round(amt * (1 + rng.choice([-1, 1]) * rng.uniform(0.008, 0.025)), 2)
            truth["amount_diff"].append(tid)
        lag = rng.randint(7, 12) if rng.random() < 0.03 else rng.randint(0, 3)  # a few keyed very late
        ed = d + timedelta(days=lag)
        led_name = VARIANTS[name] if name in VARIANTS and rng.random() < 0.12 else name  # bookkeeper naming drift
        eid += 1
        base = {"entry_id": f"QB-{eid}", "date": ed.isoformat(), "vendor": led_name,
                "amount": f"{ledger_amt:.2f}", "category": cat, "memo": ""}
        ledger.append(base)
        ids = [base["entry_id"]]
        if 0.89 <= roll < 0.92:  # keyed twice
            eid += 1
            twin = dict(base, entry_id=f"QB-{eid}", memo="")
            ledger.append(twin)
            ids.append(twin["entry_id"])
            truth["duplicate_txns"].append(tid)
        truth["entries"][tid] = ids

    for name, amt in LEDGER_ONLY:  # real ledger lines that never touch this card
        for _ in range(2):
            eid += 1
            d = start + timedelta(days=rng.randint(0, days - 1))
            ledger.append({"entry_id": f"QB-{eid}", "date": d.isoformat(), "vendor": name,
                           "amount": f"{amt:.2f}", "category": "Bank / Other", "memo": "not a card transaction"})
            truth["ledger_only"].append(f"QB-{eid}")
    ledger.sort(key=lambda e: (e["date"], e["entry_id"]))
    return txns, ledger, truth


def build_orders_and_invoices(rng, n_orders=400, start=date(2026, 7, 1)):
    channels = ["Amazon", "eBay", "Walmart", "Website"]
    orders = []
    for i in range(n_orders):
        r = rng.random()
        status = "Shipped" if r < 0.90 else ("Cancelled" if r < 0.96 else "Pending")
        orders.append({"order_id": f"ORD-{10001 + i}",
                       "order_date": (start + timedelta(days=rng.randint(0, 91))).isoformat(),
                       "channel": rng.choice(channels), "customer": f"Customer {rng.randint(100, 999)}",
                       "total": f"{rng.uniform(15, 600):.2f}", "status": status})
    shipped = [o for o in orders if o["status"] == "Shipped"]
    cancelled = [o for o in orders if o["status"] == "Cancelled"]
    rng.shuffle(shipped)
    missing = {o["order_id"] for o in shipped[:14]}
    mismatch = {o["order_id"] for o in shipped[14:24]}
    dupes = {o["order_id"] for o in shipped[24:30]}
    cancelled_inv = {o["order_id"] for o in cancelled[:4]}
    invoices, n = [], 5000
    for o in orders:
        oid = o["order_id"]
        if o["status"] == "Shipped" and oid not in missing or oid in cancelled_inv:
            total = float(o["total"])
            if oid in mismatch:
                total = round(total + rng.choice([-1, 1]) * rng.uniform(2, 25), 2)
            od = date.fromisoformat(o["order_date"]) + timedelta(days=rng.randint(1, 3))
            copies = 2 if oid in dupes else 1
            for _ in range(copies):
                n += 1
                invoices.append({"invoice_no": f"INV-{n}", "order_ref": oid, "invoice_date": od.isoformat(),
                                 "customer": o["customer"], "total": f"{total:.2f}"})
    orphans = set()
    for k in range(5):
        n += 1
        ref = f"ORD-{99000 + k}"
        orphans.add(ref)
        invoices.append({"invoice_no": f"INV-{n}", "order_ref": ref, "invoice_date": "2026-08-15",
                         "customer": f"Customer {rng.randint(100, 999)}", "total": f"{rng.uniform(20, 300):.2f}"})
    truth = {"MISSING_INVOICE": sorted(missing), "AMOUNT_MISMATCH": sorted(mismatch),
             "DUPLICATE_INVOICE": sorted(dupes), "CANCELLED_INVOICED": sorted(cancelled_inv),
             "ORPHAN_INVOICE": sorted(orphans)}
    return orders, invoices, truth


def _write(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def generate(out_dir="data", seed=42):
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    txns, ledger, card_truth = build_card_and_ledger(rng)
    orders, invoices, inv_truth = build_orders_and_invoices(rng)
    _write(out / "card_statement.csv", txns)
    _write(out / "qb_ledger.csv", ledger)
    _write(out / "orders.csv", orders)
    _write(out / "qb_invoices.csv", invoices)
    (out / "truth.json").write_text(json.dumps({"card": card_truth, "invoices": inv_truth}, indent=1))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    print(f"wrote synthetic data to {generate(a.out, a.seed)}")
