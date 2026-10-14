"""Order-management system vs QuickBooks invoice reconciliation (rule-based, fully explainable)."""
from collections import defaultdict

TOLERANCE = 0.01


def _severity(amount: float) -> str:
    return "high" if amount >= 250 else "medium" if amount >= 75 else "low"


def reconcile_invoices(orders: list, invoices: list) -> list:
    by_order = defaultdict(list)
    for inv in invoices:
        by_order[inv["order_ref"]].append(inv)
    known = {o["order_id"] for o in orders}
    findings = []

    def add(kind, order_id, invs, expected, actual, detail):
        delta = round(actual - expected, 2)
        findings.append({"kind": kind, "order_id": order_id, "invoice_nos": [i["invoice_no"] for i in invs],
                         "expected": expected, "actual": actual, "delta": delta,
                         "severity": _severity(abs(delta) if kind == "AMOUNT_MISMATCH" else max(expected, actual)),
                         "detail": detail})

    for o in orders:
        invs = by_order.get(o["order_id"], [])
        total = o["total"]
        if o["status"] == "Shipped":
            if not invs:
                add("MISSING_INVOICE", o["order_id"], [], total, 0.0, "shipped order has no invoice in QuickBooks")
            else:
                if len(invs) > 1:
                    add("DUPLICATE_INVOICE", o["order_id"], invs, total, round(sum(i["total"] for i in invs), 2),
                        f"{len(invs)} invoices exist for one order")
                if any(abs(i["total"] - total) > TOLERANCE for i in invs[:1]):
                    add("AMOUNT_MISMATCH", o["order_id"], invs[:1], total, invs[0]["total"],
                        "invoice total differs from order total")
        elif o["status"] == "Cancelled" and invs:
            add("CANCELLED_INVOICED", o["order_id"], invs, 0.0, round(sum(i["total"] for i in invs), 2),
                "order was cancelled but an invoice exists")
    for ref, invs in by_order.items():
        if ref not in known:
            add("ORPHAN_INVOICE", ref, invs, 0.0, round(sum(i["total"] for i in invs), 2),
                "invoice references an order that does not exist")
    return findings
