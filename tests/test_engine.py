import sqlite3
from datetime import date

import pytest

from src import cli
from src.invoice_recon import reconcile_invoices
from src.matcher import AUTO_THRESHOLD, amount_score, match_card, normalize_vendor, vendor_similarity
from src.store import Store


def txn(i, desc, amt, d):
    return {"txn_id": i, "description": desc, "amount": amt, "date": d}


def ent(i, vendor, amt, d):
    return {"entry_id": i, "vendor": vendor, "amount": amt, "date": d}


def test_normalize_strips_reference_codes_and_aliases():
    assert normalize_vendor("AMZN MKTP US*2K4LM") == "AMAZON"
    assert normalize_vendor("FACEBK *K3J2") == "META"
    assert normalize_vendor("GOOGLE *ADS8812") == "GOOGLE ADS"
    assert normalize_vendor("INTUIT *QBOOKS 8F2") == "QUICKBOOKS"


def test_vendor_similarity_distinguishes_vendors():
    assert vendor_similarity("UPS*7K2AB", "UPS") == 1.0
    assert vendor_similarity("UPS*7K2AB", "Uline") < 0.6


def test_amount_score_tolerance():
    assert amount_score(100.0, 100.0) == 1.0
    assert amount_score(100.0, 101.5) is not None  # fx fee / tip sized gap
    assert amount_score(100.0, 130.0) is None


def test_clean_match_is_auto():
    r = match_card([txn("T1", "UPS*7K2AB", 55.10, date(2026, 8, 1))], [ent("E1", "UPS", 55.10, date(2026, 8, 2))])
    assert len(r["auto"]) == 1 and r["auto"][0]["score"] >= AUTO_THRESHOLD


def test_one_to_one_and_ambiguity_goes_to_review():
    st = [txn("T1", "CANVA* 2AAAA", 12.99, date(2026, 8, 1)), txn("T2", "CANVA* 3BBBB", 12.99, date(2026, 8, 1))]
    lg = [ent("E1", "Canva", 12.99, date(2026, 8, 1))]
    r = match_card(st, lg)
    assert len(r["auto"]) + len(r["review"]) == 1  # the single entry explains only one charge
    assert len(r["unmatched_txns"]) == 1


def test_duplicate_entry_is_flagged_not_matched():
    st = [txn("T1", "FEDEX 4412", 80.0, date(2026, 8, 3))]
    lg = [ent("E1", "FedEx", 80.0, date(2026, 8, 4)), ent("E2", "FedEx", 80.0, date(2026, 8, 4))]
    r = match_card(st, lg)
    assert len(r["auto"]) == 1 and len(r["duplicates"]) == 1


def test_late_entry_never_auto_approved():
    r = match_card([txn("T1", "STAPLES 0123", 40.0, date(2026, 8, 1))], [ent("E1", "Staples", 40.0, date(2026, 8, 11))])
    assert not r["auto"] and len(r["review"]) == 1 and "late entry" in r["review"][0]["reason"]


def test_invoice_rules():
    orders = [{"order_id": "A", "status": "Shipped", "total": 100.0}, {"order_id": "B", "status": "Shipped", "total": 50.0},
              {"order_id": "C", "status": "Cancelled", "total": 20.0}, {"order_id": "D", "status": "Shipped", "total": 10.0},
              {"order_id": "E", "status": "Pending", "total": 10.0}]
    inv = [{"invoice_no": "1", "order_ref": "A", "total": 90.0}, {"invoice_no": "2", "order_ref": "C", "total": 20.0},
           {"invoice_no": "3", "order_ref": "D", "total": 10.0}, {"invoice_no": "4", "order_ref": "D", "total": 10.0},
           {"invoice_no": "5", "order_ref": "ZZZ", "total": 5.0}]
    kinds = {(f["kind"], f["order_id"]) for f in reconcile_invoices(orders, inv)}
    assert kinds == {("AMOUNT_MISMATCH", "A"), ("MISSING_INVOICE", "B"), ("CANCELLED_INVOICED", "C"),
                     ("DUPLICATE_INVOICE", "D"), ("ORPHAN_INVOICE", "ZZZ")}  # pending order E is correctly ignored


def test_audit_log_is_append_only_and_decisions_are_final(tmp_path):
    s = Store(str(tmp_path / "r.db"))
    i = s.add_item("MISSING_ENTRY", "card", "x", {"a": 1}, 0.9)
    s.decide(i, "approved", "loyd")
    with pytest.raises(ValueError):
        s.decide(i, "rejected", "someone")
    with pytest.raises(sqlite3.DatabaseError):
        s.db.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.DatabaseError):
        s.db.execute("UPDATE audit_log SET actor='x'")
    assert [a["action"] for a in s.audit()] == ["flagged_for_review", "approved"]


def test_end_to_end_quality_floor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.generate("data", 42)
    card, proposals, findings, store = cli.reconcile()
    ev = cli.evaluate(card, proposals, findings)
    assert ev["auto_precision"] >= 0.98  # a wrong auto-match is the costly error: guard it hardest
    assert ev["missing_recall"] >= 0.95
    assert all(r == 1.0 for _, r in ev["invoice"].values())
    assert not any(i["kind"] == "MISSING_ENTRY" and i["status"] != "pending" for i in store.items())  # nothing self-approves
