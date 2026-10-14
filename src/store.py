"""SQLite review queue + append-only audit log (the human-in-the-loop control)."""
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_items(
  id INTEGER PRIMARY KEY, kind TEXT, source TEXT, summary TEXT, confidence REAL,
  payload TEXT, status TEXT DEFAULT 'pending', reviewer TEXT, note TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT, item_id INTEGER, detail TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path="reports/review.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def log(self, actor, action, item_id=None, detail=None):
        self.db.execute("INSERT INTO audit_log(ts,actor,action,item_id,detail) VALUES(?,?,?,?,?)",
                        (_now(), actor, action, item_id, json.dumps(detail) if detail is not None else None))
        self.db.commit()

    def add_item(self, kind, source, summary, payload, confidence=None):
        cur = self.db.execute("INSERT INTO review_items(kind,source,summary,confidence,payload) VALUES(?,?,?,?,?)",
                              (kind, source, summary, confidence, json.dumps(payload)))
        self.db.commit()
        self.log("engine", "flagged_for_review", cur.lastrowid, {"kind": kind, "confidence": confidence})
        return cur.lastrowid

    def decide(self, item_id, decision, reviewer, note=""):
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
        row = self.db.execute("SELECT status FROM review_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(f"no review item {item_id}")
        if row["status"] != "pending":
            raise ValueError(f"item {item_id} already {row['status']}")
        self.db.execute("UPDATE review_items SET status=?, reviewer=?, note=?, decided_at=? WHERE id=?",
                        (decision, reviewer, note, _now(), item_id))
        self.db.commit()
        self.log(reviewer, decision, item_id, {"note": note})

    def reopen(self, item_id, actor, note=""):
        """Undo a decision. The earlier decision stays in the audit log; nothing is erased."""
        row = self.db.execute("SELECT status FROM review_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(f"no review item {item_id}")
        if row["status"] == "pending":
            raise ValueError(f"item {item_id} is already pending")
        self.db.execute("UPDATE review_items SET status='pending', reviewer=NULL, note=NULL, decided_at=NULL WHERE id=?",
                        (item_id,))
        self.db.commit()
        self.log(actor, "reopened", item_id, {"was": row["status"], "note": note})

    def update_payload(self, item_id, changes: dict, actor):
        """Let a reviewer correct a proposal (e.g. the category) before approving. Pending items only."""
        row = self.db.execute("SELECT status, payload FROM review_items WHERE id=?", (item_id,)).fetchone()
        if row is None or row["status"] != "pending":
            raise ValueError("only pending items can be edited")
        payload = json.loads(row["payload"])
        before = {k: payload.get(k) for k in changes}
        payload.update(changes)
        self.db.execute("UPDATE review_items SET payload=? WHERE id=?", (json.dumps(payload), item_id))
        self.db.commit()
        self.log(actor, "edited", item_id, {"before": before, "after": changes})

    def get(self, item_id):
        r = self.db.execute("SELECT * FROM review_items WHERE id=?", (item_id,)).fetchone()
        return dict(r) if r else None

    def items(self, status=None, kind=None):
        q, args = "SELECT * FROM review_items WHERE 1=1", []
        if status:
            q += " AND status=?"; args.append(status)
        if kind:
            q += " AND kind=?"; args.append(kind)
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", args)]

    def audit(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM audit_log ORDER BY id")]
