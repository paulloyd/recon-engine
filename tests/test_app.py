"""Drive the real Streamlit app headlessly (no browser) to prove the click paths work."""
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src import service
from src.store import Store

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    return at


def go(at, page):
    at.sidebar.radio(key="page").set_value(page).run()
    assert not at.exception, at.exception
    return at


def run_demo(at):
    go(at, "1. Run")
    at.button(key="run_demo").click().run()
    assert not at.exception and at.success
    return at


def test_demo_run_then_empty_name_blocks_approval(app):
    run_demo(app)
    go(app, "2. Review card charges")
    approve = [b for b in app.button if b.key and "_approve_" in b.key]
    assert approve and all(b.disabled for b in approve)  # no reviewer name -> nothing can be approved


def test_approve_edit_category_and_undo_are_audited(app):
    run_demo(app)
    app.sidebar.text_input(key="reviewer").set_value("Amaka").run()
    go(app, "2. Review card charges")
    app.multiselect(key="card_kinds").set_value(["Charge not in books"]).run()
    cat = next(s for s in app.selectbox if s.key.startswith("card_cat_"))
    iid = int(cat.key.rsplit("_", 1)[1])
    cat.select("Travel & Meals").run()
    app.button(key=f"card_approve_{iid}").click().run()
    store = Store(str(service.DB_PATH))
    item = store.get(iid)
    assert item["status"] == "approved" and item["reviewer"] == "Amaka"
    assert json.loads(item["payload"])["proposed_category"] == "Travel & Meals"
    assert {"edited", "approved"} <= {a["action"] for a in store.audit() if a["item_id"] == iid}
    # export contains the edited category, then undo removes it from the approved list
    assert b"Travel & Meals" in service.export_csv(store, log=False)
    app.radio(key="card_view").set_value("Approved").run()
    app.button(key=f"card_undo_{iid}").click().run()
    assert Store(str(service.DB_PATH)).get(iid)["status"] == "pending"
    assert [a["action"] for a in Store(str(service.DB_PATH)).audit() if a["item_id"] == iid][-1] == "reopened"


def test_bulk_approve_and_summary_page(app):
    run_demo(app)
    app.sidebar.text_input(key="reviewer").set_value("Amaka").run()
    go(app, "2. Review card charges")
    n = len(service.high_confidence_ids(Store(str(service.DB_PATH)), "MISSING_ENTRY", 0.9))
    assert n > 0
    app.button(key="bulk_go").click().run()
    assert not app.exception
    assert len(Store(str(service.DB_PATH)).items("approved", "MISSING_ENTRY")) == n
    go(app, "4. Summary")
    assert any("Matched automatically" in m.label for m in app.metric)


def test_invoice_page_and_audit_page_render(app):
    run_demo(app)
    app.sidebar.text_input(key="reviewer").set_value("Amaka").run()
    go(app, "3. Invoice exceptions")
    btn = next(b for b in app.button if b.key.startswith("inv_approve_"))
    assert btn.label == "Confirm issue"
    go(app, "5. Audit log")
    assert not app.exception


def test_upload_validation_gives_plain_english_errors():
    ok, msg = service.validate_csv("card_statement.csv", b"txn_id,description\n1,x\n")
    assert not ok and "missing column" in msg and "post_date" in msg
    ok, msg = service.validate_csv("card_statement.csv", b"txn_id,post_date,description,amount\n1,2026-08-01,UPS,abc\n")
    assert not ok and "not a number" in msg
    ok, msg = service.validate_csv("card_statement.csv", b"txn_id,post_date,description,amount\n1,08/01/2026,UPS,5\n")
    assert not ok and "2026-08-31" in msg
    ok, _ = service.validate_csv("card_statement.csv", b"txn_id,post_date,description,amount\n1,2026-08-01,UPS,5.00\n")
    assert ok


def test_learned_alias_changes_future_matching(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src import matcher
    before = matcher.vendor_similarity("FEDEX 4412", "Federal Express")
    assert service.learn_alias("FEDEX 4412", "Federal Express") == {"FEDEX": "FEDERAL EXPRESS"}
    assert matcher.vendor_similarity("FEDEX 4412", "Federal Express") > before
    assert (tmp_path / "settings.json").exists()
    matcher.ALIASES.pop("FEDEX", None)  # don't leak into other tests
    matcher.ALIASES["FEDEX"] = "FEDEX"
