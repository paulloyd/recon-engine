"""Command line entry point.

  python -m src.cli demo                 generate data, reconcile, write report
  python -m src.cli review [--kind K]    list pending review items
  python -m src.cli decide ID approve|reject [--reviewer NAME] [--note TEXT]
  python -m src.cli export               write approved new entries as a QuickBooks-style import CSV
  python -m src.cli audit                print the audit trail
"""
import argparse
import csv
import json
import os
from datetime import date
from pathlib import Path

from .categorize import categorize
from .generate_data import generate
from .invoice_recon import reconcile_invoices
from .matcher import match_card, normalize_vendor
from .store import Store

DATA, REPORTS = Path("data"), Path("reports")


def _read(path, floats=(), dates=()):
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        for f in floats:
            r[f] = float(r[f])
        for d in dates:
            r[d] = date.fromisoformat(r[d])
    return rows


def load():
    st = _read(DATA / "card_statement.csv", ["amount"], ["post_date"])
    for t in st:
        t["date"] = t.pop("post_date")
    lg = _read(DATA / "qb_ledger.csv", ["amount"], ["date"])
    orders = _read(DATA / "orders.csv", ["total"])
    invoices = _read(DATA / "qb_invoices.csv", ["total"])
    return st, lg, orders, invoices


def _ser(obj):
    return json.loads(json.dumps(obj, default=str))


def reconcile(use_llm=False):
    st, lg, orders, invoices = load()
    REPORTS.mkdir(exist_ok=True)
    dbp = REPORTS / "review.db"
    if dbp.exists():
        dbp.unlink()  # each run is a fresh batch; the audit log inside a batch is append-only
    store = Store(str(dbp))
    store.log("engine", "run_started", detail={"statement_lines": len(st), "ledger_entries": len(lg),
                                               "orders": len(orders), "invoices": len(invoices)})
    card = match_card(st, lg)
    for m in card["auto"]:
        store.log("engine", "auto_matched", detail={"txn": m["txn"]["txn_id"], "entry": m["entry"]["entry_id"],
                                                    "score": m["score"]})
    for m in card["review"]:
        t, e = m["txn"], m["entry"]
        store.add_item("POSSIBLE_MATCH", "card", f"{t['txn_id']} {t['description']} ${t['amount']:.2f} ~ "
                       f"{e['entry_id']} {e['vendor']} ${e['amount']:.2f}  ({m['reason']})",
                       _ser(m), m["score"])
    proposals = []
    for t in card["unmatched_txns"]:
        cat, conf, src = categorize(t["description"], t["amount"], use_llm)
        vendor = normalize_vendor(t["description"]).title() or t["description"]
        p = {"txn": t, "proposed_vendor": vendor, "proposed_category": cat, "category_source": src}
        proposals.append((p, conf))
        store.add_item("MISSING_ENTRY", "card", f"{t['txn_id']} ${t['amount']:.2f} {t['description']} has no ledger "
                       f"entry -> propose {cat} ({src}, conf {conf:.2f})", _ser(p), conf)
    for d in card["duplicates"]:
        e = d["entry"]
        store.add_item("DUPLICATE_ENTRY", "card", f"{e['entry_id']} {e['vendor']} ${e['amount']:.2f} duplicates "
                       f"{d['duplicate_of']}", _ser(d), 0.9)
    for d in card["ledger_only"]:
        e = d["entry"]
        store.add_item("LEDGER_ONLY", "card", f"{e['entry_id']} {e['vendor']} ${e['amount']:.2f} has no card "
                       f"transaction (bank/other?)", _ser(d), None)
    findings = reconcile_invoices(orders, invoices)
    for f in findings:
        store.add_item(f["kind"], "invoice", f"{f['order_id']}: {f['detail']} (expected ${f['expected']:.2f}, "
                       f"actual ${f['actual']:.2f}, severity {f['severity']})", _ser(f), 1.0)
    store.log("engine", "run_finished", detail={"auto": len(card["auto"]), "review": len(card["review"])})
    return card, proposals, findings, store


def _prf(pred: set, truth: set):
    tp = len(pred & truth)
    return (tp / len(pred) if pred else 1.0, tp / len(truth) if truth else 1.0)


def evaluate(card, proposals, findings):
    truth = json.load(open(DATA / "truth.json"))
    ct, it = truth["card"], truth["invoices"]
    ok = lambda m: m["entry"]["entry_id"] in ct["entries"][m["txn"]["txn_id"]]
    auto_correct = sum(ok(m) for m in card["auto"])
    found = sum(ok(m) for m in card["auto"] + card["review"])
    matchable = sum(1 for v in ct["entries"].values() if v)
    miss_p, miss_r = _prf({t["txn_id"] for t in card["unmatched_txns"]}, set(ct["missing_txns"]))
    twin_ids = {i for t in ct["duplicate_txns"] for i in ct["entries"][t]}
    flagged = {d["entry"]["entry_id"] for d in card["duplicates"]}
    dup_hit = sum(1 for t in ct["duplicate_txns"] if set(ct["entries"][t]) & flagged)
    cats = [(p["proposed_category"] == ct["category"][p["txn"]["txn_id"]], c) for p, c in proposals]
    hi = [ok_ for ok_, c in cats if c >= 0.9]
    inv = {}
    for kind, ids in it.items():
        pred = {f["order_id"] for f in findings if f["kind"] == kind}
        inv[kind] = _prf(pred, set(ids))
    return {
        "auto_precision": auto_correct / len(card["auto"]) if card["auto"] else 1.0,
        "match_recall": found / matchable,
        "missing_precision": miss_p, "missing_recall": miss_r,
        "dup_recall": dup_hit / len(ct["duplicate_txns"]) if ct["duplicate_txns"] else 1.0,
        "dup_precision": len(flagged & twin_ids) / len(flagged) if flagged else 1.0,
        "category_accuracy_all": sum(o for o, _ in cats) / len(cats) if cats else 1.0,
        "category_accuracy_high_conf": sum(hi) / len(hi) if hi else 1.0,
        "category_high_conf_share": len(hi) / len(cats) if cats else 0.0,
        "invoice": inv,
    }


def write_report(card, proposals, findings, store, ev):
    st, lg, orders, invoices = load()
    n = len(st)
    auto, rev = len(card["auto"]), len(card["review"])
    unmatched = len(card["unmatched_txns"])
    by_kind = {}
    for f in findings:
        by_kind.setdefault(f["kind"], []).append(f)
    L = ["# Reconciliation report (synthetic data)", "",
         "## Credit card vs QuickBooks ledger", "",
         "| Metric | Value |", "|---|---|",
         f"| Card lines | {n} |", f"| Auto-matched (no human touch) | {auto} ({auto / n:.1%}) |",
         f"| Sent to review as possible/ambiguous match | {rev} |",
         f"| Card charges with no ledger entry | {unmatched} |",
         f"| Duplicate ledger entries flagged | {len(card['duplicates'])} |",
         f"| Ledger lines with no card charge | {len(card['ledger_only'])} |",
         f"| Value of unrecorded charges | ${sum(t['amount'] for t in card['unmatched_txns']):,.2f} |", "",
         "## Order system vs QuickBooks invoices", "",
         "| Finding | Count | Dollar impact |", "|---|---|---|"]
    for k, fs in sorted(by_kind.items()):
        L.append(f"| {k} | {len(fs)} | ${sum(abs(f['delta']) if k != 'MISSING_INVOICE' else f['expected'] for f in fs):,.2f} |")
    L += ["", "## Accuracy against planted defects (synthetic ground truth)", "",
          "| Check | Precision | Recall |", "|---|---|---|",
          f"| Auto-matches correct | {ev['auto_precision']:.1%} | {ev['match_recall']:.1%} (auto + review) |",
          f"| Unrecorded charges found | {ev['missing_precision']:.1%} | {ev['missing_recall']:.1%} |",
          f"| Duplicate entries | {ev['dup_precision']:.1%} | {ev['dup_recall']:.1%} |"]
    for k, (p, r) in ev["invoice"].items():
        L.append(f"| Invoice: {k} | {p:.1%} | {r:.1%} |")
    L += ["", f"Category proposals correct: {ev['category_accuracy_all']:.1%} overall; "
          f"{ev['category_accuracy_high_conf']:.1%} on the {ev['category_high_conf_share']:.0%} of rows with confidence >= 0.90 "
          "(those are the rows a reviewer can bulk-approve).", "",
          "> These numbers come from generated data with planted defects. They show the engine behaves as "
          "designed; they are not a claim about performance on real company books.", "",
          "## Review queue", "", "| Kind | Pending |", "|---|---|"]
    for k in sorted({i["kind"] for i in store.items()}):
        L.append(f"| {k} | {len(store.items('pending', k))} |")
    (REPORTS / "summary.md").write_text("\n".join(L) + "\n")
    with open(REPORTS / "exceptions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "source", "confidence", "summary"])
        for i in store.items():
            w.writerow([i["id"], i["kind"], i["source"], i["confidence"], i["summary"]])


def cmd_demo(a):
    generate(str(DATA), a.seed)
    card, proposals, findings, store = reconcile(a.llm)
    ev = evaluate(card, proposals, findings)
    write_report(card, proposals, findings, store, ev)
    print((REPORTS / "summary.md").read_text())
    print(f"Review queue: {len(store.items('pending'))} pending items -> run: python -m src.cli review")


def cmd_review(a):
    for i in Store(str(REPORTS / "review.db")).items("pending", a.kind)[: a.limit]:
        conf = "" if i["confidence"] is None else f"[{i['confidence']:.2f}] "
        print(f"#{i['id']:<4} {i['kind']:<18} {conf}{i['summary']}")


def cmd_decide(a):
    Store(str(REPORTS / "review.db")).decide(a.id, "approved" if a.decision == "approve" else "rejected",
                                            a.reviewer, a.note)
    print(f"item {a.id} {a.decision}d by {a.reviewer}")


def cmd_export(a):
    store = Store(str(REPORTS / "review.db"))
    rows = [json.loads(i["payload"]) for i in store.items("approved", "MISSING_ENTRY")]
    out = REPORTS / "qb_import_approved.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Vendor", "Amount", "Account", "Memo"])
        for p in rows:
            t = p["txn"]
            w.writerow([t["date"], p["proposed_vendor"], t["amount"], p["proposed_category"], f"card {t['txn_id']}"])
    store.log("engine", "export_written", detail={"rows": len(rows), "file": str(out)})
    print(f"wrote {len(rows)} approved entries to {out} (nothing is posted automatically)")


def cmd_audit(a):
    for r in Store(str(REPORTS / "review.db")).audit()[-a.limit:]:
        print(r["ts"], r["actor"], r["action"], r["item_id"] or "", r["detail"] or "")


def main():
    ap = argparse.ArgumentParser(prog="recon")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo"); p.add_argument("--seed", type=int, default=42)
    p.add_argument("--llm", action="store_true", help="use optional LLM for low-confidence categories")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("review"); p.add_argument("--kind"); p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("decide"); p.add_argument("id", type=int); p.add_argument("decision", choices=["approve", "reject"])
    p.add_argument("--reviewer", default=os.environ.get("USER", "reviewer")); p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_decide)
    sub.add_parser("export").set_defaults(fn=cmd_export)
    p = sub.add_parser("audit"); p.add_argument("--limit", type=int, default=20); p.set_defaults(fn=cmd_audit)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
