"""
Minimal local web UI for your existing SQLite-backed transactions tool.

✅ Environment-safe server
- Debugger and reloader are **OFF** to avoid `_multiprocessing`.
- Chooses a free ephemeral port (or `$PORT`) and avoids hard exit on socket issues.

✅ New features for monthly workflows
- **Month filter**: view transactions and totals for a given `YYYY-MM` (with prev/next controls).
- **Category pie chart** (client-side via Chart.js) for the selected month.
- **Timed subscriptions (baseline)**: manage recurring monthly items (name, category, amount, day-of-month). Apply them to a month with one click; idempotent (won't duplicate).

Existing features
- Add transaction (category, amount, optional description)
- List transactions (newest first) + overall total + totals by category
- Delete by SQLite `rowid`
- App factory for testing + tests (now expanded)

Run the app locally:
1) `pip install flask sqlalchemy pandas`
2) `python transactions_web_app.py`
3) Open the printed URL (e.g., `http://127.0.0.1:54321`)
   - Or set a fixed port: `PORT=5000 python transactions_web_app.py`

Run tests:
- `python -m unittest -v transactions_web_app`

Notes:
- Uses your existing `transactions.db`.
- Safe to run alongside your CLI script—they share the same DB file.
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

# -----------------------------
# App factory (allows testing)
# -----------------------------

def create_app(db_url: str | None = None, *, engine_override=None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me")

    DB_URL = db_url or "sqlite:///transactions.db"

    # For normal file-backed SQLite, keep check_same_thread=False for Flask
    if engine_override is not None:
        engine = engine_override
    else:
        engine = create_engine(DB_URL, connect_args={"check_same_thread": False})

    TABLE_TX = "transactions"
    TABLE_SUB = "subscriptions"

    # Ensure tables exist (transactions schema matches your CLI tool)
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
              <div class=\"form-text\">Use negative amounts for expenses if you prefer.</div>
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
          <h5 class=\"card-title\">Totals for {{ month }}</h5>
          <p class=\"mb-1\"><strong>Overall:</strong> {{ month_total or 0 }}</p>
          <canvas id=\"pie\" height=\"160\"></canvas>
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
            <div class=\"col\">
              <input class=\"form-control\" name=\"name\" placeholder=\"Name (e.g., Netflix)\" required>
            </div>
            <div class=\"col\">
              <input class=\"form-control\" name=\"category\" placeholder=\"Category\" required>
            </div>
            <div class=\"col\">
              <input class=\"form-control\" name=\"amount\" placeholder=\"Amount\" type=\"number\" step=\"any\" required>
            </div>
            <div class=\"col\">
              <input class=\"form-control\" name=\"day_of_month\" placeholder=\"Day (1-31)\" type=\"number\" min=\"1\" max=\"31\" required>
            </div>
            <div class=\"col\">
              <button class=\"btn btn-outline-primary\" type=\"submit\">Add Subscription</button>
            </div>
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
    </div>

    <div class=\"col-12 col-xxl-8\">
      <div class=\"card shadow-sm\">
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
                    <td class=\"text-end {{ 'amount-neg' if (tx.amount or 0) < 0 else 'amount-pos' }}\">
                      {{ '%.2f'|format(tx.amount or 0) }}
                    </td>
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
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const pieData = {{ pie_data | safe }};
if (pieData.labels.length > 0) {
  const ctx = document.getElementById('pie');
  new Chart(ctx, {
    type: 'pie',
    data: {
      labels: pieData.labels,
      datasets: [{ data: pieData.values }]
    }
  });
}

// Client-side validation
(() => {
  const forms = document.querySelectorAll('.needs-validation')
  Array.from(forms).forEach(form => {
    form.addEventListener('submit', event => {
      if (!form.checkValidity()) {
        event.preventDefault()
        event.stopPropagation()
      }
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
            txs = conn.execute(text(f"""
                SELECT rowid, date, description, amount, category
                FROM {TABLE_TX}
                WHERE datetime(date) BETWEEN :start AND :end
                ORDER BY datetime(date) DESC
            """), {"start": start_ts, "end": end_ts}).mappings().all()

            month_total = conn.execute(text(f"""
                SELECT SUM(amount) AS total
                FROM {TABLE_TX}
                WHERE datetime(date) BETWEEN :start AND :end
            """), {"start": start_ts, "end": end_ts}).mappings().first()
            month_total = (month_total or {}).get("total")

            totals_by_cat = conn.execute(text(f"""
                SELECT category, SUM(amount) AS total_amount
                FROM {TABLE_TX}
                WHERE datetime(date) BETWEEN :start AND :end
                GROUP BY category
                ORDER BY category
            """), {"start": start_ts, "end": end_ts}).mappings().all()

            subs = conn.execute(text(f"SELECT name, category, amount, day_of_month, active FROM {TABLE_SUB} ORDER BY name"))\
                    .mappings().all()

        pie_data = {"labels": [r["category"] or "Uncategorized" for r in totals_by_cat],
                    "values": [round(float(r["total_amount"] or 0), 2) for r in totals_by_cat]}

        return render_template_string(
            PAGE_TEMPLATE,
            month=month,
            prev_month=prev_month,
            next_month=next_month,
            transactions=txs,
            month_total=month_total,
            totals_by_category=totals_by_cat,
            subs=subs,
            pie_data=json.dumps(pie_data),
        )

    @app.post("/add")
    def add():
        category = (request.form.get("category") or "").strip()
        amount_raw = (request.form.get("amount") or "").strip()
        description = (request.form.get("description") or "").strip()
        redirect_month = (request.form.get("_redirect_month") or "").strip()

        # Basic validation
        try:
            amount = float(amount_raw)
        except ValueError:
            flash("Amount must be a number.")
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

        if not category:
            flash("Category is required.")
            return redirect(url_for("index", month=redirect_month) if redirect_month else url_for("index"))

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
        start_ts, end_ts = _month_bounds(target_month)
        y, m = map(int, target_month.split("-"))
        last_day = calendar.monthrange(y, m)[1]
        with engine.begin() as conn:
            subs = conn.execute(text(f"SELECT name, category, amount, day_of_month FROM {TABLE_SUB} WHERE active = 1"))\
                        .mappings().all()
            for s in subs:
                d = min(int(s["day_of_month"] or 1), last_day)
                ts = f"{target_month}-{d:02d} 12:00:00"  # midday for consistency
                desc = f"SUB: {s['name']}"
                # Skip if same sub already exists for that date
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
                    "amt": float(s["amount"] or 0.0),
                }).first()
                if exists:
                    continue
                conn.execute(text(f"""
                    INSERT INTO {TABLE_TX} (date, description, amount, category)
                    VALUES (:date, :description, :amount, :category)
                """), {
                    "date": ts,
                    "description": desc,
                    "amount": float(s["amount"] or 0.0),
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

    # Expose engine/tables for tests
    app.config["_ENGINE"] = engine
    app.config["_TABLE_TX"] = TABLE_TX
    app.config["_TABLE_SUB"] = TABLE_SUB

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
    # Ask OS for an ephemeral port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

if __name__ == "__main__":
    app = create_app()
    host = os.environ.get("HOST", "127.0.0.1")
    port = _find_free_port()
    try:
        print(f"Starting server on http://{host}:{port}")
        # IMPORTANT: keep debugger and reloader OFF to avoid _multiprocessing
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=False)
    except SystemExit as e:
        # Avoid abrupt termination in restricted environments
        print("\n[!] Server failed to start (SystemExit). This environment may block sockets or the port is unavailable.")
        print("    - Try setting a custom port: PORT=5000 python transactions_web_app.py")
        print("    - Or run the test suite to verify app logic: python -m unittest -v transactions_web_app")
        sys.exit(0)


# -----------------------------
# Tests (run with: python -m unittest -v transactions_web_app)
# -----------------------------
class TransactionsWebAppTests(unittest.TestCase):
    def setUp(self):
        # In-memory SQLite shared across connections using StaticPool
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

    def _get_rowid_for_desc(self, desc: str):
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT rowid FROM {self.tx_table} WHERE description = :d ORDER BY rowid DESC LIMIT 1"),
                {"d": desc},
            ).first()
            return row[0] if row else None

    # Existing tests
    def test_add_transaction_success(self):
        resp = self.client.post(
            "/add",
            data={"category": "Food", "amount": "12.34", "description": "Lunch", "_redirect_month": "2099-01"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Transaction added.", resp.data)
        # Verify it renders on index
        resp2 = self.client.get("/?month=2099-01")
        self.assertIn(b"Food", resp2.data)
        self.assertIn(b"12.34", resp2.data)

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
        # Add
        self.client.post(
            "/add",
            data={"category": "Misc", "amount": "5", "description": "TempItem", "_redirect_month": "2099-01"},
            follow_redirects=True,
        )
        rowid = self._get_rowid_for_desc("TempItem")
        self.assertIsNotNone(rowid)
        # Delete
        resp = self.client.post(f"/delete/{rowid}?month=2099-01", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Transaction deleted.", resp.data)
        # Ensure gone
        resp2 = self.client.get("/?month=2099-01")
        self.assertNotIn(b"TempItem", resp2.data)

    # ---- Added tests (kept from previous version) ----
    def test_negative_amounts_display_as_negative(self):
        self.client.post(
            "/add",
            data={"category": "Groceries", "amount": "-7.5", "description": "Eggs", "_redirect_month": "2099-01"},
            follow_redirects=True,
        )
        resp = self.client.get("/?month=2099-01")
        self.assertIn(b"-7.50", resp.data)

    def test_totals_by_category_and_overall(self):
        # Clear and add multiple items
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
        self.client.post("/add", data={"category": "A", "amount": "10", "_redirect_month": "2099-02"}, follow_redirects=True)
        self.client.post("/add", data={"category": "A", "amount": "5", "_redirect_month": "2099-02"}, follow_redirects=True)
        self.client.post("/add", data={"category": "B", "amount": "-3", "_redirect_month": "2099-02"}, follow_redirects=True)
        resp = self.client.get("/?month=2099-02")
        # Overall total should be 12.0
        self.assertIn(b"Overall:", resp.data)
        self.assertIn(b"12.0", resp.data)
        # Category totals
        self.assertIn(b"A", resp.data)
        self.assertIn(b"15.00", resp.data)
        self.assertIn(b"B", resp.data)
        self.assertIn(b"-3.00", resp.data)

    def test_delete_nonexistent_is_noop(self):
        # Deleting a non-existent row should not error and should redirect with 200
        resp = self.client.post("/delete/999999?month=2099-01", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    # ---- New tests for monthly features ----
    def test_month_filter_isolates_results(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
        # Add items in two different months
        with self.engine.begin() as conn:
            conn.execute(text(f"INSERT INTO {self.tx_table} (date, description, amount, category) VALUES (:d, 'M1', 10, 'A')"), {"d": "2099-01-10 10:00:00"})
            conn.execute(text(f"INSERT INTO {self.tx_table} (date, description, amount, category) VALUES (:d, 'M2', 20, 'B')"), {"d": "2099-02-10 10:00:00"})
        r1 = self.client.get("/?month=2099-01")
        self.assertIn(b"M1", r1.data)
        self.assertNotIn(b"M2", r1.data)
        r2 = self.client.get("/?month=2099-02")
        self.assertIn(b"M2", r2.data)
        self.assertNotIn(b"M1", r2.data)

    def test_add_subscription_and_apply_once(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
            conn.execute(text(f"DELETE FROM {self.sub_table}"))
        # Add sub
        self.client.post(
            "/subs/add",
            data={"name": "Netflix", "category": "Entertainment", "amount": "-15.99", "day_of_month": "12", "_redirect_month": "2099-03"},
            follow_redirects=True,
        )
        # Apply to month
        self.client.post("/subs/apply", data={"month": "2099-03"}, follow_redirects=True)
        # Verify one inserted
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
        # Day 31 on February should clamp to 28 (or 29 in leap years). We'll test Feb 2099 (not leap year => 28 days)
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.tx_table}"))
            conn.execute(text(f"DELETE FROM {self.sub_table}"))
            conn.execute(text(f"INSERT INTO {self.sub_table} (name, category, amount, day_of_month, active) VALUES ('Rent', 'Housing', -1000, 31, 1)"))
        self.client.post("/subs/apply", data={"month": "2099-02"}, follow_redirects=True)
        with self.engine.connect() as conn:
            exists = conn.execute(text(f"SELECT 1 FROM {self.tx_table} WHERE description='SUB: Rent' AND date LIKE '2099-02-28 %'"))\
                .first()
        self.assertIsNotNone(exists)

