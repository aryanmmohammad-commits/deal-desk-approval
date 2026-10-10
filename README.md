# Deal Desk Approval Flow (prototype)

A small, working approval system for sales quotes. When a seller submits a quote, the system checks it against the company's pricing rules, asks the right approvers in Slack only when needed, and logs every decision with timestamps.

> Working prototype / reference design. All data is synthetic.

## Where it sits in the revenue chain
Quote → approval → signature → booking. It covers the end of the Opportunity stage and the hand-off into Booking.

## Architecture

```
Seller / CRM ──► n8n Webhook ──► FastAPI /evaluate ──► Airtable (rules + approvers)
                                      │
                                      └──► SQLite logbook (deal_approvals)
             ──► If: duplicate or auto-approved? stop
             ──► Slack approval (Approve / Decline)
             ──► FastAPI /decision (records the decision and the time)
```

| Part | Job | Tool |
|---|---|---|
| Trigger | Announces a submitted quote | CRM (simulated with an HTTP call) |
| Carrier | Moves the quote between steps, waits for humans | n8n |
| Judge | Applies the rules, returns approvers and reasons | FastAPI (Python) |
| Rulebook | Approval thresholds, editable without code | Airtable |
| Logbook | State, duplicates, timestamps | SQLite (PostgreSQL-ready) |
| Messenger | Asks approvers, collects the click | Slack |

## Design decisions
- **Rules live outside the code** (Airtable), so Deal Ops can change a threshold without an engineer.
- **One place decides** (FastAPI). n8n only carries and waits.
- **Idempotency:** `(deal_id, quote_version)` is the primary key. A duplicate submission creates no second row and no second Slack request.
- **Versioning:** a new quote version supersedes the older ones.
- **Blank ≠ zero:** missing fields go to manual review instead of auto-approving.
- **Humans only see exceptions:** clean quotes are auto-approved.
- **Audit:** every verdict stores the rule-matrix version it used.

## Metrics it enables
- Turnaround = `decided_at − received_at`
- Exception rate = quotes needing approval ÷ all quotes
- Open stock = rows with status `pending`

## Run it locally
1. `python -m venv .venv` and activate it
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and add your Airtable token and base ID
4. Import `data/approval_matrix.csv` and `data/approvers.csv` into an Airtable base (tables `approval_matrix` and `approvers`)
5. `uvicorn main:app --reload`
6. Install and start n8n (`npm install -g n8n`, then `n8n start`), import `n8n/deal-desk-approval-flow.json`, add a Slack bot credential (scope `chat:write`) and your channel ID, then publish
7. Send a quote:
```
   curl -X POST http://localhost:5678/webhook/quote-submitted \
     -H "Content-Type: application/json" \
     -d '{"deal_id":"D-1007","quote_version":1,"discount_pct":40,"payment_terms_days":45,"term_months":36,"annual_commit_eur":300000,"special_terms":""}'
```

## Roadmap: what a production version needs
1. Separate approval per approver, plus a role check on who clicked
2. A hosted server with HTTPS (always on, buttons work from a phone)
3. A real CRM trigger, and the decision written back to the quote
4. Reminders and escalation after 24 hours
5. PostgreSQL and monitoring
6. An AI brief for non-standard terms; booked-vs-approved reconciliation in dbt

## Data
All accounts, people and numbers are invented. The test cases include boundary discounts, a duplicate submission, a superseded version, a missing field, and non-standard contract terms.