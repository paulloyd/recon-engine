"""Credit-card statement <-> QuickBooks-style ledger matching.

Design goals (what a finance lead cares about):
  * one-to-one matching (a ledger line can only explain one card charge)
  * explainable scores (amount / vendor / date) instead of a black box
  * conservative auto-approval: anything ambiguous goes to a human
"""
import re
from datetime import date
from difflib import SequenceMatcher

AUTO_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.55
AMBIGUITY_MARGIN = 0.05
MAX_REL_AMOUNT_DIFF = 0.03
DATE_WINDOW = (-2, 5)  # ledger date minus card post date, in days
LATE_WINDOW = (-2, 14)  # second pass, leftovers only: entries keyed late. Never auto-approved.

STOP = {"MKTP", "US", "COM", "PO", "THE", "STORE", "TRIP", "SHIP", "SQ", "INC", "LLC", "CO", "VIA"}
ALIASES = {"AMZN": "AMAZON", "FACEBK": "META", "INTUIT": "QUICKBOOKS", "QBOOKS": "QUICKBOOKS",
           "GSUITE": "GOOGLE WORKSPACE"}


def normalize_vendor(text: str) -> str:
    """Reduce a cryptic card descriptor or a clean ledger name to comparable tokens."""
    out = []
    for tok in re.findall(r"[A-Z0-9]+", text.upper().replace("'", "")):
        tok = re.sub(r"\d+$", "", tok)  # ADS8812 -> ADS
        if len(tok) < 2 or any(c.isdigit() for c in tok) or tok in STOP:
            continue  # drops random reference codes like 2K4LM
        for word in ALIASES.get(tok, tok).split():
            if word not in out:
                out.append(word)
    return " ".join(out)


def vendor_similarity(a: str, b: str) -> float:
    na, nb = normalize_vendor(a), normalize_vendor(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb)
    subset = 0.8 if (ta <= tb or tb <= ta) else 0.0
    return max(SequenceMatcher(None, na, nb).ratio(), jaccard, subset)


def amount_score(a: float, b: float):
    diff = abs(a - b)
    if diff < 0.005:
        return 1.0
    rel = diff / max(abs(a), abs(b))
    return None if rel > MAX_REL_AMOUNT_DIFF else 0.7 - 10 * rel


def score_pair(txn: dict, entry: dict, window=DATE_WINDOW):
    """Return (score, parts) or None if the pair is not even a candidate."""
    dd = (entry["date"] - txn["date"]).days
    if not window[0] <= dd <= window[1]:
        return None
    amt = amount_score(txn["amount"], entry["amount"])
    if amt is None:
        return None
    vs = vendor_similarity(txn["description"], entry["vendor"])
    ds = max(0.0, 1 - abs(dd) / 8)
    return 0.5 * amt + 0.3 * vs + 0.2 * ds, {"amount": round(amt, 2), "vendor": round(vs, 2), "date": round(ds, 2)}


def _twin(a: dict, b: dict) -> bool:
    return (normalize_vendor(a["vendor"]) == normalize_vendor(b["vendor"])
            and abs(a["amount"] - b["amount"]) < 0.005 and abs((a["date"] - b["date"]).days) <= 1)


def match_card(statement: list, ledger: list) -> dict:
    cands = []
    for t in statement:
        for e in ledger:
            res = score_pair(t, e)
            if res and res[0] >= REVIEW_THRESHOLD:
                cands.append((res[0], t["txn_id"], e["entry_id"], res[1]))
    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    t_by = {t["txn_id"]: t for t in statement}
    e_by = {e["entry_id"]: e for e in ledger}
    by_txn, by_entry = {}, {}
    for s, tid, eid, _ in cands:
        by_txn.setdefault(tid, []).append((s, eid))
        by_entry.setdefault(eid, []).append((s, tid))

    used_t, used_e, auto, review = set(), set(), [], []
    for s, tid, eid, parts in cands:
        if tid in used_t or eid in used_e:
            continue
        used_t.add(tid)
        used_e.add(eid)
        rivals = [x for x, other in by_txn[tid] if other != eid and not _twin(e_by[eid], e_by[other])]
        rivals += [x for x, other in by_entry[eid] if other != tid]
        margin = s - max(rivals) if rivals else 1.0
        row = {"txn": t_by[tid], "entry": e_by[eid], "score": round(s, 3), "parts": parts, "margin": round(margin, 3)}
        if s >= AUTO_THRESHOLD and margin >= AMBIGUITY_MARGIN:
            auto.append(row)
        else:
            row["reason"] = ("ambiguous: a rival candidate scores within %.2f" % AMBIGUITY_MARGIN
                             if s >= AUTO_THRESHOLD else "partial match: check amount/vendor/date")
            review.append(row)

    # second pass: leftovers only, wider date window, human review required
    late = []
    for t in statement:
        if t["txn_id"] in used_t:
            continue
        for e in ledger:
            if e["entry_id"] in used_e:
                continue
            res = score_pair(t, e, LATE_WINDOW)
            if res and res[0] >= REVIEW_THRESHOLD:
                late.append((res[0], t["txn_id"], e["entry_id"], res[1]))
    late.sort(key=lambda c: (-c[0], c[1], c[2]))
    for s_, tid, eid, parts in late:
        if tid in used_t or eid in used_e:
            continue
        used_t.add(tid)
        used_e.add(eid)
        days = (e_by[eid]["date"] - t_by[tid]["date"]).days
        review.append({"txn": t_by[tid], "entry": e_by[eid], "score": round(s_, 3), "parts": parts, "margin": 0.0,
                       "reason": f"late entry? ledger date is {days} days after the card post date"})

    unmatched_txns = [t for t in statement if t["txn_id"] not in used_t]
    matched_entries = [e_by[i] for i in used_e]
    duplicates, ledger_only = [], []
    for e in ledger:
        if e["entry_id"] in used_e:
            continue
        twin = next((m for m in matched_entries if _twin(e, m)), None)
        (duplicates if twin else ledger_only).append({"entry": e, "duplicate_of": twin["entry_id"] if twin else None})
    return {"auto": auto, "review": review, "unmatched_txns": unmatched_txns,
            "duplicates": duplicates, "ledger_only": ledger_only}
