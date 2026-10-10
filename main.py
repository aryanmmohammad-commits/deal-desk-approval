import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, field_validator
from pyairtable import Api

load_dotenv()  # open the .env safe

api = Api(os.environ["AIRTABLE_TOKEN"])  # the key card
matrix = api.table(os.environ["AIRTABLE_BASE_ID"], "approval_matrix")  # which page

app = FastAPI()  # build the counter window

# Fields the rules need. If one is missing, a human must look.
REQUIRED = ["discount_pct", "payment_terms_days", "term_months"]

# ---------- The logbook (SQLite) ----------
DB_PATH = "deal_desk.db"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows come back with column names
    return conn


with db() as conn:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deal_approvals (
            deal_id        TEXT    NOT NULL,
            quote_version  INTEGER NOT NULL,
            tier           TEXT    NOT NULL,
            approvers      TEXT,
            reasons        TEXT,
            matrix_version INTEGER,
            status         TEXT    NOT NULL,
            received_at    TEXT    NOT NULL,
            decided_by     TEXT,
            decided_at     TEXT,
            PRIMARY KEY (deal_id, quote_version)
        )
    """)


def now():
    return datetime.now(timezone.utc).isoformat()


# ---------- The judge (same as before) ----------
class Quote(BaseModel):
    deal_id: str
    quote_version: int
    discount_pct: Optional[float] = None
    payment_terms_days: Optional[int] = None
    term_months: Optional[int] = None
    annual_commit_eur: Optional[float] = None
    special_terms: Optional[str] = None

    # A blank box means "missing", never zero (the D-1013 trap)
    @field_validator("*", mode="before")
    @classmethod
    def blank_means_missing(cls, value):
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


def load_rules():
    rules = [record["fields"] for record in matrix.all()]
    return sorted(rules, key=lambda rule: rule["rule_id"])


def rule_is_broken(rule, quote):
    value = getattr(quote, rule["field"], None)
    operator = rule["operator"]

    if operator == "is_not_empty":       # R-05: no number, just "is there text?"
        return value is not None

    threshold = rule.get("threshold")
    if value is None or threshold is None:
        return False
    if operator == ">":
        return value > threshold
    if operator == "<":
        return value < threshold
    return False


def judge(quote):
    missing = [field for field in REQUIRED if getattr(quote, field) is None]
    if missing:
        return {
            "deal_id": quote.deal_id,
            "quote_version": quote.quote_version,
            "tier": "manual_review",
            "approvers": ["Deal Desk Analyst"],
            "reasons": [f"Missing value: {field}" for field in missing],
        }

    rules = load_rules()
    approvers, reasons = [], []
    for rule in rules:
        if rule_is_broken(rule, quote):
            if rule["approver_role"] not in approvers:
                approvers.append(rule["approver_role"])
            reasons.append(rule["reason_text"])

    return {
        "deal_id": quote.deal_id,
        "quote_version": quote.quote_version,
        "tier": "needs_approval" if approvers else "auto_approve",
        "approvers": approvers,
        "reasons": reasons,
        "matrix_version": max(rule.get("matrix_version", 0) for rule in rules),
    }


# ---------- Writing in the logbook ----------
def save_to_logbook(verdict):
    auto = verdict["tier"] == "auto_approve"
    t = now()
    try:
        with db() as conn:
            conn.execute(
                """INSERT INTO deal_approvals
                   (deal_id, quote_version, tier, approvers, reasons, matrix_version,
                    status, received_at, decided_by, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    verdict["deal_id"],
                    verdict["quote_version"],
                    verdict["tier"],
                    json.dumps(verdict["approvers"]),
                    json.dumps(verdict["reasons"]),
                    verdict.get("matrix_version"),
                    "approved" if auto else "pending",
                    t,
                    "auto" if auto else None,
                    t if auto else None,
                ),
            )
            # A newer version cancels the older ones (the re-submitted quote, D-1011)
            conn.execute(
                """UPDATE deal_approvals SET status = 'superseded'
                   WHERE deal_id = ? AND quote_version < ?
                     AND status IN ('pending', 'approved')""",
                (verdict["deal_id"], verdict["quote_version"]),
            )
        return False  # a new line was written
    except sqlite3.IntegrityError:
        return True   # this deal_id + quote_version is already in the logbook


# ---------- The windows ----------
@app.get("/rules")
def show_rules():
    return load_rules()


@app.post("/evaluate")
def evaluate(quote: Quote):
    verdict = judge(quote)
    verdict["duplicate"] = save_to_logbook(verdict)
    return verdict


@app.get("/approvals")
def show_approvals():
    with db() as conn:
        rows = conn.execute("SELECT * FROM deal_approvals ORDER BY received_at").fetchall()
    return [dict(row) for row in rows]
# ---------- Recording the human decision ----------
class Decision(BaseModel):
    deal_id: str
    quote_version: int
    approved: bool
    decided_by: Optional[str] = "slack"


@app.post("/decision")
def record_decision(decision: Decision):
    status = "approved" if decision.approved else "declined"
    with db() as conn:
        cursor = conn.execute(
            """UPDATE deal_approvals
               SET status = ?, decided_by = ?, decided_at = ?
               WHERE deal_id = ? AND quote_version = ? AND status = 'pending'""",
            (status, decision.decided_by, now(), decision.deal_id, decision.quote_version),
        )
    return {"updated": cursor.rowcount == 1, "status": status}