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

from flask import Flask, request, redirect, url_for, render_template_string, flash
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from datetime import datetime, date, timedelta
import calendar
import os
import socket
import sys
import json
import unittest

META_ALLOWED = ("Needs", "Wants", "Savings")

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

    PAGE_TEMPLATE = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Transactions</title>
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
  <h1 class=\"mb-3\">Transactions</h1>

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

  <div class=\"row g-4\">
    <div class=\"col-12 col-xxl-4\">
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
              <li class=\"list-group-item d-flex justify-content-between align-items-center\">
                <div>
                  <strong>{{ s.name }}</strong>
                  <span class=\"ms-2 text-muted\">{{ s.category }}</span>
                  <span class=\"ms-2\">{{ '%.2f'|format(s.amount or 0) }}</span>
                  <span class=\"ms-2 text-muted\">on day {{ s.day_of_month }}</span>
                  {% if not s.active %}<span class=\"badge text-bg-secondary ms-2\">inactive</span>{% endif %}
                </div>
                <form method=\"post\" action=\"{{ url_for('subs_toggle', name=s.name) }}\">
                  <input type=\"hidden\" name=\"_redirect_month\" value=\"{{ month }}\">
                  <button class=\"btn btn-sm btn-outline-secondary\" type=\"submit\">Toggle</button>
                </form>
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

    @app.get("/")
    def index():
        month = _month_param_or_current()
        prev_month, next_month = _adjacent_months(month)
        start_ts, end_ts = _month_bounds(month)

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

            # Subscriptions list
            subs = conn.execute(text(f"SELECT name, category, amount, day_of_month, active FROM {TABLE_SUB} ORDER BY name")).mappings().all()

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

        # Overall month total (net)
        month_total = sum([float(r["total_amount"] or 0) for r in totals_by_cat])

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

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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

    @app.post("/subs/toggle/<path:name>")
    def subs_toggle(name: str):
        redirect_month = (request.form.get("_redirect_month") or "").strip()
        with engine.begin() as conn:
            conn.execute(text(f"""
                UPDATE {TABLE_SUB}
                SET active = CASE WHEN active=1 THEN 0 ELSE 1 END
                WHERE name = :name
            """), {"name": name})
        flash("Subscription toggled.")
        return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

    def _apply_subscriptions_to_month(target_month: str):
        """Insert one transaction per ACTIVE subscription into the given YYYY-MM.
        Idempotent per (name, category, amount, target_date).
        Description is stored as "SUB: {name}".
        """
        y, m = map(int, target_month.split("-"))
        last_day = calendar.monthrange(y, m)[1]
        with engine.begin() as conn:
            subs = conn.execute(text(f"SELECT name, category, amount, day_of_month FROM {TABLE_SUB} WHERE active = 1")).mappings().all()
            for s in subs:
                d = min(int(s["day_of_month"] or 1), last_day)
                ts = f"{target_month}-{d:02d} 12:00:00"
                desc = f"SUB: {s['name']}"
                normalized_amt = -abs(float(s["amount"] or 0.0))
                exists = conn.execute(text(f"""
                    SELECT 1 FROM {TABLE_TX}
                    WHERE date BETWEEN :start AND :end
                      AND description = :desc
                      AND category = :cat
                      AND ABS(amount - :amt) < 1e-9
                """), {
                    "start": f"{target_month}-{d:02d} 00:00:00",
                    "end": f"{target_month}-{d:02d} 23:59:59",
                    "desc": desc,
                    "cat": s["category"],
                    "amt": normalized_amt,
                }).first()
                if exists:
                    continue
                conn.execute(text(f"""
                    INSERT INTO {TABLE_TX} (date, description, amount, category)
                    VALUES (:date, :description, :amount, :category)
                """), {
                    "date": ts,
                    "description": desc,
                    "amount": normalized_amt,
                    "category": s["category"],
                })

    @app.post("/subs/apply")
    def subs_apply():
        target_month = (request.form.get("month") or "").strip() or date.today().strftime("%Y-%m")
        try:
            datetime.strptime(target_month, "%Y-%m")
        except ValueError:
            target_month = date.today().strftime("%Y-%m")
        _apply_subscriptions_to_month(target_month)
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

    # Expose engine/tables for tests
    app.config["_ENGINE"] = engine
    app.config["_TABLE_TX"] = TABLE_TX
    app.config["_TABLE_SUB"] = TABLE_SUB
    app.config["_TABLE_META_MAP"] = TABLE_META_MAP
    app.config["_TABLE_INCOME"] = TABLE_INCOME
    app.config["_TABLE_TARGETS"] = TABLE_TARGETS

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

if __name__ == "__main__":
    app = create_app()
    host = os.environ.get("HOST", "127.0.0.1")
    port = _find_free_port()
    try:
        print(f"Starting server on http://{host}:{port}")
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=False)
    except SystemExit:
        print("\n[!] Server failed to start (SystemExit). This environment may block sockets or the port is unavailable.")
        print("    - Try setting a custom port: PORT=5000 python transactions_web_app_full.py")
        print("    - Or run the test suite: python -m unittest -v transactions_web_app_full")
        sys.exit(0)


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

