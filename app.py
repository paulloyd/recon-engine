"""Reconciliation Review: a point-and-click screen for finance staff.

Run from the repo root:  streamlit run app.py
"""
import json

import streamlit as st

from src import cli, service
from src.categorize import CATEGORIES
from src.matcher import normalize_vendor, vendor_similarity
from src.store import Store

st.set_page_config(page_title="Reconciliation Review", page_icon="🧾", layout="wide")
service.apply_settings()

PAGES = ["1. Run", "2. Review card charges", "3. Invoice exceptions", "4. Summary", "5. Audit log", "6. Settings"]
PAGE_SIZE = 8


def get_store():
    return Store(str(service.DB_PATH)) if service.DB_PATH.exists() else None


def item_amount(item):
    p = json.loads(item["payload"])
    if "txn" in p:
        return p["txn"]["amount"]
    if "entry" in p:
        return p["entry"]["amount"]
    return max(p.get("expected", 0), p.get("actual", 0))


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("🧾 Reconciliation Review")
    reviewer = st.text_input("Your name", key="reviewer", placeholder="Type your name to start reviewing",
                             help="Recorded on every approval so there is always a trail.").strip()
    page = st.radio("Go to", PAGES, key="page", label_visibility="collapsed")
    store = get_store()
    if store:
        pend = len(store.items("pending"))
        st.metric("Waiting for you", pend)
    st.caption("Nothing here posts to QuickBooks. Approved items are exported as a file you import yourself.")


# ------------------------------------------------------------------ shared card renderer
def render_items(store, kinds, status, key_prefix, approve_label="Approve", reject_label="Reject",
                 min_conf=0.0, sort="Lowest confidence first"):
    items = [i for i in store.items(status) if i["kind"] in kinds and (i["confidence"] or 0) >= min_conf]
    if sort == "Lowest confidence first":
        items.sort(key=lambda i: (i["confidence"] is None, i["confidence"] or 0))
    elif sort == "Highest confidence first":
        items.sort(key=lambda i: -(i["confidence"] or 0))
    else:
        items.sort(key=lambda i: -item_amount(i))
    if not items:
        st.success("Nothing here. 🎉" if status == "pending" else "No items in this list.")
        return
    pages = max(1, -(-len(items) // PAGE_SIZE))
    c1, c2 = st.columns([1, 5])
    page_no = c1.number_input("Page", 1, pages, 1, key=f"{key_prefix}_page")
    c2.caption(f"{len(items)} item(s), page {page_no} of {pages}")
    for it in items[(page_no - 1) * PAGE_SIZE: page_no * PAGE_SIZE]:
        iid, p = it["id"], json.loads(it["payload"])
        with st.container(border=True):
            top, side = st.columns([5, 1])
            top.markdown(f"**{service.FRIENDLY[it['kind']]}** · ${item_amount(it):,.2f}")
            top.caption(it["summary"])
            if it["confidence"] is not None and it["source"] == "card":
                side.metric("Confidence", f"{it['confidence']:.0%}")
            with st.expander("Why is this here?"):
                st.write(service.why(it))
            if status != "pending":
                st.caption(f"{it['status'].title()} by {it['reviewer']} at {it['decided_at']}"
                           + (f" · note: {it['note']}" if it["note"] else ""))
                if st.button("↩ Undo", key=f"{key_prefix}_undo_{iid}", disabled=not reviewer):
                    store.reopen(iid, reviewer)
                    st.rerun()
                continue
            new_cat, remember = None, False
            if it["kind"] == "MISSING_ENTRY":
                new_cat = st.selectbox("Category", CATEGORIES, index=CATEGORIES.index(p["proposed_category"]),
                                       key=f"{key_prefix}_cat_{iid}", help="Change it if the suggestion is wrong.")
            if it["kind"] == "POSSIBLE_MATCH":
                a, b = p["txn"]["description"], p["entry"]["vendor"]
                if vendor_similarity(a, b) < 0.8 and len(normalize_vendor(a).split()) == 1:
                    remember = st.checkbox(f"Remember that “{a}” means “{b}”", key=f"{key_prefix}_mem_{iid}")
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button(approve_label, type="primary", key=f"{key_prefix}_approve_{iid}", disabled=not reviewer):
                if new_cat and new_cat != p["proposed_category"]:
                    store.update_payload(iid, {"proposed_category": new_cat, "category_source": "reviewer"}, reviewer)
                store.decide(iid, "approved", reviewer, st.session_state.get("note", ""))
                if remember:
                    service.learn_alias(p["txn"]["description"], p["entry"]["vendor"])
                st.rerun()
            if b2.button(reject_label, key=f"{key_prefix}_reject_{iid}", disabled=not reviewer):
                store.decide(iid, "rejected", reviewer, st.session_state.get("note", ""))
                st.rerun()


def need_name():
    if not reviewer:
        st.warning("Type your name in the sidebar to enable Approve and Reject.")


# ------------------------------------------------------------------ pages
if page == PAGES[0]:
    st.header("Run a reconciliation")
    st.write("Bring in the credit-card statement, the QuickBooks ledger, the orders export and the invoices. "
             "The tool matches what it can and puts everything else in your review queue.")
    existing = store.items() if store else []
    decided = [i for i in existing if i["status"] != "pending"]
    confirm = True
    if decided:
        st.warning(f"The current queue has {len(decided)} decision(s) already made. Starting a new run replaces the queue "
                   "(the export of approved entries is the thing to keep, so download it first).")
        confirm = st.checkbox("I understand and want to start a new run", key="confirm_replace")
    source = st.radio("Where is the data coming from?", ["Try it with demo data", "Upload my files"], horizontal=True)
    if source == "Try it with demo data":
        st.caption("Generates a made-up month of card charges, ledger entries, orders and invoices with planted errors.")
        if st.button("Create demo data and reconcile", type="primary", disabled=not confirm, key="run_demo"):
            with st.spinner("Matching…"):
                res = service.run_demo_data()
            st.session_state["demo_eval"] = res["evaluation"]
            st.success(f"Done: {res['auto']} matched automatically, {res['review']} possible matches need a look. "
                       "Open '2. Review card charges'.")
    else:
        files, ok_all = {}, True
        for name in service.TEMPLATES:
            c1, c2 = st.columns([4, 1])
            up = c1.file_uploader(name, type="csv", key=f"up_{name}")
            c2.download_button("Template", service.template_csv(name), file_name=name, key=f"tpl_{name}")
            if up is not None:
                raw = up.getvalue()
                ok, msg = service.validate_csv(name, raw)
                (st.success if ok else st.error)(msg)
                ok_all &= ok
                files[name] = raw
            else:
                ok_all = False
        if st.button("Reconcile", type="primary", disabled=not (ok_all and confirm), key="run_upload"):
            service.install_uploads(files)
            with st.spinner("Matching…"):
                res = service.run_reconciliation(has_truth=False)
            st.session_state["demo_eval"] = None
            st.success(f"Done: {res['auto']} matched automatically, {res['review']} possible matches need a look.")

elif page == PAGES[1]:
    st.header("Review card charges")
    if not store:
        st.info("Nothing to review yet. Start on '1. Run'.")
        st.stop()
    need_name()
    view = st.radio("Show", ["Pending", "Approved", "Rejected"], horizontal=True, key="card_view")
    status = view.lower()
    f1, f2, f3 = st.columns(3)
    kinds_friendly = f1.multiselect("Type", [service.FRIENDLY[k] for k in service.CARD_KINDS],
                                    default=[service.FRIENDLY[k] for k in service.CARD_KINDS], key="card_kinds")
    min_conf = f2.slider("Minimum confidence", 0.0, 1.0, 0.0, 0.05, key="card_minconf")
    sort = f3.selectbox("Sort", ["Lowest confidence first", "Highest confidence first", "Largest amount first"], key="card_sort")
    rev = {v: k for k, v in service.FRIENDLY.items()}
    kinds = [rev[k] for k in kinds_friendly]
    if status == "pending":
        with st.expander("⚡ Bulk approve the obvious ones"):
            bk = st.selectbox("Type", [service.FRIENDLY[k] for k in ("MISSING_ENTRY", "POSSIBLE_MATCH")], key="bulk_kind")
            bt = st.slider("Only items with confidence of at least", 0.5, 1.0, 0.9, 0.01, key="bulk_thr")
            ids = service.high_confidence_ids(store, rev[bk], bt)
            st.write(f"**{len(ids)}** item(s) qualify.")
            st.caption("You can still undo any of them afterwards from the Approved tab.")
            if st.button(f"Approve {len(ids)} item(s)", disabled=not (reviewer and ids), key="bulk_go"):
                n = service.decide_many(store, ids, "approved", reviewer, f"bulk approve >= {bt:.0%}")
                st.toast(f"Approved {n} items")
                st.rerun()
    if status == "approved":
        st.download_button("⬇ Download approved entries for QuickBooks import (CSV)",
                           service.export_csv(store, reviewer or "unknown", log=False), file_name="qb_import_approved.csv",
                           on_click=lambda: Store(str(service.DB_PATH)).log(reviewer or "unknown", "export_downloaded"),
                           key="export_dl")
    render_items(store, kinds, status, "card", min_conf=min_conf, sort=sort)

elif page == PAGES[2]:
    st.header("Invoice exceptions")
    st.caption("Orders in the order system compared with invoices in QuickBooks. Biggest dollar impact first.")
    if not store:
        st.info("Nothing to review yet. Start on '1. Run'.")
        st.stop()
    need_name()
    view = st.radio("Show", ["Pending", "Confirmed", "Not an issue"], horizontal=True, key="inv_view")
    status = {"Pending": "pending", "Confirmed": "approved", "Not an issue": "rejected"}[view]
    kinds_friendly = st.multiselect("Type", [service.FRIENDLY[k] for k in service.INVOICE_KINDS],
                                    default=[service.FRIENDLY[k] for k in service.INVOICE_KINDS], key="inv_kinds")
    rev = {v: k for k, v in service.FRIENDLY.items()}
    render_items(store, [rev[k] for k in kinds_friendly], status, "inv", approve_label="Confirm issue",
                 reject_label="Not an issue", sort="Largest amount first")

elif page == PAGES[3]:
    st.header("Summary")
    if not store:
        st.info("Nothing to summarise yet. Start on '1. Run'.")
        st.stop()
    s = service.summary(store)
    run = s["run"] or {}
    lines = run.get("statement_lines", 0)
    c = st.columns(4)
    c[0].metric("Card lines", lines)
    c[1].metric("Matched automatically", s["auto"], f"{s['auto'] / lines:.0%}" if lines else None)
    c[2].metric("Waiting for review", s["by_status"]["pending"])
    c[3].metric("Decided", s["by_status"]["approved"] + s["by_status"]["rejected"])
    c = st.columns(2)
    c[0].metric("Card charges not in the books", f"${s['unrecorded']:,.2f}")
    c[1].metric("Invoice problems still open", f"${s['invoice_pending_dollars']:,.2f}")
    if s["total_items"]:
        done = (s["by_status"]["approved"] + s["by_status"]["rejected"]) / s["total_items"]
        st.progress(done, text=f"{done:.0%} of review items decided")
    st.subheader("Still open, by type")
    st.bar_chart({k: v for k, v in s["pending_by_kind"].items() if v})
    st.subheader("Time saved (your estimate)")
    mins = st.number_input("Minutes to match one card line by hand", 0.0, 30.0, 1.5, 0.5, key="mins_per_line",
                           help="An assumption you set, not a measurement. Replace it after timing a real month-end.")
    st.write(f"Automatic matches ≈ **{s['auto'] * mins / 60:.1f} hours** of manual matching avoided this run.")
    if st.session_state.get("demo_eval"):
        ev = st.session_state["demo_eval"]
        with st.expander("Accuracy on the demo data (planted errors)"):
            st.write(f"Auto-matches that were correct: **{ev['auto_precision']:.1%}**. "
                     f"Real matches found (auto + review): **{ev['match_recall']:.1%}**. "
                     f"Unrecorded charges found: **{ev['missing_recall']:.0%}**.")
            st.caption("Measured on generated data only.")

elif page == PAGES[4]:
    st.header("Audit log")
    if not store:
        st.info("Nothing yet. Start on '1. Run'.")
        st.stop()
    rows = store.audit()
    a1, a2 = st.columns(2)
    actors = a1.multiselect("Who", sorted({r["actor"] for r in rows}), key="aud_actor")
    acts = a2.multiselect("What", sorted({r["action"] for r in rows}), key="aud_action")
    rows = [r for r in rows if (not actors or r["actor"] in actors) and (not acts or r["action"] in acts)]
    st.caption(f"{len(rows)} entries. This log cannot be edited or deleted.")
    st.dataframe(list(reversed(rows)), use_container_width=True, hide_index=True)
    st.download_button("⬇ Download audit log (CSV)", "id,ts,actor,action,item_id,detail\n" + "\n".join(
        ",".join(json.dumps(str(r[k] if r[k] is not None else "")) for k in ("id", "ts", "actor", "action", "item_id", "detail"))
        for r in rows), file_name="audit_log.csv", key="aud_dl")

else:
    st.header("Settings")
    s = service.load_settings()
    st.subheader("How careful should automatic matching be?")
    thr = st.slider("Auto-match confidence", 0.70, 0.99, float(s["auto_threshold"]), 0.01, key="set_thr",
                    help="Higher means fewer automatic matches and more items for people to review.")
    st.caption("Takes effect on the next run.")
    if st.button("Save", key="set_save"):
        s["auto_threshold"] = thr
        service.save_settings(s)
        st.success("Saved.")
    st.subheader("Vendor name shortcuts")
    st.caption("Teach the tool that a card descriptor and a ledger name are the same vendor. "
               "You can also add one from the review screen with 'Remember that…'.")
    st.dataframe([{"Card descriptor": k, "Means": v} for k, v in s["aliases"].items()] or [{"Card descriptor": "(none yet)", "Means": ""}],
                 hide_index=True, use_container_width=True)
    a, b = st.columns(2)
    d, v = a.text_input("Card descriptor, e.g. FACEBK", key="al_desc"), b.text_input("Ledger name, e.g. Facebook Ads", key="al_vendor")
    if st.button("Add shortcut", key="al_add"):
        res = service.learn_alias(d, v) if d and v else None
        st.success(f"Added {res}") if res else st.error("Enter a one-word descriptor and a different ledger name.")
