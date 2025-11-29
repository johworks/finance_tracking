"""
Fully expanded, working version of the Transactions Web App.
- Positive numbers entered in the UI are stored as negative (expenses) automatically.
- Subscriptions UI restored; application is idempotent per day and normalized to negatives on apply.
- Compact responsive charts (meta and sub-categories) and budgeting controls.
- Includes a reliable "Overall:" summary line used by tests.
- Tests included at bottom; run with: `python -m unittest -v transactions_web_app_full`.

If you prefer multiple physical files later (e.g., routes.py, models.py, tests.py), we can split easily. This single file is kept for canvas reliability.
"""

from __future__ import annotations

import argparse
import calendar
import os
import socket
import sys
import json
import unittest
from datetime import datetime, date, timedelta

from flask import Flask, request, redirect, url_for, render_template_string, flash
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

META_ALLOWED = ("Needs", "Wants", "Savings")


def _normalized_month(value: str | None) -> str:
    """Return a YYYY-MM string, defaulting to the current month."""

    value = (value or "").strip()
    if value:
        try:
            datetime.strptime(value, "%Y-%m")
            return value
        except ValueError:
            pass
    return date.today().strftime("%Y-%m")


def apply_subscriptions(engine, table_tx: str, table_sub: str, target_month: str) -> None:
    """Insert subscription transactions for the provided month using the given engine."""

    target_month = _normalized_month(target_month)
    y, m = map(int, target_month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    with engine.begin() as conn:
        subs = (
            conn.execute(
                text(
                    f"SELECT name, category, amount, day_of_month FROM {table_sub} WHERE active = 1"
                )
            )
            .mappings()
            .all()
        )
        for s in subs:
            d = min(int(s["day_of_month"] or 1), last_day)
            ts = f"{target_month}-{d:02d} 12:00:00"
            desc = f"SUB: {s['name']}"
            normalized_amt = -abs(float(s["amount"] or 0.0))
            exists = conn.execute(
                text(
                    f"""
                    SELECT 1 FROM {table_tx}
                    WHERE date BETWEEN :start AND :end
                      AND description = :desc
                      AND category = :cat
                      AND ABS(amount - :amt) < 1e-9
                """
                ),
                {
                    "start": f"{target_month}-{d:02d} 00:00:00",
                    "end": f"{target_month}-{d:02d} 23:59:59",
                    "desc": desc,
                    "cat": s["category"],
                    "amt": normalized_amt,
                },
            ).first()
            if exists:
                continue
            conn.execute(
                text(
                    f"""
                    INSERT INTO {table_tx} (date, description, amount, category)
                    VALUES (:date, :description, :amount, :category)
                """
                ),
                {
                    "date": ts,
                    "description": desc,
                    "amount": normalized_amt,
                    "category": s["category"],
                },
            )

# -----------------------------
# App factory (allows testing)
# -----------------------------

def create_app(db_url: str | None = None, *, engine_override=None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me")

    DB_URL = db_url or "sqlite:///transactions.db"

    if engine_override is not None:
        engine = engine_override
    else:
        engine = create_engine(DB_URL, connect_args={"check_same_thread": False})

    TABLE_TX = "transactions"
    TABLE_SUB = "subscriptions"
    TABLE_META_MAP = "category_meta"         # category -> meta
    TABLE_INCOME = "monthly_income"          # month -> income
    TABLE_TARGETS = "meta_targets"           # single-row needs/wants/savings %
    TABLE_BUCKETS = "funding_buckets"
    TABLE_PAYROLL = "payroll_entries"

    # Create tables
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_TX} (
                date TEXT DEFAULT CURRENT_TIMESTAMP,
                description TEXT,
                amount REAL,
                category TEXT
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_SUB} (
                name TEXT,
                category TEXT,
                amount REAL,
                day_of_month INTEGER,
                active INTEGER DEFAULT 1
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_META_MAP} (
                category TEXT PRIMARY KEY,
                meta TEXT CHECK (meta in ('Needs','Wants','Savings'))
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_INCOME} (
                month TEXT PRIMARY KEY,
                income REAL
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_TARGETS} (
                id INTEGER PRIMARY KEY CHECK (id=1),
                needs REAL,
                wants REAL,
                savings REAL
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_BUCKETS} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT,
                goal REAL NOT NULL,
                current REAL NOT NULL DEFAULT 0,
                status TEXT CHECK(status IN ('filling','ready','spent','archived')) NOT NULL DEFAULT 'filling',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # lightweight migration for legacy buckets table to add category
        cols = [row[1] for row in conn.execute(text(f"PRAGMA table_info({TABLE_BUCKETS})")).fetchall()]
        if "category" not in cols:
            conn.execute(text(f"ALTER TABLE {TABLE_BUCKETS} ADD COLUMN category TEXT"))

        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_PAYROLL} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pay_date TEXT NOT NULL,
                gross REAL DEFAULT 0,
                tax REAL DEFAULT 0,
                k401 REAL DEFAULT 0,
                hsa REAL DEFAULT 0,
                espp REAL DEFAULT 0,
                other REAL DEFAULT 0,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """))
        exists = conn.execute(text(f"SELECT 1 FROM {TABLE_TARGETS} WHERE id=1")).first()
        if not exists:
            conn.execute(text(f"INSERT INTO {TABLE_TARGETS} (id, needs, wants, savings) VALUES (1, 50, 30, 20)"))

    def _month_param_or_current() -> str:
        m = (request.args.get("month") or "").strip()
        try:
            if m:
                datetime.strptime(m, "%Y-%m")
                return m
        except ValueError:
            pass
        return date.today().strftime("%Y-%m")

    def _month_bounds(ym: str) -> tuple[str, str]:
        y, m = map(int, ym.split("-"))
        first = date(y, m, 1)
        last_day = calendar.monthrange(y, m)[1]
        last = date(y, m, last_day)
        return first.strftime("%Y-%m-%d 00:00:00"), last.strftime("%Y-%m-%d 23:59:59")

    def _adjacent_months(ym: str) -> tuple[str, str]:
        y, m = map(int, ym.split("-"))
        prev_m = (date(y, m, 15) - timedelta(days=31)).strftime("%Y-%m")
        next_m = (date(y, m, 15) + timedelta(days=31)).strftime("%Y-%m")
        return prev_m, next_m

    def _parse_sum_field(raw: str | None) -> float:
        """Allow inputs like '14.27+4.28+17.02' by summing numeric tokens."""
        text_val = (raw or "").strip()
        if not text_val:
            return 0.0
        # Remove spaces to keep UI flexible
        tokens = [t for t in text_val.replace(" ", "").split("+") if t]
        total = 0.0
        for tok in tokens:
            total += float(tok)
        return total

    PAGE_TEMPLATE = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Transactions & Funding Buckets</title>
  <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
  <link href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css\" rel=\"stylesheet\">
  <script src=\"https://cdn.jsdelivr.net/npm/chart.js\"></script>
  <style>
    body { padding-top: 2rem; }
    .amount-neg { color: #b00020; }
    .amount-pos { color: #0a7d2a; }
    .muted { color: #6c757d; }
    .chart-card { height: 300px; }
    .chart-wrap { position: relative; height: 220px; }
  </style>
</head>
<body>
<div class=\"container\">
  <div class=\"d-flex flex-wrap justify-content-between align-items-start mb-3 gap-2\">
    <div>
      <h1 class=\"mb-1\">Your Finances</h1>
      <div class=\"text-muted\">Spending, budgeting, and buckets in one place.</div>
    </div>
    <div class=\"btn-group\" role=\"group\">
      <a class=\"btn btn-outline-primary\" href=\"#payroll\">Bi-weekly payroll</a>
      <a class=\"btn btn-outline-primary\" href=\"#spend\">Transactions</a>
      <a class=\"btn btn-outline-primary\" href=\"#buckets\">Buckets</a>
      <a class=\"btn btn-outline-secondary\" href=\"{{ url_for('buckets_index') }}\">Buckets page</a>
    </div>
  </div>

  <form class=\"d-flex align-items-center gap-2 mb-4\" method=\"get\" action=\"/\">
    <label class=\"form-label m-0\">Month:</label>
    <input type=\"month\" class=\"form-control\" style=\"max-width: 200px\" name=\"month\" value=\"{{ month }}\">
    <button class=\"btn btn-outline-secondary\" type=\"submit\">Go</button>
    <a class=\"btn btn-outline-primary\" href=\"/?month={{ prev_month }}\">◀ {{ prev_month }}</a>
    <a class=\"btn btn-outline-primary\" href=\"/?month={{ next_month }}\">{{ next_month }} ▶</a>
  </form>

  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <div class=\"alert alert-info\">{{ messages[0] }}</div>
    {% endif %}
  {% endwith %}

  <div class=\"row g-3 mb-3\">
    <div class=\"col-12 col-lg-4\">
      <div class=\"card shadow-sm h-100\">
        <div class=\"card-body\">
          <div class=\"d-flex justify-content-between align-items-center\">
            <div>
              <div class=\"text-muted small\">Net for {{ month }}</div>
              <div class=\"fs-4 fw-semibold\">{{ '%.2f'|format(month_total) }}</div>
            </div>
            <span class=\"badge text-bg-{{ 'success' if month_total >=0 else 'danger' }}\">Overall</span>
          </div>
        </div>
      </div>
    </div>
    <div class=\"col-12 col-lg-4\">
      <div class=\"card shadow-sm h-100\">
        <div class=\"card-body\">
          <div class=\"text-muted small\">Active buckets</div>
          <div class=\"d-flex justify-content-between align-items-center\">
            <div>
              <div class=\"fs-5 fw-semibold\">{{ '%.2f'|format(bucket_totals.current) }} / {{ '%.2f'|format(bucket_totals.goal) }}</div>
              <div class=\"text-muted small\">{{ bucket_totals.ready_count }} ready · {{ bucket_totals.filling_count }} filling</div>
            </div>
            <div class=\"progress\" style=\"width:120px; height: 8px;\">
              <div class=\"progress-bar\" role=\"progressbar\" style=\"width: {{ bucket_totals.progress_pct }}%;\"></div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div class=\"col-12 col-lg-4\">
      <div class=\"card shadow-sm h-100\">
        <div class=\"card-body\">
          <div class=\"text-muted small\">Needs/Wants/Savings targets</div>
          <div class=\"d-flex gap-2 flex-wrap\">
            <span class=\"badge text-bg-primary\">Needs {{ targets.needs }}%</span>
            <span class=\"badge text-bg-warning text-dark\">Wants {{ targets.wants }}%</span>
            <span class=\"badge text-bg-success\">Savings {{ targets.savings }}%</span>
          </div>
          {% if income is not none %}
            <div class=\"text-muted small mt-1\">Income set: {{ '%.2f'|format(income) }}</div>
          {% else %}
            <div class=\"text-muted small mt-1\">No income set for {{ month }}.</div>
          {% endif %}
        </div>
      </div>
    </div>
  </div>

  <div class=\"card shadow-sm mb-4\" id=\"payroll\">
    <div class=\"card-body\">
      <div class=\"d-flex justify-content-between align-items-center mb-2\">
        <h5 class=\"card-title mb-0\">Bi-weekly payroll</h5>
        <div class=\"text-muted small\">Enter each pay stub; monthly goals stay aligned with income & targets.</div>
      </div>
      <div class=\"row g-3\">
        <div class=\"col-12 col-xl-5\">
          <form class=\"row g-2\" method=\"post\" action=\"{{ url_for('payroll_add') }}\">
            <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
            <div class=\"col-12\">
              <label class=\"form-label\">Pay date</label>
              <input class=\"form-control\" type=\"date\" name=\"pay_date\" value=\"{{ month }}-01\" required>
            </div>
            <div class=\"col-6\">
              <label class=\"form-label\">Gross</label>
              <input class=\"form-control\" type=\"text\" inputmode=\"decimal\" name=\"gross\" placeholder=\"0.00 or 1500+250\">
            </div>
            <div class=\"col-6\">
              <label class=\"form-label\">Tax withheld</label>
              <input class=\"form-control\" type=\"text\" inputmode=\"decimal\" name=\"tax\" placeholder=\"0.00 or 200+50\">
            </div>
            <div class=\"col-6\">
              <label class=\"form-label\">401k pre-tax</label>
              <input class=\"form-control\" type=\"text\" inputmode=\"decimal\" name=\"k401\" placeholder=\"0.00 or 100+75\">
            </div>
            <div class=\"col-6\">
              <label class=\"form-label\">HSA</label>
              <input class=\"form-control\" type=\"text\" inputmode=\"decimal\" name=\"hsa\" placeholder=\"0.00 or 25+25\">
            </div>
            <div class=\"col-6\">
              <label class=\"form-label\">ESPP</label>
              <input class=\"form-control\" type=\"text\" inputmode=\"decimal\" name=\"espp\" placeholder=\"0.00\">
            </div>
            <div class=\"col-6\">
              <label class=\"form-label\">Other deductions</label>
              <input class=\"form-control\" type=\"text\" inputmode=\"decimal\" name=\"other\" placeholder=\"0.00 or 14.27+4.28\">
            </div>
            <div class=\"col-12\">
              <label class=\"form-label\">Notes</label>
              <input class=\"form-control\" type=\"text\" name=\"notes\" placeholder=\"e.g., Bonus, reimbursement\">
            </div>
            <div class=\"col-12 d-flex justify-content-end\">
              <button class=\"btn btn-primary\" type=\"submit\">Add payroll entry</button>
            </div>
          </form>
        </div>
        <div class=\"col-12 col-xl-7\">
          <div class=\"row g-2 mb-2\">
            <div class=\"col-6 col-md-4\">
              <div class=\"p-2 border rounded small\">Gross: <strong>{{ '%.2f'|format(payroll_summary.gross) }}</strong></div>
            </div>
            <div class=\"col-6 col-md-4\">
              <div class=\"p-2 border rounded small\">Net: <strong>{{ '%.2f'|format(payroll_summary.net) }}</strong></div>
            </div>
            <div class=\"col-6 col-md-4\">
              <div class=\"p-2 border rounded small\">401k: <strong>{{ '%.2f'|format(payroll_summary.k401) }}</strong></div>
            </div>
            <div class=\"col-6 col-md-4\">
              <div class=\"p-2 border rounded small\">HSA: <strong>{{ '%.2f'|format(payroll_summary.hsa) }}</strong></div>
            </div>
            <div class=\"col-6 col-md-4\">
              <div class=\"p-2 border rounded small\">ESPP: <strong>{{ '%.2f'|format(payroll_summary.espp) }}</strong></div>
            </div>
            <div class=\"col-6 col-md-4\">
              <div class=\"p-2 border rounded small\">Tax: <strong>{{ '%.2f'|format(payroll_summary.tax) }}</strong></div>
            </div>
          </div>
          <div class=\"table-responsive\">
            <table class=\"table table-sm align-middle\">
              <thead><tr><th>Date</th><th>Gross</th><th>Tax</th><th>401k</th><th>HSA</th><th>ESPP</th><th>Other</th><th>Net</th><th></th></tr></thead>
              <tbody>
              {% for p in payroll_rows %}
                <tr>
                  <td class=\"text-nowrap\">{{ p.pay_date }}</td>
                  <td>{{ '%.2f'|format(p.gross or 0) }}</td>
                  <td>{{ '%.2f'|format(p.tax or 0) }}</td>
                  <td>{{ '%.2f'|format(p.k401 or 0) }}</td>
                  <td>{{ '%.2f'|format(p.hsa or 0) }}</td>
                  <td>{{ '%.2f'|format(p.espp or 0) }}</td>
                  <td>{{ '%.2f'|format(p.other or 0) }}</td>
                  <td class=\"fw-semibold\">{{ '%.2f'|format(p.net) }}</td>
                  <td class=\"text-end\">
                    <form method=\"post\" action=\"{{ url_for('payroll_delete', rowid=p.id) }}\">
                      <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
                      <button class=\"btn btn-sm btn-outline-danger\" type=\"submit\">Delete</button>
                    </form>
                  </td>
                </tr>
              {% else %}
                <tr><td colspan=\"9\" class=\"text-muted\">No payroll entries for this month.</td></tr>
              {% endfor %}
              </tbody>
            </table>
          </div>
          <div class=\"text-muted small\">Net = gross - tax - 401k - HSA - ESPP - other.</div>
        </div>
      </div>
    </div>
  </div>

  <div class=\"row g-4\">
    <div class=\"col-12 col-xxl-4\" id=\"spend\">
      <div class=\"card shadow-sm\">
        <div class=\"card-body\">
          <h5 class=\"card-title\">Add transaction</h5>
          <form method=\"post\" action=\"{{ url_for('add') }}\" class=\"needs-validation\" novalidate>
            <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
            <div class=\"mb-3\">
              <label class=\"form-label\">Category <span class=\"text-danger\">*</span></label>
              <input type=\"text\" class=\"form-control\" name=\"category\" required>
              <div class=\"invalid-feedback\">Please provide a category.</div>
            </div>
            <div class=\"mb-3\">
              <label class=\"form-label\">Amount <span class=\"text-danger\">*</span></label>
              <input type=\"number\" step=\"any\" class=\"form-control\" name=\"amount\" required>
              <div class=\"form-text\">Enter expenses as positive numbers; app stores them as negative.</div>
              <div class=\"invalid-feedback\">Please provide a valid number.</div>
            </div>
            <div class=\"mb-3\">
              <label class=\"form-label\">Description (optional)</label>
              <input type=\"text\" class=\"form-control\" name=\"description\" placeholder=\"e.g., Groceries at Market\">
            </div>
            <button class=\"btn btn-primary\" type=\"submit\">Add</button>
          </form>
        </div>
      </div>

      <div class=\"card mt-4 shadow-sm\">
        <div class=\"card-body\">
          <div class=\"d-flex justify-content-between align-items-center\">
            <h5 class=\"card-title mb-0\">Subscriptions</h5>
            <form method=\"post\" action=\"{{ url_for('subs_apply') }}\" class=\"ms-2\">
              <input type=\"hidden\" name=\"month\" value=\"{{ month }}\">
              <button class=\"btn btn-sm btn-outline-success\" type=\"submit\">Apply to {{ month }}</button>
            </form>
          </div>
          <form method=\"post\" action=\"{{ url_for('subs_add') }}\" class=\"row row-cols-1 g-2 mt-2\">
            <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
            <div class=\"col\"><input class=\"form-control\" name=\"name\" placeholder=\"Name (e.g., Netflix)\" required></div>
            <div class=\"col\"><input class=\"form-control\" name=\"category\" placeholder=\"Category\" required></div>
            <div class=\"col\"><input class=\"form-control\" name=\"amount\" placeholder=\"Amount\" type=\"number\" step=\"any\" required></div>
            <div class=\"col\"><input class=\"form-control\" name=\"day_of_month\" placeholder=\"Day (1-31)\" type=\"number\" min=\"1\" max=\"31\" required></div>
            <div class=\"col\"><button class=\"btn btn-outline-primary\" type=\"submit\">Add Subscription</button></div>
          </form>
          <ul class=\"list-group list-group-flush mt-3\">
            {% for s in subs %}
              <li class=\"list-group-item\">
                <form method=\"post\" action=\"{{ url_for('subs_update', rowid=s.rowid) }}\"> 
                  <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
                  <div class=\"row g-2 align-items-center\">
                    <div class=\"col-12 col-lg-3\">
                      <input class=\"form-control form-control-sm\" name=\"name\" value=\"{{ s.name }}\" required>
                    </div>
                    <div class=\"col-12 col-lg-3\">
                      <input class=\"form-control form-control-sm\" name=\"category\" value=\"{{ s.category }}\" required>
                    </div>
                    <div class=\"col-6 col-lg-2\">
                      <input class=\"form-control form-control-sm\" name=\"amount\" type=\"number\" step=\"any\" value=\"{{ '%.2f'|format(((s.amount or 0)|abs)) }}\" required>
                    </div>
                    <div class=\"col-6 col-lg-2\">
                      <input class=\"form-control form-control-sm\" name=\"day_of_month\" type=\"number\" min=\"1\" max=\"31\" value=\"{{ s.day_of_month }}\" required>
                    </div>
                    <div class=\"col-auto d-flex align-items-center gap-2\">
                      <button class=\"btn btn-sm btn-primary\" type=\"submit\">Save</button>
                      {% if s.active %}
                        <span class=\"badge text-bg-success\">Active</span>
                      {% else %}
                        <span class=\"badge text-bg-secondary\">Inactive</span>
                      {% endif %}
                    </div>
                  </div>
                </form>
                <div class=\"d-flex justify-content-end gap-2 mt-2\">
                  <form method=\"post\" action=\"{{ url_for('subs_toggle', rowid=s.rowid) }}\">
                    <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
                    <button class=\"btn btn-sm btn-outline-secondary\" type=\"submit\">Toggle</button>
                  </form>
                  <form method=\"post\" action=\"{{ url_for('subs_delete', rowid=s.rowid) }}\" onsubmit=\"return confirm('Delete this subscription?');\">
                    <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
                    <button class=\"btn btn-sm btn-outline-danger\" type=\"submit\">Delete</button>
                  </form>
                </div>
              </li>
            {% else %}
              <li class=\"list-group-item\"><span class=\"muted\">No subscriptions yet</span></li>
            {% endfor %}
          </ul>
        </div>
      </div>

      <div class=\"card mt-4 shadow-sm\">
        <div class=\"card-body\">
          <h5 class=\"card-title\">Budget ({{ month }})</h5>
          <form class=\"row row-cols-1 g-2 align-items-end\" method=\"post\" action=\"{{ url_for('income_set') }}\">
            <input type=\"hidden\" name=\"month\" value=\"{{ month }}\">
            <div class=\"col\">
              <label class=\"form-label\">Monthly income</label>
              <input class=\"form-control\" type=\"number\" step=\"any\" name=\"income\" value=\"{{ income or '' }}\" placeholder=\"e.g., 5000\">
            </div>
            <div class=\"col\">
              <button class=\"btn btn-outline-primary\" type=\"submit\">Save income</button>
            </div>
          </form>
          <hr/>
          <form class=\"row row-cols-3 g-2 align-items-end\" method=\"post\" action=\"{{ url_for('targets_set') }}\">
            <div>
              <label class=\"form-label\">Needs %</label>
              <input class=\"form-control\" name=\"needs\" type=\"number\" min=\"0\" max=\"100\" step=\"any\" value=\"{{ targets.needs }}\">
            </div>
            <div>
              <label class=\"form-label\">Wants %</label>
              <input class=\"form-control\" name=\"wants\" type=\"number\" min=\"0\" max=\"100\" step=\"any\" value=\"{{ targets.wants }}\">
            </div>
            <div>
              <label class=\"form-label\">Savings %</label>
              <input class=\"form-control\" name=\"savings\" type=\"number\" min=\"0\" max=\"100\" step=\"any\" value=\"{{ targets.savings }}\">
            </div>
            <div class=\"col-12\">
              <button class=\"btn btn-outline-secondary\" type=\"submit\">Save targets</button>
            </div>
          </form>
          <div class=\"mt-3\">
            <div class=\"small text-muted\">Planned vs actual (actual uses spending magnitudes):</div>
            <ul class=\"list-group list-group-flush\">
              {% for meta in meta_summary %}
              <li class=\"list-group-item d-flex justify-content-between\">
                <span><strong>{{ meta.name }}</strong> — planned {{ '%.2f'|format(meta.planned) }}</span>
                <span>actual {{ '%.2f'|format(meta.actual) }} · remaining {{ '%.2f'|format(meta.remaining) }}</span>
              </li>
              {% endfor %}
            </ul>
          </div>
        </div>
      </div>

      <div class=\"card mt-4 shadow-sm\">
        <div class=\"card-body\">
          <h5 class=\"card-title\">Map sub-category → meta</h5>
          <form class=\"row row-cols-1 g-2\" method=\"post\" action=\"{{ url_for('meta_map') }}\">
            <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
            <div class=\"col\"><input class=\"form-control\" name=\"category\" placeholder=\"e.g., Car\" required></div>
            <div class=\"col\">
              <select class=\"form-select\" name=\"meta\" required>
                <option value=\"\" disabled selected>Select meta</option>
                {% for m in meta_allowed %}
                  <option value=\"{{ m }}\">{{ m }}</option>
                {% endfor %}
              </select>
            </div>
            <div class=\"col\"><button class=\"btn btn-outline-primary\" type=\"submit\">Map</button></div>
          </form>
          <ul class=\"list-group list-group-flush mt-3\">
            {% for cat, meta in mappings.items() %}
              <li class=\"list-group-item d-flex justify-content-between\">
                <span>{{ cat }} → <strong>{{ meta }}</strong></span>
                <form method=\"post\" action=\"{{ url_for('meta_unmap') }}\">
                  <input type=\"hidden\" name=\"category\" value=\"{{ cat }}\">
                  <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
                  <button class=\"btn btn-sm btn-outline-danger\" type=\"submit\">Unmap</button>
                </form>
              </li>
            {% else %}
              <li class=\"list-group-item\"><span class=\"muted\">No mappings yet</span></li>
            {% endfor %}
          </ul>
        </div>
      </div>
    </div>

    <div class=\"col-12 col-xxl-8\">
      <div class=\"card shadow-sm mb-4\" id=\"buckets\">
        <div class=\"card-body\">
          <div class=\"d-flex justify-content-between align-items-center mb-2\">
            <h5 class=\"card-title mb-0\">Funding Buckets</h5>
            <a class=\"btn btn-sm btn-outline-secondary\" href=\"{{ url_for('buckets_index') }}\">Open full view</a>
          </div>
          <form class=\"row row-cols-1 row-cols-lg-5 g-2 mb-3\" method=\"post\" action=\"{{ url_for('buckets_add') }}\">
            <input type=\"hidden\" name=\"_redirect\" value=\"index\">
            <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
            <div class=\"col\">
              <input class=\"form-control form-control-sm\" name=\"name\" placeholder=\"Name\" required>
            </div>
            <div class=\"col\">
              <input class=\"form-control form-control-sm\" name=\"category\" placeholder=\"Category\" required>
            </div>
            <div class=\"col\">
              <input class=\"form-control form-control-sm\" name=\"goal\" type=\"number\" step=\"any\" min=\"0\" placeholder=\"Goal\" required>
            </div>
            <div class=\"col\">
              <select class=\"form-select form-select-sm\" name=\"meta\">
                <option value=\"\">Meta</option>
                {% for m in meta_allowed %}
                  <option value=\"{{ m }}\">{{ m }}</option>
                {% endfor %}
              </select>
            </div>
            <div class=\"col d-flex justify-content-end\">
              <button class=\"btn btn-sm btn-primary\" type=\"submit\">Add</button>
            </div>
          </form>
          <div class=\"row g-3\">
            <div class=\"col-12 col-lg-6\">
              <div class=\"d-flex justify-content-between align-items-center mb-2\">
                <h6 class=\"mb-0\">Filling</h6>
                <span class=\"badge text-bg-secondary\">{{ bucket_totals.filling_count }}</span>
              </div>
              {% for b in bucket_filling %}
                <div class=\"mb-3 p-2 border rounded\">
                  <div class=\"d-flex justify-content-between align-items-center\">
                    <div>
                      <div class=\"fw-semibold\">{{ b.name }}</div>
                      <div class=\"text-muted small\">{{ b.category or 'Uncategorized' }}{% if b.meta %} · {{ b.meta }}{% endif %}</div>
                    </div>
                    <div class=\"text-muted small\">{{ '%.0f'|format(b.progress_pct) }}%</div>
                  </div>
                  <div class=\"progress my-2\" style=\"height: 6px;\">
                    <div class=\"progress-bar\" role=\"progressbar\" style=\"width: {{ b.progress_pct }}%;\"></div>
                  </div>
                  <div class=\"d-flex justify-content-between align-items-center\">
                    <div class=\"text-muted small\">{{ '%.2f'|format(b.current) }} / {{ '%.2f'|format(b.goal) }}</div>
                    <form class=\"d-flex gap-2\" method=\"post\" action=\"{{ url_for('buckets_contribute', bucket_id=b.id) }}\">
                      <input type=\"hidden\" name=\"_redirect\" value=\"index\">
                      <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
                      <input class=\"form-control form-control-sm\" name=\"amount\" type=\"number\" step=\"any\" min=\"0\" placeholder=\"Add\" required>
                      <button class=\"btn btn-sm btn-outline-primary\" type=\"submit\">Add</button>
                    </form>
                  </div>
                </div>
              {% else %}
                <div class=\"text-muted small\">No filling buckets yet.</div>
              {% endfor %}
            </div>
            <div class=\"col-12 col-lg-6\">
              <div class=\"d-flex justify-content-between align-items-center mb-2\">
                <h6 class=\"mb-0\">Ready to spend</h6>
                <span class=\"badge text-bg-success\">{{ bucket_totals.ready_count }}</span>
              </div>
              {% for b in bucket_ready %}
                <div class=\"mb-3 p-2 border rounded\">
                  <div class=\"d-flex justify-content-between align-items-center\">
                    <div>
                      <div class=\"fw-semibold\">{{ b.name }}</div>
                      <div class=\"text-muted small\">{{ b.category or 'Uncategorized' }}{% if b.meta %} · {{ b.meta }}{% endif %}</div>
                    </div>
                    <span class=\"badge text-bg-success\">Ready</span>
                  </div>
                  <div class=\"progress my-2\" style=\"height: 6px;\">
                    <div class=\"progress-bar bg-success\" role=\"progressbar\" style=\"width: {{ b.progress_pct }}%;\"></div>
                  </div>
                  <div class=\"d-flex justify-content-between align-items-center\">
                    <div class=\"text-muted small\">{{ '%.2f'|format(b.current) }} / {{ '%.2f'|format(b.goal) }}</div>
                    <form method=\"post\" action=\"{{ url_for('buckets_spend', bucket_id=b.id) }}\">
                      <input type=\"hidden\" name=\"_redirect\" value=\"index\">
                      <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
                      <button class=\"btn btn-sm btn-success\" type=\"submit\">Spend &amp; archive</button>
                    </form>
                  </div>
                </div>
              {% else %}
                <div class=\"text-muted small\">No buckets ready yet.</div>
              {% endfor %}
              {% if bucket_recent %}
                <div class=\"mt-3\">
                  <div class=\"text-muted small mb-1\">Recently completed</div>
                  <ul class=\"list-group list-group-flush\">
                    {% for b in bucket_recent %}
                      <li class=\"list-group-item d-flex justify-content-between align-items-center px-0\">
                        <span>{{ b.name }} <span class=\"badge text-bg-secondary ms-2\">{{ b.status }}</span> <span class=\"text-muted small ms-2\">{{ b.category or 'Uncategorized' }}{% if b.meta %} · {{ b.meta }}{% endif %}</span></span>
                        <span class=\"text-muted small\">{{ b.updated_at }}</span>
                      </li>
                    {% endfor %}
                  </ul>
                </div>
              {% endif %}
            </div>
          </div>
        </div>
      </div>

      <div class=\"row g-4\">
        <div class=\"col-12 col-lg-6\">
          <div class=\"card shadow-sm chart-card\">
            <div class=\"card-body\">
              <h6 class=\"card-title\">Meta (Needs/Wants/Savings)</h6>
              <div class=\"chart-wrap\"><canvas id=\"pie_meta\"></canvas></div>
            </div>
          </div>
        </div>
        <div class=\"col-12 col-lg-6\">
          <div class=\"card shadow-sm chart-card\">
            <div class=\"card-body\">
              <h6 class=\"card-title\">Sub-categories (spend)</h6>
              <div class=\"chart-wrap\"><canvas id=\"pie_sub\"></canvas></div>
            </div>
          </div>
        </div>
      </div>

      <div class=\"card mt-4 shadow-sm\">
        <div class=\"card-body\">
          <h5 class=\"card-title\">Transactions ({{ month }})</h5>
          <div class=\"table-responsive\">
            <table class=\"table table-sm align-middle\">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Description</th>
                  <th>Category</th>
                  <th class=\"text-end\">Amount</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {% for tx in transactions %}
                  <tr>
                    <td class=\"text-nowrap\">{{ tx.date }}</td>
                    <td>{{ tx.description or '' }}</td>
                    <td>{{ tx.category or '' }}</td>
                    <td class=\"text-end {{ 'amount-neg' if (tx.amount or 0) < 0 else 'amount-pos' }}\">{{ '%.2f'|format(tx.amount or 0) }}</td>
                    <td class=\"text-end\">
                      <form method=\"post\" action=\"{{ url_for('delete', rowid=tx.rowid) }}?month={{ month }}\" onsubmit=\"return confirm('Delete this transaction?');\">
                        <button class=\"btn btn-outline-danger btn-sm\" type=\"submit\">Delete</button>
                      </form>
                    </td>
                  </tr>
                {% else %}
                  <tr><td colspan=\"5\" class=\"text-center text-muted\">No transactions for this month</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
          <div class=\"mt-2 text-end\"><strong>Overall:</strong> {{ '%.2f'|format(month_total) }}</div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const pieMeta = {{ pie_meta | safe }};
const pieSub  = {{ pie_sub  | safe }};

function renderPie(elId, data) {
  if (!data.labels.length) return;
  const ctx = document.getElementById(elId);
  new Chart(ctx, {
    type: 'pie',
    data: { labels: data.labels, datasets: [{ data: data.values }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } } }
  });
}
renderPie('pie_meta', pieMeta);
renderPie('pie_sub', pieSub);

(() => { // Client-side validation
  const forms = document.querySelectorAll('.needs-validation')
  Array.from(forms).forEach(form => {
    form.addEventListener('submit', event => {
      if (!form.checkValidity()) { event.preventDefault(); event.stopPropagation(); }
      form.classList.add('was-validated')
    }, false)
  })
})()
</script>
</body>
</html>
"""

    PAGE_BUCKETS = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Funding Buckets</title>
  <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
  <link href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css\" rel=\"stylesheet\">
  <style>
    body { padding-top: 1.5rem; }
  </style>
</head>
<body>
<div class=\"container py-3\">
  <div class=\"d-flex justify-content-between align-items-center mb-3\">
    <h1 class=\"h3 mb-0\">Funding Buckets</h1>
    <a class=\"btn btn-outline-secondary\" href=\"{{ url_for('index') }}\">Back to Transactions</a>
  </div>

  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <div class=\"alert alert-info\">{{ messages[0] }}</div>
    {% endif %}
  {% endwith %}

  <div class=\"card mb-4 shadow-sm\">
    <div class=\"card-body\">
      <h5 class=\"card-title\">Create bucket</h5>
      <form class=\"row row-cols-1 row-cols-md-4 g-3\" method=\"post\" action=\"{{ url_for('buckets_add') }}\">
        <div class=\"col\">
          <label class=\"form-label\">Name</label>
          <input class=\"form-control\" name=\"name\" placeholder=\"e.g., Vacation\" required>
        </div>
        <div class=\"col\">
          <label class=\"form-label\">Category</label>
          <input class=\"form-control\" name=\"category\" placeholder=\"e.g., Tech\" required>
        </div>
        <div class=\"col\">
          <label class=\"form-label\">Goal amount</label>
          <input class=\"form-control\" name=\"goal\" type=\"number\" step=\"any\" min=\"0\" placeholder=\"e.g., 1500\" required>
        </div>
        <div class=\"col\">
          <label class=\"form-label\">Meta (optional)</label>
          <select class=\"form-select\" name=\"meta\">
            <option value=\"\">Auto</option>
            {% for m in meta_allowed %}
              <option value=\"{{ m }}\">{{ m }}</option>
            {% endfor %}
          </select>
        </div>
        <div class=\"col d-flex align-items-end\">
          <button class=\"btn btn-primary\" type=\"submit\">Add bucket</button>
        </div>
      </form>
    </div>
  </div>

  <div class=\"row g-3\">
    <div class=\"col-12 col-lg-6\">
      <h5 class=\"mb-2\">Filling</h5>
      {% for b in filling %}
        <div class=\"card mb-3 shadow-sm\">
          <div class=\"card-body\">
            <div class=\"d-flex justify-content-between align-items-start\">
              <div>
                <h6 class=\"mb-1\">{{ b.name }}</h6>
                <div class=\"text-muted small\">{{ b.category or 'Uncategorized' }}{% if b.meta %} · {{ b.meta }}{% endif %} · Status: {{ b.status }}</div>
              </div>
              <div class=\"d-flex align-items-start gap-2\">
                <div class=\"text-end fw-semibold\">{{ '%.2f'|format(b.current) }} / {{ '%.2f'|format(b.goal) }}</div>
                <div class=\"btn-group dropstart\">
                  <button class=\"btn btn-sm btn-outline-secondary dropdown-toggle\" type=\"button\" data-bs-toggle=\"dropdown\" aria-expanded=\"false\">⋮</button>
                  <ul class=\"dropdown-menu\">
                    <li><button class=\"dropdown-item\" type=\"button\" data-bs-toggle=\"collapse\" data-bs-target=\"#edit-b{{ b.id }}\">Edit</button></li>
                    <li>
                      <form method=\"post\" action=\"{{ url_for('buckets_delete', bucket_id=b.id) }}\" onsubmit=\"return confirm('Delete this bucket?');\">
                        {% if show_archived %}<input type=\"hidden\" name=\"show_archived\" value=\"1\">{% endif %}
                        <button class=\"dropdown-item text-danger\" type=\"submit\">Delete</button>
                      </form>
                    </li>
                  </ul>
                </div>
              </div>
            </div>
            <div class=\"progress my-2\" style=\"height: 8px;\">
              <div class=\"progress-bar\" role=\"progressbar\" style=\"width: {{ b.progress_pct }}%;\"></div>
            </div>
            <form class=\"row row-cols-1 row-cols-sm-2 g-2\" method=\"post\" action=\"{{ url_for('buckets_contribute', bucket_id=b.id) }}\">
              {% if show_archived %}<input type=\"hidden\" name=\"show_archived\" value=\"1\">{% endif %}
              <div class=\"col\">
                <input class=\"form-control\" name=\"amount\" type=\"number\" step=\"any\" min=\"0\" placeholder=\"Add amount\" required>
              </div>
              <div class=\"col d-flex gap-2\">
                <button class=\"btn btn-outline-primary\" type=\"submit\">Contribute</button>
                {% if b.status == 'ready' %}
                  <span class=\"badge text-bg-success ms-auto\">Ready</span>
                {% endif %}
              </div>
            </form>
            <div class=\"collapse mt-3\" id=\"edit-b{{ b.id }}\">
              <form class=\"row row-cols-1 row-cols-sm-3 g-2\" method=\"post\" action=\"{{ url_for('buckets_edit', bucket_id=b.id) }}\">
                {% if show_archived %}<input type=\"hidden\" name=\"show_archived\" value=\"1\">{% endif %}
                <div class=\"col\">
                  <label class=\"form-label\">Name</label>
                  <input class=\"form-control\" name=\"name\" value=\"{{ b.name }}\" required>
                </div>
                <div class=\"col\">
                  <label class=\"form-label\">Category</label>
                  <input class=\"form-control\" name=\"category\" value=\"{{ b.category }}\" required>
                </div>
                <div class=\"col\">
                  <label class=\"form-label\">Goal</label>
                  <input class=\"form-control\" name=\"goal\" type=\"number\" step=\"any\" min=\"0\" value=\"{{ '%.2f'|format(b.goal) }}\" required>
                </div>
                <div class=\"col\">
                  <label class=\"form-label\">Meta</label>
                  <select class=\"form-select\" name=\"meta\">
                    <option value=\"\">Auto</option>
                    {% for m in meta_allowed %}
                      <option value=\"{{ m }}\" {% if b.meta == m %}selected{% endif %}>{{ m }}</option>
                    {% endfor %}
                  </select>
                </div>
                <div class=\"col d-flex align-items-end\">
                  <button class=\"btn btn-outline-secondary\" type=\"submit\">Save</button>
                </div>
              </form>
            </div>
          </div>
        </div>
      {% else %}
        <div class=\"text-muted\">No filling buckets yet.</div>
      {% endfor %}
    </div>

    <div class=\"col-12 col-lg-6\">
      <h5 class=\"mb-2\">Ready to Spend</h5>
      {% for b in ready %}
        <div class=\"card mb-3 shadow-sm\">
          <div class=\"card-body\">
            <div class=\"d-flex justify-content-between align-items-start\">
              <div>
                <h6 class=\"mb-1\">{{ b.name }}</h6>
                <div class=\"text-muted small\">{{ b.category or 'Uncategorized' }}{% if b.meta %} · {{ b.meta }}{% endif %} · Status: {{ b.status }}</div>
              </div>
              <div class=\"d-flex align-items-start gap-2\">
                <div class=\"text-end fw-semibold\">{{ '%.2f'|format(b.current) }} / {{ '%.2f'|format(b.goal) }}</div>
                <div class=\"btn-group dropstart\">
                  <button class=\"btn btn-sm btn-outline-secondary dropdown-toggle\" type=\"button\" data-bs-toggle=\"dropdown\" aria-expanded=\"false\">⋮</button>
                  <ul class=\"dropdown-menu\">
                    <li><button class=\"dropdown-item\" type=\"button\" data-bs-toggle=\"collapse\" data-bs-target=\"#edit-b{{ b.id }}\">Edit</button></li>
                    <li>
                      <form method=\"post\" action=\"{{ url_for('buckets_delete', bucket_id=b.id) }}\" onsubmit=\"return confirm('Delete this bucket?');\">
                        {% if show_archived %}<input type=\"hidden\" name=\"show_archived\" value=\"1\">{% endif %}
                        <button class=\"dropdown-item text-danger\" type=\"submit\">Delete</button>
                      </form>
                    </li>
                  </ul>
                </div>
              </div>
            </div>
            <div class=\"progress my-2\" style=\"height: 8px;\">
              <div class=\"progress-bar bg-success\" role=\"progressbar\" style=\"width: {{ b.progress_pct }}%;\"></div>
            </div>
            <form method=\"post\" action=\"{{ url_for('buckets_spend', bucket_id=b.id) }}\">
              {% if show_archived %}<input type=\"hidden\" name=\"show_archived\" value=\"1\">{% endif %}
              <button class=\"btn btn-success\" type=\"submit\">Spend &amp; Archive</button>
            </form>
            <div class=\"collapse mt-3\" id=\"edit-b{{ b.id }}\">
              <form class=\"row row-cols-1 row-cols-sm-3 g-2\" method=\"post\" action=\"{{ url_for('buckets_edit', bucket_id=b.id) }}\">
                {% if show_archived %}<input type=\"hidden\" name=\"show_archived\" value=\"1\">{% endif %}
                <div class=\"col\">
                  <label class=\"form-label\">Name</label>
                  <input class=\"form-control\" name=\"name\" value=\"{{ b.name }}\" required>
                </div>
                <div class=\"col\">
                  <label class=\"form-label\">Category</label>
                  <input class=\"form-control\" name=\"category\" value=\"{{ b.category }}\" required>
                </div>
                <div class=\"col\">
                  <label class=\"form-label\">Goal</label>
                  <input class=\"form-control\" name=\"goal\" type=\"number\" step=\"any\" min=\"0\" value=\"{{ '%.2f'|format(b.goal) }}\" required>
                </div>
                <div class=\"col\">
                  <label class=\"form-label\">Meta</label>
                  <select class=\"form-select\" name=\"meta\">
                    <option value=\"\">Auto</option>
                    {% for m in meta_allowed %}
                      <option value=\"{{ m }}\" {% if b.meta == m %}selected{% endif %}>{{ m }}</option>
                    {% endfor %}
                  </select>
                </div>
                <div class=\"col d-flex align-items-end\">
                  <button class=\"btn btn-outline-secondary\" type=\"submit\">Save</button>
                </div>
              </form>
            </div>
          </div>
        </div>
      {% else %}
        <div class=\"text-muted\">No buckets ready yet.</div>
      {% endfor %}

      <div class=\"mt-4\">
        <div class=\"d-flex justify-content-between align-items-center mb-2\">
          <h6 class=\"mb-0\">Recently Completed</h6>
          {% if not show_archived %}
            <a class=\"btn btn-sm btn-outline-secondary\" href=\"{{ url_for('buckets_index', show_archived=1) }}\">Show archived</a>
          {% else %}
            <a class=\"btn btn-sm btn-outline-secondary\" href=\"{{ url_for('buckets_index') }}\">Hide archived</a>
          {% endif %}
        </div>
        {% for b in completed %}
          <div class=\"card mb-3 shadow-sm\">
            <div class=\"card-body\">
              <div class=\"d-flex justify-content-between align-items-start\">
                <div>
                  <h6 class=\"mb-1\">{{ b.name }}</h6>
                  <div class=\"text-muted small\">{{ b.category or 'Uncategorized' }}{% if b.meta %} · {{ b.meta }}{% endif %} · {{ b.status }} · Updated {{ b.updated_at }}</div>
                </div>
                <div class=\"btn-group dropstart\">
                  <button class=\"btn btn-sm btn-outline-secondary dropdown-toggle\" type=\"button\" data-bs-toggle=\"dropdown\" aria-expanded=\"false\">⋮</button>
                  <ul class=\"dropdown-menu\">
                    <li><button class=\"dropdown-item\" type=\"button\" data-bs-toggle=\"collapse\" data-bs-target=\"#edit-b{{ b.id }}\">Edit</button></li>
                    <li>
                      <form method=\"post\" action=\"{{ url_for('buckets_delete', bucket_id=b.id) }}\" onsubmit=\"return confirm('Delete this bucket?');\">
                        {% if show_archived %}<input type=\"hidden\" name=\"show_archived\" value=\"1\">{% endif %}
                        <button class=\"dropdown-item text-danger\" type=\"submit\">Delete</button>
                      </form>
                    </li>
                  </ul>
                </div>
              </div>
              <div class=\"collapse mt-3\" id=\"edit-b{{ b.id }}\">
                <form class=\"row row-cols-1 row-cols-sm-3 g-2\" method=\"post\" action=\"{{ url_for('buckets_edit', bucket_id=b.id) }}\">
                  {% if show_archived %}<input type=\"hidden\" name=\"show_archived\" value=\"1\">{% endif %}
                  <div class=\"col\">
                    <label class=\"form-label\">Name</label>
                    <input class=\"form-control\" name=\"name\" value=\"{{ b.name }}\" required>
                  </div>
                  <div class=\"col\">
                    <label class=\"form-label\">Category</label>
                    <input class=\"form-control\" name=\"category\" value=\"{{ b.category }}\" required>
                  </div>
                  <div class=\"col\">
                    <label class=\"form-label\">Goal</label>
                    <input class=\"form-control\" name=\"goal\" type=\"number\" step=\"any\" min=\"0\" value=\"{{ '%.2f'|format(b.goal) }}\" required>
                  </div>
                  <div class=\"col\">
                    <label class=\"form-label\">Meta</label>
                    <select class=\"form-select\" name=\"meta\">
                      <option value=\"\">Auto</option>
                      {% for m in meta_allowed %}
                        <option value=\"{{ m }}\" {% if b.meta == m %}selected{% endif %}>{{ m }}</option>
                      {% endfor %}
                    </select>
                  </div>
                  <div class=\"col d-flex align-items-end\">
                    <button class=\"btn btn-outline-secondary\" type=\"submit\">Save</button>
                  </div>
                </form>
              </div>
            </div>
          </div>
        {% else %}
          <div class=\"text-muted\">No completed buckets yet.</div>
        {% endfor %}

        {% if show_archived %}
          <div class=\"mt-3\">
            <h6>Archived</h6>
            {% for b in archived %}
              <div class=\"card mb-3 shadow-sm\">
                <div class=\"card-body\">
                  <div class=\"d-flex justify-content-between align-items-start\">
                    <div>
                      <h6 class=\"mb-1\">{{ b.name }}</h6>
                      <div class=\"text-muted small\">{{ b.category or 'Uncategorized' }}{% if b.meta %} · {{ b.meta }}{% endif %} · archived · Updated {{ b.updated_at }}</div>
                    </div>
                    <div class=\"btn-group dropstart\">
                      <button class=\"btn btn-sm btn-outline-secondary dropdown-toggle\" type=\"button\" data-bs-toggle=\"dropdown\" aria-expanded=\"false\">⋮</button>
                      <ul class=\"dropdown-menu\">
                        <li><button class=\"dropdown-item\" type=\"button\" data-bs-toggle=\"collapse\" data-bs-target=\"#edit-b{{ b.id }}\">Edit</button></li>
                        <li>
                          <form method=\"post\" action=\"{{ url_for('buckets_delete', bucket_id=b.id) }}\" onsubmit=\"return confirm('Delete this bucket?');\">
                            <input type=\"hidden\" name=\"show_archived\" value=\"1\">
                            <button class=\"dropdown-item text-danger\" type=\"submit\">Delete</button>
                          </form>
                        </li>
                      </ul>
                    </div>
                  </div>
                  <div class=\"collapse mt-3\" id=\"edit-b{{ b.id }}\">
                    <form class=\"row row-cols-1 row-cols-sm-3 g-2\" method=\"post\" action=\"{{ url_for('buckets_edit', bucket_id=b.id) }}\">
                      <input type=\"hidden\" name=\"show_archived\" value=\"1\">
                      <div class=\"col\">
                        <label class=\"form-label\">Name</label>
                        <input class=\"form-control\" name=\"name\" value=\"{{ b.name }}\" required>
                      </div>
                      <div class=\"col\">
                        <label class=\"form-label\">Category</label>
                        <input class=\"form-control\" name=\"category\" value=\"{{ b.category }}\" required>
                      </div>
                      <div class=\"col\">
                        <label class=\"form-label\">Goal</label>
                        <input class=\"form-control\" name=\"goal\" type=\"number\" step=\"any\" min=\"0\" value=\"{{ '%.2f'|format(b.goal) }}\" required>
                      </div>
                      <div class=\"col\">
                        <label class=\"form-label\">Meta</label>
                        <select class=\"form-select\" name=\"meta\">
                          <option value=\"\">Auto</option>
                          {% for m in meta_allowed %}
                            <option value=\"{{ m }}\" {% if b.meta == m %}selected{% endif %}>{{ m }}</option>
                          {% endfor %}
                        </select>
                      </div>
                      <div class=\"col d-flex align-items-end\">
                        <button class=\"btn btn-outline-secondary\" type=\"submit\">Save</button>
                      </div>
                    </form>
                  </div>
                </div>
              </div>
            {% else %}
              <div class=\"text-muted\">No archived buckets.</div>
            {% endfor %}
          </div>
        {% endif %}
      </div>
    </div>
  </div>
</div>
<script src=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js\"></script>
</body>
</html>
"""

    def _enrich_bucket_rows(rows, mappings=None):
        enriched = []
        for r in rows:
            data = dict(r)
            goal = float(data.get("goal") or 0.0)
            current = float(data.get("current") or 0.0)
            pct = 0.0 if goal <= 0 else min(100.0, round((current / goal) * 100, 2))
            data["goal"] = goal
            data["current"] = current
            data["progress_pct"] = pct
            cat = (data.get("category") or "").strip()
            data["category"] = cat
            data["meta"] = mappings.get(cat) if mappings else None
            enriched.append(data)
        return enriched

    @app.get("/buckets")
    def buckets_index():
        show_archived = request.args.get("show_archived") == "1"
        with engine.connect() as conn:
            mappings = dict(conn.execute(text(f"SELECT category, meta FROM {TABLE_META_MAP} ORDER BY category")).fetchall())
            filling_rows = conn.execute(text(f"""
                SELECT id, name, category, goal, current, status, created_at, updated_at
                FROM {TABLE_BUCKETS}
                WHERE status = 'filling'
                ORDER BY datetime(created_at) DESC
            """)).mappings().all()
            ready_rows = conn.execute(text(f"""
                SELECT id, name, category, goal, current, status, created_at, updated_at
                FROM {TABLE_BUCKETS}
                WHERE status = 'ready'
                ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC
            """)).mappings().all()
            completed_rows = conn.execute(text(f"""
                SELECT id, name, category, goal, current, status, created_at, updated_at
                FROM {TABLE_BUCKETS}
                WHERE status IN ('spent','archived')
                ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC
                LIMIT 10
            """)).mappings().all()
            archived_rows = []
            if show_archived:
                archived_rows = conn.execute(text(f"""
                    SELECT id, name, category, goal, current, status, created_at, updated_at
                    FROM {TABLE_BUCKETS}
                    WHERE status = 'archived'
                    ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC
                """)).mappings().all()

        return render_template_string(
            PAGE_BUCKETS,
            filling=_enrich_bucket_rows(filling_rows, mappings),
            ready=_enrich_bucket_rows(ready_rows, mappings),
            completed=_enrich_bucket_rows(completed_rows, mappings),
            archived=_enrich_bucket_rows(archived_rows, mappings),
            show_archived=show_archived,
            meta_allowed=META_ALLOWED,
        )

    @app.post("/buckets/add")
    def buckets_add():
        name = (request.form.get("name") or "").strip()
        category = (request.form.get("category") or "").strip()
        goal_raw = (request.form.get("goal") or "").strip()
        meta_choice = (request.form.get("meta") or "").strip()
        redirect_to = (request.form.get("_redirect") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()
        try:
            goal = float(goal_raw)
            if goal <= 0:
                raise ValueError
        except ValueError:
            flash("Goal must be a positive number.")
            return redirect(url_for("index", month=redirect_month) if redirect_to == "index" else url_for("buckets_index"))
        if not name or not category:
            flash("Bucket name and category are required.")
            return redirect(url_for("index", month=redirect_month) if redirect_to == "index" else url_for("buckets_index"))
        with engine.begin() as conn:
            conn.execute(text(f"""
                INSERT INTO {TABLE_BUCKETS} (name, category, goal, current, status)
                VALUES (:name, :category, :goal, 0, 'filling')
            """), {"name": name, "category": category, "goal": goal})
            if meta_choice in META_ALLOWED:
                conn.execute(
                    text(f"INSERT INTO {TABLE_META_MAP} (category, meta) VALUES (:c, :m) ON CONFLICT(category) DO UPDATE SET meta=excluded.meta"),
                    {"c": category, "m": meta_choice},
                )
        flash("Bucket created.")
        if redirect_to == "index":
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
        return redirect(url_for("buckets_index"))

    @app.post("/buckets/contribute/<int:bucket_id>")
    def buckets_contribute(bucket_id: int):
        show_archived = (request.form.get("show_archived") or "").strip() == "1"
        amount_raw = (request.form.get("amount") or "").strip()
        redirect_to = (request.form.get("_redirect") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()
        try:
            amount = float(amount_raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            flash("Contribution must be a positive number.")
            if redirect_to == "index":
                return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
            return redirect(url_for("buckets_index", show_archived=1) if show_archived else url_for("buckets_index"))

        with engine.begin() as conn:
            bucket = conn.execute(text(f"""
                SELECT id, goal, current, status FROM {TABLE_BUCKETS} WHERE id = :id
            """), {"id": bucket_id}).mappings().first()
            if not bucket:
                flash("Bucket not found.")
                if redirect_to == "index":
                    return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
                return redirect(url_for("buckets_index", show_archived=1) if show_archived else url_for("buckets_index"))

            goal = float(bucket["goal"] or 0.0)
            new_current = float(bucket["current"] or 0.0) + amount
            status = bucket["status"]
            if status not in ("spent", "archived") and goal > 0 and new_current >= goal:
                status = "ready"

            now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(text(f"""
                UPDATE {TABLE_BUCKETS}
                SET current = :cur,
                    status = :status,
                    updated_at = :updated_at
                WHERE id = :id
            """), {"cur": new_current, "status": status, "updated_at": now_ts, "id": bucket_id})
        flash("Contribution added.")
        if redirect_to == "index":
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
        return redirect(url_for("buckets_index", show_archived=1) if show_archived else url_for("buckets_index"))

    @app.post("/buckets/edit/<int:bucket_id>")
    def buckets_edit(bucket_id: int):
        show_archived = (request.form.get("show_archived") or "").strip() == "1"
        name = (request.form.get("name") or "").strip()
        category = (request.form.get("category") or "").strip()
        goal_raw = (request.form.get("goal") or "").strip()
        meta_choice = (request.form.get("meta") or "").strip()
        redirect_to = (request.form.get("_redirect") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()
        try:
            goal = float(goal_raw)
            if goal <= 0:
                raise ValueError
        except ValueError:
            flash("Goal must be a positive number.")
            if redirect_to == "index":
                return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
            return redirect(url_for("buckets_index", show_archived=1) if show_archived else url_for("buckets_index"))
        if not name or not category:
            flash("Bucket name and category are required.")
            if redirect_to == "index":
                return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
            return redirect(url_for("buckets_index", show_archived=1) if show_archived else url_for("buckets_index"))

        with engine.begin() as conn:
            bucket = conn.execute(text(f"""
                SELECT id, goal, current, status FROM {TABLE_BUCKETS} WHERE id = :id
            """), {"id": bucket_id}).mappings().first()
            if not bucket:
                flash("Bucket not found.")
                if redirect_to == "index":
                    return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
                return redirect(url_for("buckets_index", show_archived=1) if show_archived else url_for("buckets_index"))
            current = float(bucket["current"] or 0.0)
            status = bucket["status"]
            if status not in ("spent", "archived") and current >= goal and goal > 0:
                status = "ready"
            now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(text(f"""
                UPDATE {TABLE_BUCKETS}
                SET name = :name,
                    category = :category,
                    goal = :goal,
                    status = :status,
                    updated_at = :updated_at
                WHERE id = :id
            """), {"name": name, "category": category, "goal": goal, "status": status, "updated_at": now_ts, "id": bucket_id})
            if meta_choice in META_ALLOWED:
                conn.execute(
                    text(f"INSERT INTO {TABLE_META_MAP} (category, meta) VALUES (:c, :m) ON CONFLICT(category) DO UPDATE SET meta=excluded.meta"),
                    {"c": category, "m": meta_choice},
                )
        flash("Bucket updated.")
        if redirect_to == "index":
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
        return redirect(url_for("buckets_index", show_archived=1) if show_archived else url_for("buckets_index"))

    @app.post("/buckets/spend/<int:bucket_id>")
    def buckets_spend(bucket_id: int):
        show_archived = (request.form.get("show_archived") or "").strip() == "1"
        redirect_to = (request.form.get("_redirect") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()
        now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with engine.begin() as conn:
            exists = conn.execute(text(f"SELECT 1 FROM {TABLE_BUCKETS} WHERE id = :id"), {"id": bucket_id}).first()
            if not exists:
                flash("Bucket not found.")
                if redirect_to == "index":
                    return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
                return redirect(url_for("buckets_index", show_archived=1) if show_archived else url_for("buckets_index"))
            conn.execute(text(f"""
                UPDATE {TABLE_BUCKETS}
                SET status = 'archived',
                    current = 0,
                    updated_at = :updated_at
                WHERE id = :id
            """), {"updated_at": now_ts, "id": bucket_id})
        flash("Bucket archived.")
        if redirect_to == "index":
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
        return redirect(url_for("buckets_index", show_archived=1) if show_archived else url_for("buckets_index"))

    @app.post("/buckets/delete/<int:bucket_id>")
    def buckets_delete(bucket_id: int):
        show_archived = (request.form.get("show_archived") or "").strip() == "1"
        redirect_to = (request.form.get("_redirect") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()
        with engine.begin() as conn:
            result = conn.execute(text(f"DELETE FROM {TABLE_BUCKETS} WHERE id = :id"), {"id": bucket_id})
        if result.rowcount:
            flash("Bucket deleted.")
        else:
            flash("Bucket not found.")
        if redirect_to == "index":
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
        return redirect(url_for("buckets_index", show_archived=1) if show_archived else url_for("buckets_index"))

    @app.get("/")
    def index():
        month = _month_param_or_current()
        prev_month, next_month = _adjacent_months(month)
        start_ts, end_ts = _month_bounds(month)
        start_date_only = start_ts.split(" ")[0]
        end_date_only = end_ts.split(" ")[0]

        with engine.connect() as conn:
            # Transactions in month
            txs = conn.execute(text(f"""
                SELECT rowid, date, description, amount, category
                FROM {TABLE_TX}
                WHERE datetime(date) BETWEEN :start AND :end
                ORDER BY datetime(date) DESC
            """), {"start": start_ts, "end": end_ts}).mappings().all()

            # Net totals by category (signed)
            totals_by_cat = conn.execute(text(f"""
                SELECT category, SUM(amount) AS total_amount
                FROM {TABLE_TX}
                WHERE datetime(date) BETWEEN :start AND :end
                GROUP BY category
                ORDER BY category
            """), {"start": start_ts, "end": end_ts}).mappings().all()

            # Spending magnitudes by category (for charts/budget)
            expenses_by_cat = conn.execute(text(f"""
                SELECT category, SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS spend
                FROM {TABLE_TX}
                WHERE datetime(date) BETWEEN :start AND :end
                GROUP BY category
                ORDER BY category
            """), {"start": start_ts, "end": end_ts}).mappings().all()
            spend_map = { (r["category"] or "Uncategorized"): float(r["spend"] or 0.0) for r in expenses_by_cat }

            # Meta mappings
            mappings = dict(conn.execute(text(f"SELECT category, meta FROM {TABLE_META_MAP} ORDER BY category")).fetchall())

            # Income & targets
            income_row = conn.execute(text(f"SELECT income FROM {TABLE_INCOME} WHERE month=:m"), {"m": month}).first()
            income = float(income_row[0]) if income_row else None
            trow = conn.execute(text(f"SELECT needs, wants, savings FROM {TABLE_TARGETS} WHERE id=1")).first()
            targets = {"needs": float(trow[0]), "wants": float(trow[1]), "savings": float(trow[2])}

            # Payroll entries (bi-weekly captures)
            payroll_entries = conn.execute(text(f"""
                SELECT id, pay_date, gross, tax, k401, hsa, espp, other, notes
                FROM {TABLE_PAYROLL}
                WHERE date(pay_date) BETWEEN date(:start) AND date(:end)
                ORDER BY date(pay_date) DESC
            """), {"start": start_date_only, "end": end_date_only}).mappings().all()

            # Subscriptions list
            subs = conn.execute(
                text(f"SELECT rowid, name, category, amount, day_of_month, active FROM {TABLE_SUB} ORDER BY name")
            ).mappings().all()

            # Buckets (overview for dashboard)
            bucket_filling_rows = conn.execute(text(f"""
                SELECT id, name, category, goal, current, status, created_at, updated_at
                FROM {TABLE_BUCKETS}
                WHERE status = 'filling'
                ORDER BY datetime(created_at) DESC
            """)).mappings().all()
            bucket_ready_rows = conn.execute(text(f"""
                SELECT id, name, category, goal, current, status, created_at, updated_at
                FROM {TABLE_BUCKETS}
                WHERE status = 'ready'
                ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC
            """)).mappings().all()
            bucket_recent_rows = conn.execute(text(f"""
                SELECT id, name, category, goal, current, status, created_at, updated_at
                FROM {TABLE_BUCKETS}
                WHERE status IN ('spent','archived')
                ORDER BY datetime(updated_at) DESC
                LIMIT 5
            """)).mappings().all()

        # Build meta totals (sum of spend magnitudes by mapped meta)
        meta_totals = {"Needs": 0.0, "Wants": 0.0, "Savings": 0.0, "Uncategorized": 0.0}
        for cat, amt in spend_map.items():
            meta = mappings.get(cat)
            if meta in META_ALLOWED:
                meta_totals[meta] += amt
            else:
                meta_totals["Uncategorized"] += amt

        # Chart datasets
        pie_meta = {
            "labels": [k for k,v in meta_totals.items() if v > 0 and k != "Uncategorized"] + (["Uncategorized"] if meta_totals["Uncategorized"]>0 else []),
            "values": [round(meta_totals[k],2) for k in meta_totals if meta_totals[k] > 0 and k != "Uncategorized"] + ([round(meta_totals["Uncategorized"],2)] if meta_totals["Uncategorized"]>0 else [])
        }
        pie_sub = {
            "labels": list(spend_map.keys()),
            "values": [round(v, 2) for v in spend_map.values()]
        }

        # Budget summary (planned from income * targets, actual from meta_totals)
        meta_summary = []
        if income is None:
            planned_needs = planned_wants = planned_savings = 0.0
        else:
            planned_needs   = income * (targets["needs"]/100.0)
            planned_wants   = income * (targets["wants"]/100.0)
            planned_savings = income * (targets["savings"]/100.0)
        actual_needs   = meta_totals["Needs"]
        actual_wants   = meta_totals["Wants"]
        actual_savings = meta_totals["Savings"]
        meta_summary.append({"name": "Needs",   "planned": planned_needs,   "actual": actual_needs,   "remaining": (planned_needs - actual_needs)})
        meta_summary.append({"name": "Wants",   "planned": planned_wants,   "actual": actual_wants,   "remaining": (planned_wants - actual_wants)})
        meta_summary.append({"name": "Savings", "planned": planned_savings, "actual": actual_savings, "remaining": (planned_savings - actual_savings)})

        # Payroll summary (bi-weekly captures)
        payroll_summary = {"gross": 0.0, "tax": 0.0, "k401": 0.0, "hsa": 0.0, "espp": 0.0, "other": 0.0, "net": 0.0}
        payroll_rows = []
        for p in payroll_entries:
            gross = float(p.get("gross") or 0.0)
            tax = float(p.get("tax") or 0.0)
            k401 = float(p.get("k401") or 0.0)
            hsa = float(p.get("hsa") or 0.0)
            espp = float(p.get("espp") or 0.0)
            other = float(p.get("other") or 0.0)
            net = gross - tax - k401 - hsa - espp - other
            payroll_summary["gross"] += gross
            payroll_summary["tax"] += tax
            payroll_summary["k401"] += k401
            payroll_summary["hsa"] += hsa
            payroll_summary["espp"] += espp
            payroll_summary["other"] += other
            payroll_summary["net"] += net
            rec = dict(p)
            rec["net"] = net
            payroll_rows.append(rec)

        # Overall month total (net)
        month_total = sum([float(r["total_amount"] or 0) for r in totals_by_cat])

        # Bucket summaries
        bucket_filling = _enrich_bucket_rows(bucket_filling_rows, mappings)
        bucket_ready = _enrich_bucket_rows(bucket_ready_rows, mappings)
        bucket_recent = _enrich_bucket_rows(bucket_recent_rows, mappings)
        active_buckets = bucket_filling + bucket_ready
        active_goal = sum([b["goal"] for b in active_buckets])
        active_current = sum([b["current"] for b in active_buckets])
        bucket_totals = {
            "goal": active_goal,
            "current": active_current,
            "progress_pct": 0.0 if active_goal <= 0 else min(100.0, round((active_current / active_goal) * 100, 2)),
            "ready_count": len(bucket_ready),
            "filling_count": len(bucket_filling),
        }

        return render_template_string(
            PAGE_TEMPLATE,
            month=month,
            prev_month=prev_month,
            next_month=next_month,
            transactions=txs,
            month_total=month_total,
            totals_by_category=totals_by_cat,
            subs=subs,
            mappings=mappings,
            targets=targets,
            income=income,
            meta_summary=meta_summary,
            meta_allowed=META_ALLOWED,
            payroll_rows=payroll_rows,
            payroll_summary=payroll_summary,
            bucket_filling=bucket_filling,
            bucket_ready=bucket_ready,
            bucket_recent=bucket_recent,
            bucket_totals=bucket_totals,
            pie_meta=json.dumps(pie_meta),
            pie_sub=json.dumps(pie_sub),
        )

    @app.post("/add")
    def add():
        category = (request.form.get("category") or "").strip()
        amount_raw = (request.form.get("amount") or "").strip()
        description = (request.form.get("description") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()

        # Parse & normalize
        try:
            amount = float(amount_raw)
        except ValueError:
            flash("Amount must be a number.")
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
        if not category:
            flash("Category is required.")
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
        # Always treat user-entered positives as expenses
        amount = -abs(amount)

        now_dt = datetime.now()
        if redirect_month:
            try:
                target_month = datetime.strptime(redirect_month, "%Y-%m")
                last_day = calendar.monthrange(target_month.year, target_month.month)[1]
                safe_day = min(now_dt.day, last_day)
                now_dt = now_dt.replace(year=target_month.year, month=target_month.month, day=safe_day)
            except ValueError:
                pass
        now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
        with engine.begin() as conn:
            conn.execute(
                text(f"""
                    INSERT INTO {TABLE_TX} (date, description, amount, category)
                    VALUES (:date, :description, :amount, :category)
                """),
                {"date": now_str, "description": description, "amount": amount, "category": category}
            )
        flash("Transaction added.")
        return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

    @app.post("/delete/<int:rowid>")
    def delete(rowid: int):
        month = (request.args.get("month") or "").strip()
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {TABLE_TX} WHERE rowid = :rowid"), {"rowid": rowid})
        flash("Transaction deleted.")
        return redirect(url_for("index", month=month) if month else url_for("index"))

    # ---- Subscriptions ----
    @app.post("/subs/add")
    def subs_add():
        name = (request.form.get("name") or "").strip()
        category = (request.form.get("category") or "").strip()
        amount_raw = (request.form.get("amount") or "").strip()
        day_raw = (request.form.get("day_of_month") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()

        if not name or not category:
            flash("Name and category are required for subscriptions.")
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
        try:
            amount = float(amount_raw)
            day = int(day_raw)
            if not (1 <= day <= 31):
                raise ValueError
        except ValueError:
            flash("Amount must be a number and day must be 1-31.")
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

        # Treat as expense by default
        amount = -abs(amount)

        with engine.begin() as conn:
            conn.execute(text(f"""
                INSERT INTO {TABLE_SUB} (name, category, amount, day_of_month, active)
                VALUES (:name, :category, :amount, :day, 1)
            """), {"name": name, "category": category, "amount": amount, "day": day})
        flash("Subscription added.")
        return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

    @app.post("/subs/update/<int:rowid>")
    def subs_update(rowid: int):
        name = (request.form.get("name") or "").strip()
        category = (request.form.get("category") or "").strip()
        amount_raw = (request.form.get("amount") or "").strip()
        day_raw = (request.form.get("day_of_month") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()

        if not name or not category:
            flash("Name and category are required for subscriptions.")
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

        try:
            amount = float(amount_raw)
            day = int(day_raw)
            if not (1 <= day <= 31):
                raise ValueError
        except ValueError:
            flash("Amount must be a number and day must be 1-31.")
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

        amount = -abs(amount)

        with engine.begin() as conn:
            result = conn.execute(text(f"""
                UPDATE {TABLE_SUB}
                SET name = :name,
                    category = :category,
                    amount = :amount,
                    day_of_month = :day
                WHERE rowid = :rowid
            """), {"name": name, "category": category, "amount": amount, "day": day, "rowid": rowid})

        if result.rowcount:
            flash("Subscription updated.")
        else:
            flash("Subscription not found.")
        return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

    @app.post("/subs/toggle/<int:rowid>")
    def subs_toggle(rowid: int):
        redirect_month = (request.form.get("_redirect_month") or "").strip()
        with engine.begin() as conn:
            result = conn.execute(text(f"""
                UPDATE {TABLE_SUB}
                SET active = CASE WHEN active=1 THEN 0 ELSE 1 END
                WHERE rowid = :rowid
            """), {"rowid": rowid})
        if result.rowcount:
            flash("Subscription toggled.")
        else:
            flash("Subscription not found.")
        return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

    @app.post("/subs/delete/<int:rowid>")
    def subs_delete(rowid: int):
        redirect_month = (request.form.get("_redirect_month") or "").strip()
        with engine.begin() as conn:
            result = conn.execute(text(f"DELETE FROM {TABLE_SUB} WHERE rowid = :rowid"), {"rowid": rowid})
        if result.rowcount:
            flash("Subscription deleted.")
        else:
            flash("Subscription not found.")
        return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

    @app.post("/subs/apply")
    def subs_apply():
        target_month = (request.form.get("month") or "").strip() or date.today().strftime("%Y-%m")
        try:
            datetime.strptime(target_month, "%Y-%m")
        except ValueError:
            target_month = date.today().strftime("%Y-%m")
        apply_subscriptions(engine, TABLE_TX, TABLE_SUB, target_month)
        flash(f"Subscriptions applied to {target_month}.")
        return redirect(url_for("index", month=target_month))

    # ---- Meta & Budgeting ----
    @app.post("/meta/map")
    def meta_map():
        category = (request.form.get("category") or "").strip()
        meta = (request.form.get("meta") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()
        if not category or meta not in META_ALLOWED:
            flash("Provide a category and a valid meta (Needs/Wants/Savings).")
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))
        with engine.begin() as conn:
            conn.execute(text(f"INSERT INTO {TABLE_META_MAP} (category, meta) VALUES (:c, :m) ON CONFLICT(category) DO UPDATE SET meta=excluded.meta"), {"c": category, "m": meta})
        flash(f"Mapped '{category}' to {meta}.")
        return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

    @app.post("/meta/unmap")
    def meta_unmap():
        category = (request.form.get("category") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {TABLE_META_MAP} WHERE category=:c"), {"c": category})
        flash(f"Unmapped '{category}'.")
        return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

    @app.post("/income/set")
    def income_set():
        month = (request.form.get("month") or "").strip()
        income_raw = (request.form.get("income") or "").strip()
        try:
            income = float(income_raw)
        except ValueError:
            flash("Income must be a number.")
            return redirect(url_for("index", month=month) if month else url_for("index"))
        with engine.begin() as conn:
            conn.execute(text(f"INSERT INTO {TABLE_INCOME} (month, income) VALUES (:m,:i) ON CONFLICT(month) DO UPDATE SET income=excluded.income"), {"m": month, "i": income})
        flash("Income saved.")
        return redirect(url_for("index", month=month) if month else url_for("index"))

    @app.post("/targets/set")
    def targets_set():
        needs = (request.form.get("needs") or "0").strip()
        wants = (request.form.get("wants") or "0").strip()
        savings = (request.form.get("savings") or "0").strip()
        try:
            n, w, s = float(needs), float(wants), float(savings)
            if abs((n + w + s) - 100.0) > 1e-6:
                raise ValueError
        except ValueError:
            flash("Targets must be numbers and sum to 100%.")
            return redirect(url_for("index"))
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE {TABLE_TARGETS} SET needs=:n, wants=:w, savings=:s WHERE id=1"), {"n": n, "w": w, "s": s})
        flash("Targets saved.")
        return redirect(url_for("index"))

    # ---- Payroll (bi-weekly captures) ----
    @app.post("/payroll/add")
    def payroll_add():
        pay_date = (request.form.get("pay_date") or "").strip()
        gross_raw = (request.form.get("gross") or "0").strip()
        tax_raw = (request.form.get("tax") or "0").strip()
        k401_raw = (request.form.get("k401") or "0").strip()
        hsa_raw = (request.form.get("hsa") or "0").strip()
        espp_raw = (request.form.get("espp") or "0").strip()
        other_raw = (request.form.get("other") or "0").strip()
        notes = (request.form.get("notes") or "").strip()
        month = (request.form.get("_redirect_month") or "").strip()
        try:
            datetime.strptime(pay_date, "%Y-%m-%d")
            gross = _parse_sum_field(gross_raw)
            tax = _parse_sum_field(tax_raw)
            k401 = _parse_sum_field(k401_raw)
            hsa = _parse_sum_field(hsa_raw)
            espp = _parse_sum_field(espp_raw)
            other = _parse_sum_field(other_raw)
        except ValueError:
            flash("Please provide a valid pay date and numeric amounts.")
            return redirect(url_for("index", month=month) if month else url_for("index"))
        with engine.begin() as conn:
            conn.execute(text(f"""
                INSERT INTO {TABLE_PAYROLL} (pay_date, gross, tax, k401, hsa, espp, other, notes)
                VALUES (:pay_date, :gross, :tax, :k401, :hsa, :espp, :other, :notes)
            """), {"pay_date": pay_date, "gross": gross, "tax": tax, "k401": k401, "hsa": hsa, "espp": espp, "other": other, "notes": notes})
        flash("Payroll entry added.")
        return redirect(url_for("index", month=month) if month else url_for("index"))

    @app.post("/payroll/delete/<int:rowid>")
    def payroll_delete(rowid: int):
        month = (request.form.get("_redirect_month") or "").strip()
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {TABLE_PAYROLL} WHERE id = :id"), {"id": rowid})
        flash("Payroll entry removed.")
        return redirect(url_for("index", month=month) if month else url_for("index"))

    # Expose engine/tables for tests
    app.config["_ENGINE"] = engine
    app.config["_TABLE_TX"] = TABLE_TX
    app.config["_TABLE_SUB"] = TABLE_SUB
    app.config["_TABLE_META_MAP"] = TABLE_META_MAP
    app.config["_TABLE_INCOME"] = TABLE_INCOME
    app.config["_TABLE_TARGETS"] = TABLE_TARGETS
    app.config["_TABLE_BUCKETS"] = TABLE_BUCKETS
    app.config["_TABLE_PAYROLL"] = TABLE_PAYROLL

    return app


# -----------------------------
# Dev server with safe port binding (debugger & reloader disabled)
# -----------------------------

def _find_free_port() -> int:
    env_port = os.environ.get("PORT")
    if env_port:
        try:
            port = int(env_port)
            if 0 <= port <= 65535:
                return port
        except ValueError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Finance tracking web application helper.")
    parser.add_argument(
        "--database",
        help="SQLAlchemy database URL to use (defaults to the bundled sqlite file).",
    )
    parser.add_argument(
        "--host",
        help="Host interface for the development server. Defaults to HOST env var or 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Port for the development server. Defaults to PORT env var or an ephemeral port.",
    )
    parser.add_argument(
        "--apply-subscriptions",
        action="store_true",
        help="Apply active subscriptions to the given (or current) month and exit.",
    )
    parser.add_argument(
        "--month",
        help="Target month in YYYY-MM format when applying subscriptions. Defaults to the current month.",
    )

    args = parser.parse_args(argv)
    app = create_app(db_url=args.database)

    if args.apply_subscriptions:
        month = _normalized_month(args.month)
        apply_subscriptions(
            app.config["_ENGINE"],
            app.config["_TABLE_TX"],
            app.config["_TABLE_SUB"],
            month,
        )
        print(f"Subscriptions applied to {month}.")
        return

    host = args.host or os.environ.get("HOST", "127.0.0.1")
    port = args.port
    if port is None:
        env_port = os.environ.get("PORT")
        if env_port:
            try:
                parsed_port = int(env_port)
                if 0 <= parsed_port <= 65535:
                    port = parsed_port
            except ValueError:
                port = None
        if port is None:
            port = _find_free_port()

    try:
        print(f"Starting server on http://{host}:{port}")
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=False)
    except SystemExit:
        print(
            "\n[!] Server failed to start (SystemExit). This environment may block sockets or the port is unavailable."
        )
        print("    - Try setting a custom port: PORT=5000 python transactions_web_app_full.py")
        print("    - Or run the test suite: python -m unittest -v transactions_web_app_full")
        sys.exit(0)


if __name__ == "__main__":
    main()


# -----------------------------
# Tests (run with: python -m unittest -v transactions_web_app_full)
# -----------------------------
class TransactionsWebAppTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.app = create_app(engine_override=engine)
        self.client = self.app.test_client()
        self.engine = engine
        self.tx_table = self.app.config["_TABLE_TX"]
        self.sub_table = self.app.config["_TABLE_SUB"]
        self.map_table = self.app.config["_TABLE_META_MAP"]
        self.income_table = self.app.config["_TABLE_INCOME"]
        self.targets_table = self.app.config["_TABLE_TARGETS"]

    def _get_rowid_for_desc(self, desc: str):
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT rowid FROM {self.tx_table} WHERE description = :d ORDER BY rowid DESC LIMIT 1"),
                {"d": desc},
            ).first()
            return row[0] if row else None

    def _get_subscription_rowid(self, name: str):
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT rowid FROM {self.sub_table} WHERE name = :n ORDER BY rowid DESC LIMIT 1"),
                {"n": name},
            ).first()
            return row[0] if row else None

    # Core tests
    def test_add_transaction_success(self):
        resp = self.client.post(
            "/add",
            data={"category": "Food", "amount": "12.34", "description": "Lunch", "_redirect_month": "2099-01"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        # Amount should be stored negative
        r = self.client.get("/?month=2099-01")
        self.assertIn(b"-12.34", r.data)

    def test_add_transaction_validation_amount(self):
        resp = self.client.post(
            "/add", data={"category": "Bills", "amount": "abc", "_redirect_month": "2099-01"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Amount must be a number.", resp.data)

    def test_add_transaction_validation_category(self):
        resp = self.client.post(
            "/add", data={"category": "", "amount": "10", "_redirect_month": "2099-01"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Category is required.", resp.data)

    def test_delete_transaction(self):
        self.client.post(
            "/add",
            data={"category": "Misc", "amount": "5", "description": "TempItem", "_redirect_month": "2099-01"},
            follow_redirects=True,
        )
        rowid = self._get_rowid_for_desc("TempItem")
        self.assertIsNotNone(rowid)
        resp = self.client.post(f"/delete/{rowid}?month=2099-01", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Transaction deleted.", resp.data)
        resp2 = self.client.get("/?month=2099-01")
        self.assertNotIn(b"TempItem", resp2.data)

    def test_totals_by_category_and_overall_label_present(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
        self.client.post("/add", data={"category": "A", "amount": "10", "_redirect_month": "2099-02"}, follow_redirects=True)
        self.client.post("/add", data={"category": "A", "amount": "5", "_redirect_month": "2099-02"}, follow_redirects=True)
        self.client.post("/add", data={"category": "B", "amount": "3", "_redirect_month": "2099-02"}, follow_redirects=True)
        resp = self.client.get("/?month=2099-02")
        self.assertIn(b"Overall:", resp.data)

    def test_delete_nonexistent_is_noop(self):
        resp = self.client.post("/delete/999999?month=2099-01", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    # Monthly filters
    def test_month_filter_isolates_results(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
        with self.engine.begin() as conn:
            conn.execute(text(f"INSERT INTO {self.tx_table} (date, description, amount, category) VALUES (:d, 'M1', -10, 'A')"), {"d": "2099-01-10 10:00:00"})
            conn.execute(text(f"INSERT INTO {self.tx_table} (date, description, amount, category) VALUES (:d, 'M2', -20, 'B')"), {"d": "2099-02-10 10:00:00"})
        r1 = self.client.get("/?month=2099-01")
        self.assertIn(b"M1", r1.data)
        self.assertNotIn(b"M2", r1.data)
        r2 = self.client.get("/?month=2099-02")
        self.assertIn(b"M2", r2.data)
        self.assertNotIn(b"M1", r2.data)

    # Subscriptions
    def test_add_subscription_and_apply_once(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
            conn.execute(text(f"DELETE FROM {self.sub_table}"))
        self.client.post(
            "/subs/add",
            data={"name": "Netflix", "category": "Entertainment", "amount": "15.99", "day_of_month": "12", "_redirect_month": "2099-03"},
            follow_redirects=True,
        )
        self.client.post("/subs/apply", data={"month": "2099-03"}, follow_redirects=True)
        with self.engine.connect() as conn:
            row = conn.execute(text(f"""
                SELECT COUNT(*) FROM {self.tx_table}
                WHERE description = 'SUB: Netflix' AND category = 'Entertainment'
                  AND date BETWEEN '2099-03-12 00:00:00' AND '2099-03-12 23:59:59'
            """)).scalar()
        self.assertEqual(row, 1)
        # Re-apply should not duplicate
        self.client.post("/subs/apply", data={"month": "2099-03"}, follow_redirects=True)
        with self.engine.connect() as conn:
            row2 = conn.execute(text(f"SELECT COUNT(*) FROM {self.tx_table} WHERE description = 'SUB: Netflix' AND category = 'Entertainment' AND date LIKE '2099-03-12 %'"))\
                .scalar()
        self.assertEqual(row2, 1)

    def test_subscription_day_clamped_to_month_length(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
            conn.execute(text(f"DELETE FROM {self.sub_table}"))
            conn.execute(text(f"INSERT INTO {self.sub_table} (name, category, amount, day_of_month, active) VALUES ('Rent', 'Housing', 1000, 31, 1)"))
        # Amount will be normalized to -1000 on apply
        self.client.post("/subs/apply", data={"month": "2099-02"}, follow_redirects=True)
        with self.engine.connect() as conn:
            exists = conn.execute(text(f"SELECT 1 FROM {self.tx_table} WHERE description='SUB: Rent' AND date LIKE '2099-02-28 %'"))\
                .first()
        self.assertIsNotNone(exists)

    # Budgeting/meta
    def test_set_income_targets_mapping_and_charts(self):
        self.client.post("/income/set", data={"month": "2099-04", "income": "4000"}, follow_redirects=True)
        self.client.post("/targets/set", data={"needs": "50", "wants": "30", "savings": "20"}, follow_redirects=True)
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
            conn.execute(text(f"DELETE FROM {self.map_table}"))
        # Add expenses as positives; app stores negatives
        self.client.post("/add", data={"category": "Car", "amount": "200", "_redirect_month": "2099-04"}, follow_redirects=True)
        self.client.post("/add", data={"category": "Food", "amount": "300", "_redirect_month": "2099-04"}, follow_redirects=True)
        self.client.post("/add", data={"category": "Entertainment", "amount": "100", "_redirect_month": "2099-04"}, follow_redirects=True)
        self.client.post("/meta/map", data={"category": "Car", "meta": "Needs", "_redirect_month": "2099-04"}, follow_redirects=True)
        self.client.post("/meta/map", data={"category": "Food", "meta": "Needs", "_redirect_month": "2099-04"}, follow_redirects=True)
        self.client.post("/meta/map", data={"category": "Entertainment", "meta": "Wants", "_redirect_month": "2099-04"}, follow_redirects=True)
        resp = self.client.get("/?month=2099-04")
        self.assertIn(b'pie_meta', resp.data)
        self.assertIn(b'pie_sub', resp.data)

    # Charts & normalization consistency
    def test_pie_sub_uses_positive_spend_magnitudes(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
        self.client.post("/add", data={"category": "Food", "amount": "10", "_redirect_month": "2099-06"}, follow_redirects=True)
        self.client.post("/add", data={"category": "Car", "amount": "5", "_redirect_month": "2099-06"}, follow_redirects=True)
        r = self.client.get("/?month=2099-06")
        self.assertTrue(b'"values": [10.0, 5.0]' in r.data or b'"values": [5.0, 10.0]' in r.data)

    def test_subs_add_normalizes_to_negative(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.sub_table}"))
        self.client.post(
            "/subs/add",
            data={"name": "Gym", "category": "Fitness", "amount": "20", "day_of_month": "3", "_redirect_month": "2099-07"},
            follow_redirects=True,
        )
        with self.engine.connect() as conn:
            amt = conn.execute(text(f"SELECT amount FROM {self.sub_table} WHERE name='Gym'"))\
                .scalar()
        self.assertLess(amt, 0)

    def test_subs_update_edits_subscription(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.sub_table}"))
        self.client.post(
            "/subs/add",
            data={"name": "Gym", "category": "Fitness", "amount": "20", "day_of_month": "3", "_redirect_month": "2099-07"},
            follow_redirects=True,
        )
        rowid = self._get_subscription_rowid("Gym")
        self.assertIsNotNone(rowid)
        resp = self.client.post(
            f"/subs/update/{rowid}",
            data={
                "name": "Gym Plus",
                "category": "Health",
                "amount": "30",
                "day_of_month": "10",
                "_redirect_month": "2099-07",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Subscription updated.", resp.data)
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT name, category, amount, day_of_month FROM {self.sub_table} WHERE rowid = :rowid"),
                {"rowid": rowid},
            ).mappings().first()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Gym Plus")
        self.assertEqual(row["category"], "Health")
        self.assertLess(row["amount"], 0)
        self.assertEqual(row["day_of_month"], 10)

    def test_subs_toggle_flips_active(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.sub_table}"))
        self.client.post(
            "/subs/add",
            data={"name": "Gym", "category": "Fitness", "amount": "20", "day_of_month": "3", "_redirect_month": "2099-07"},
            follow_redirects=True,
        )
        rowid = self._get_subscription_rowid("Gym")
        self.assertIsNotNone(rowid)
        resp = self.client.post(
            f"/subs/toggle/{rowid}",
            data={"_redirect_month": "2099-07"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        with self.engine.connect() as conn:
            active = conn.execute(
                text(f"SELECT active FROM {self.sub_table} WHERE rowid = :rowid"),
                {"rowid": rowid},
            ).scalar()
        self.assertEqual(active, 0)

    def test_subs_delete_removes_subscription(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.sub_table}"))
        self.client.post(
            "/subs/add",
            data={"name": "Gym", "category": "Fitness", "amount": "20", "day_of_month": "3", "_redirect_month": "2099-07"},
            follow_redirects=True,
        )
        rowid = self._get_subscription_rowid("Gym")
        self.assertIsNotNone(rowid)
        resp = self.client.post(
            f"/subs/delete/{rowid}",
            data={"_redirect_month": "2099-07"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Subscription deleted.", resp.data)
        with self.engine.connect() as conn:
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM {self.sub_table} WHERE rowid = :rowid"),
                {"rowid": rowid},
            ).scalar()
        self.assertEqual(count, 0)

    def test_apply_normalizes_direct_db_positive_amounts(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
            conn.execute(text(f"DELETE FROM {self.sub_table}"))
            conn.execute(text(f"INSERT INTO {self.sub_table} (name, category, amount, day_of_month, active) VALUES ('Direct', 'Other', 25, 30, 1)"))
        self.client.post("/subs/apply", data={"month": "2099-08"}, follow_redirects=True)
        with self.engine.connect() as conn:
            row = conn.execute(text(f"SELECT amount FROM {self.tx_table} WHERE description='SUB: Direct'"))\
                .first()
        self.assertIsNotNone(row)
        self.assertLess(row[0], 0)


if __name__ == "__main__":
    unittest.main()
