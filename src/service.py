"""Application layer between the engine and the web UI. Pure Python so it can be tested without a browser."""
import csv
import io
import json
from pathlib import Path

from . import cli, matcher
from .generate_data import generate
from .store import Store

SETTINGS_PATH = Path("settings.json")
DB_PATH = cli.REPORTS / "review.db"
INVOICE_KINDS = ["MISSING_INVOICE", "AMOUNT_MISMATCH", "DUPLICATE_INVOICE", "ORPHAN_INVOICE", "CANCELLED_INVOICED"]
CARD_KINDS = ["POSSIBLE_MATCH", "MISSING_ENTRY", "DUPLICATE_ENTRY", "LEDGER_ONLY"]
FRIENDLY = {
    "POSSIBLE_MATCH": "Possible match", "MISSING_ENTRY": "Charge not in books", "DUPLICATE_ENTRY": "Duplicate entry",
    "LEDGER_ONLY": "Entry with no card charge", "MISSING_INVOICE": "Shipped, no invoice",
    "AMOUNT_MISMATCH": "Invoice amount differs", "DUPLICATE_INVOICE": "Duplicate invoice",
    "ORPHAN_INVOICE": "Invoice for unknown order", "CANCELLED_INVOICED": "Cancelled but invoiced"}

TEMPLATES = {
    "card_statement.csv": ["txn_id", "post_date", "description", "amount"],
    "qb_ledger.csv": ["entry_id", "date", "vendor", "amount", "category", "memo"],
    "orders.csv": ["order_id", "order_date", "channel", "customer", "total", "status"],
    "qb_invoices.csv": ["invoice_no", "order_ref", "invoice_date", "customer", "total"],
}
NUMERIC = {"card_statement.csv": ["amount"], "qb_ledger.csv": ["amount"], "orders.csv": ["total"], "qb_invoices.csv": ["total"]}
DATES = {"card_statement.csv": ["post_date"], "qb_ledger.csv": ["date"], "orders.csv": ["order_date"], "qb_invoices.csv": ["invoice_date"]}


# ---------- settings (editable by staff, no code) ----------
def load_settings():
    s = {"auto_threshold": matcher.AUTO_THRESHOLD, "aliases": {}}
    if SETTINGS_PATH.exists():
        s.update(json.loads(SETTINGS_PATH.read_text()))
    return s


def save_settings(s):
    SETTINGS_PATH.write_text(json.dumps(s, indent=1))


def apply_settings(s=None):
    s = s or load_settings()
    matcher.AUTO_THRESHOLD = float(s["auto_threshold"])
    matcher.ALIASES.update({k.upper(): v.upper() for k, v in s["aliases"].items()})


def learn_alias(descriptor: str, ledger_vendor: str):
    """Remember that a card descriptor and a ledger name mean the same vendor. Returns the alias or None."""
    nd, nv = matcher.normalize_vendor(descriptor), matcher.normalize_vendor(ledger_vendor)
    if not nd or not nv or nd == nv or len(nd.split()) != 1:
        return None
    s = load_settings()
    s["aliases"][nd] = nv
    save_settings(s)
    apply_settings(s)
    return {nd: nv}


# ---------- data in ----------
def validate_csv(name: str, raw: bytes):
    """Return (ok, message). Plain-English errors for the person uploading."""
    try:
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    except Exception:
        return False, f"{name}: could not read this file as a CSV."
    if not rows:
        return False, f"{name}: the file has no data rows."
    missing = [c for c in TEMPLATES[name] if c not in rows[0]]
    if missing:
        return False, f"{name}: missing column(s) {', '.join(missing)}. Download the template to see the expected headers."
    for i, r in enumerate(rows[:2000], start=2):
        for c in NUMERIC[name]:
            try:
                float(r[c])
            except ValueError:
                return False, f"{name}: row {i}, column '{c}' is not a number ({r[c]!r})."
        for c in DATES[name]:
            try:
                cli.date.fromisoformat(r[c])
            except ValueError:
                return False, f"{name}: row {i}, column '{c}' should look like 2026-08-31 (got {r[c]!r})."
    return True, f"{name}: {len(rows)} rows look good."


def template_csv(name: str) -> bytes:
    return (",".join(TEMPLATES[name]) + "\n").encode()


def install_uploads(files: dict):
    cli.DATA.mkdir(exist_ok=True)
    for name, raw in files.items():
        (cli.DATA / name).write_bytes(raw)


def run_demo_data(seed=42):
    generate(str(cli.DATA), seed)
    return run_reconciliation(has_truth=True)


def run_reconciliation(has_truth=False):
    apply_settings()
    card, proposals, findings, store = cli.reconcile()
    ev = cli.evaluate(card, proposals, findings) if has_truth else None
    return {"auto": len(card["auto"]), "review": len(card["review"]), "evaluation": ev}


# ---------- actions ----------
def decide_many(store, ids, decision, reviewer, note=""):
    done = 0
    for i in ids:
        try:
            store.decide(i, decision, reviewer, note)
            done += 1
        except ValueError:
            pass  # already decided by someone else
    return done


def high_confidence_ids(store, kind, min_conf):
    return [i["id"] for i in store.items("pending", kind) if i["confidence"] is not None and i["confidence"] >= min_conf]


def export_csv(store, reviewer="system", log=True) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Vendor", "Amount", "Account", "Memo"])
    n = 0
    for i in store.items("approved", "MISSING_ENTRY"):
        p = json.loads(i["payload"])
        t = p["txn"]
        w.writerow([t["date"], p["proposed_vendor"], t["amount"], p["proposed_category"], f"card {t['txn_id']}"])
        n += 1
    if log:
        store.log(reviewer, "export_downloaded", detail={"rows": n})
    return out.getvalue().encode()


def summary(store):
    audit = store.audit()
    start = next((json.loads(a["detail"]) for a in reversed(audit) if a["action"] == "run_started"), None)
    auto = sum(a["action"] == "auto_matched" for a in audit)
    items = store.items()
    by_status = {s: sum(i["status"] == s for i in items) for s in ("pending", "approved", "rejected")}
    unrecorded = sum(json.loads(i["payload"])["txn"]["amount"] for i in items if i["kind"] == "MISSING_ENTRY")
    inv_pending = [json.loads(i["payload"]) for i in items if i["kind"] in INVOICE_KINDS and i["status"] == "pending"]
    return {"run": start, "auto": auto, "by_status": by_status, "total_items": len(items), "unrecorded": unrecorded,
            "invoice_pending_dollars": sum(abs(f["delta"]) if f["kind"] != "MISSING_INVOICE" else f["expected"] for f in inv_pending),
            "pending_by_kind": {FRIENDLY[k]: sum(1 for i in items if i["kind"] == k and i["status"] == "pending")
                                for k in CARD_KINDS + INVOICE_KINDS}}


def why(item):
    """Plain-English explanation shown under every row."""
    p = json.loads(item["payload"])
    k = item["kind"]
    if k == "POSSIBLE_MATCH":
        pt = p["parts"]
        return (f"{p['reason']}. Amount fit {pt['amount']:.0%}, vendor name fit {pt['vendor']:.0%}, date fit {pt['date']:.0%}. "
                f"Card posted {p['txn']['date']}, ledger entry dated {p['entry']['date']}.")
    if k == "MISSING_ENTRY":
        return (f"No ledger entry matched this charge. Suggested category '{p['proposed_category']}' came from "
                f"{'a built-in rule' if p['category_source'] == 'rule' else 'the AI assistant'}; confidence {item['confidence']:.0%}.")
    if k == "DUPLICATE_ENTRY":
        return f"Same vendor, amount and date as {p['duplicate_of']}, which already explains the card charge."
    if k == "LEDGER_ONLY":
        return "This ledger line has no card charge. It may be a bank transfer, cash or payroll entry."
    return p["detail"].capitalize() + f". Expected ${p['expected']:.2f}, found ${p['actual']:.2f}."
