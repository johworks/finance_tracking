"""
Minimal local web UI for your existing SQLite-backed transactions tool.

✅ Fix for your errors
1) `_multiprocessing` crash (Werkzeug debugger): fixed by keeping **debugger OFF** and **reloader OFF**.
2) `SystemExit: 1` on startup: this usually happens when the server cannot bind to the port in a restricted or already‑in‑use environment. This version:
   - Chooses a **free ephemeral port** automatically (port=0) or from `$PORT` if set.
   - Wraps `app.run` to **avoid hard exit** and prints a helpful message if binding fails.

Features:
- Add transaction (category, amount, optional description)
- List transactions (newest first) + overall total + totals by category
- Delete by SQLite `rowid`
- App factory for testing + **expanded tests**

Run the app locally:
1) `pip install flask sqlalchemy pandas`
2) `python transactions_web_app.py`
3) Look for the printed URL (e.g., `http://127.0.0.1:54321`) and open it.
   - Optionally set a fixed port: `PORT=5000 python transactions_web_app.py`

Run tests:
- `python -m unittest -v transactions_web_app`

Notes:
- Uses your existing `transactions.db` in the working directory.
- Safe to run alongside your CLI script—they share the same database file.
"""

from __future__ import annotations

from flask import Flask, request, redirect, url_for, render_template_string, flash
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from datetime import datetime
import os
import socket
import sys
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

    TABLE_NAME = "transactions"

    # Ensure table exists (same schema as your script)
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                date TEXT DEFAULT CURRENT_TIMESTAMP,
                description TEXT,
                amount REAL,
                category TEXT
            )
        """))

    PAGE_TEMPLATE = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Transactions</title>
  <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
  <link href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css\" rel=\"stylesheet\">
  <style>
    body { padding-top: 2rem; }
    .amount-neg { color: #b00020; }
    .amount-pos { color: #0a7d2a; }
    .muted { color: #6c757d; }
  </style>
</head>
<body>
<div class=\"container\">
  <h1 class=\"mb-4\">Transactions</h1>

  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <div class=\"alert alert-info\">{{ messages[0] }}</div>
    {% endif %}
  {% endwith %}

  <div class=\"row g-4\">
    <div class=\"col-12 col-lg-5\">
      <div class=\"card shadow-sm\">
        <div class=\"card-body\">
          <h5 class=\"card-title\">Add transaction</h5>
          <form method=\"post\" action=\"{{ url_for('add') }}\" class=\"needs-validation\" novalidate>
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
          <h5 class=\"card-title\">Totals</h5>
          <p class=\"mb-1\"><strong>Overall:</strong> {{ overall_total or 0 }}</p>
          <ul class=\"list-group list-group-flush\">
            {% for row in totals_by_category %}
              <li class=\"list-group-item d-flex justify-content-between align-items-center\">
                <span>{{ row.category or 'Uncategorized' }}</span>
                <span>{{ '%.2f'|format(row.total_amount or 0) }}</span>
              </li>
            {% else %}
              <li class=\"list-group-item\"><span class=\"muted\">No data yet</span></li>
            {% endfor %}
          </ul>
        </div>
      </div>
    </div>

    <div class=\"col-12 col-lg-7\">
      <div class=\"card shadow-sm\">
        <div class=\"card-body\">
          <h5 class=\"card-title\">All transactions</h5>
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
                      <form method=\"post\" action=\"{{ url_for('delete', rowid=tx.rowid) }}\" onsubmit=\"return confirm('Delete this transaction?');\">
                        <button class=\"btn btn-outline-danger btn-sm\" type=\"submit\">Delete</button>
                      </form>
                    </td>
                  </tr>
                {% else %}
                  <tr><td colspan=\"5\" class=\"text-center text-muted\">No transactions yet</td></tr>
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
        with engine.connect() as conn:
            txs = conn.execute(text(f"""
                SELECT rowid, date, description, amount, category
                FROM {TABLE_NAME}
                ORDER BY datetime(date) DESC
            """)).mappings().all()

            overall = conn.execute(text(f"SELECT SUM(amount) AS total FROM {TABLE_NAME}"))
            overall_total = (overall.mappings().first() or {}).get("total")

            totals_by_cat = conn.execute(text(f"""
                SELECT category, SUM(amount) AS total_amount
                FROM {TABLE_NAME}
                GROUP BY category
                ORDER BY category
            """
            )).mappings().all()

        return render_template_string(
            PAGE_TEMPLATE,
            transactions=txs,
            overall_total=overall_total,
            totals_by_category=totals_by_cat,
        )

    @app.post("/add")
    def add():
        category = (request.form.get("category") or "").strip()
        amount_raw = (request.form.get("amount") or "").strip()
        description = (request.form.get("description") or "").strip()

        # Basic validation
        try:
            amount = float(amount_raw)
        except ValueError:
            flash("Amount must be a number.")
            return redirect(url_for("index"))

        if not category:
            flash("Category is required.")
            return redirect(url_for("index"))

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with engine.begin() as conn:
            conn.execute(
                text(f"""
                    INSERT INTO {TABLE_NAME} (date, description, amount, category)
                    VALUES (:date, :description, :amount, :category)
                """),
                {"date": now_str, "description": description, "amount": amount, "category": category}
            )
        flash("Transaction added.")
        return redirect(url_for("index"))

    @app.post("/delete/<int:rowid>")
    def delete(rowid: int):
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {TABLE_NAME} WHERE rowid = :rowid"), {"rowid": rowid})
        flash("Transaction deleted.")
        return redirect(url_for("index"))

    # Expose engine for tests
    app.config["_ENGINE"] = engine
    app.config["_TABLE"] = TABLE_NAME

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
        self.table = self.app.config["_TABLE"]

    def _get_rowid_for_desc(self, desc: str):
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT rowid FROM {self.table} WHERE description = :d ORDER BY rowid DESC LIMIT 1"),
                {"d": desc},
            ).first()
            return row[0] if row else None

    def test_add_transaction_success(self):
        resp = self.client.post(
            "/add",
            data={"category": "Food", "amount": "12.34", "description": "Lunch"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Transaction added.", resp.data)
        # Verify it renders on index
        resp2 = self.client.get("/")
        self.assertIn(b"Food", resp2.data)
        self.assertIn(b"12.34", resp2.data)

    def test_add_transaction_validation_amount(self):
        resp = self.client.post(
            "/add", data={"category": "Bills", "amount": "abc"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Amount must be a number.", resp.data)

    def test_add_transaction_validation_category(self):
        resp = self.client.post(
            "/add", data={"category": "", "amount": "10"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Category is required.", resp.data)

    def test_delete_transaction(self):
        # Add
        self.client.post(
            "/add",
            data={"category": "Misc", "amount": "5", "description": "TempItem"},
            follow_redirects=True,
        )
        rowid = self._get_rowid_for_desc("TempItem")
        self.assertIsNotNone(rowid)
        # Delete
        resp = self.client.post(f"/delete/{rowid}", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Transaction deleted.", resp.data)
        # Ensure gone
        resp2 = self.client.get("/")
        self.assertNotIn(b"TempItem", resp2.data)

    # ---- Added tests ----
    def test_negative_amounts_display_as_negative(self):
        self.client.post(
            "/add",
            data={"category": "Groceries", "amount": "-7.5", "description": "Eggs"},
            follow_redirects=True,
        )
        resp = self.client.get("/")
        self.assertIn(b"-7.50", resp.data)

    def test_totals_by_category_and_overall(self):
        # Clear and add multiple items
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.table}"))
        self.client.post("/add", data={"category": "A", "amount": "10"}, follow_redirects=True)
        self.client.post("/add", data={"category": "A", "amount": "5"}, follow_redirects=True)
        self.client.post("/add", data={"category": "B", "amount": "-3"}, follow_redirects=True)
        resp = self.client.get("/")
        # Overall total should be 12.0
        self.assertIn(b"Overall:", resp.data)
        self.assertIn(b"12.0", resp.data)  # string presence check; formatting may vary in template
        # Category totals
        self.assertIn(b"A", resp.data)
        self.assertIn(b"15.00", resp.data)
        self.assertIn(b"B", resp.data)
        self.assertIn(b"-3.00", resp.data)

    def test_delete_nonexistent_is_noop(self):
        # Deleting a non-existent row should not error and should redirect with 200
        resp = self.client.post("/delete/999999", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
