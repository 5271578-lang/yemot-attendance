"""Phone attendance line (NBS / Yemot API extension) - entry point.

Run locally:   python app.py            (http://localhost:5000/admin)
Production:    gunicorn -w 2 -b 0.0.0.0:$PORT app:app
"""
import os

# Load settings from a .env file next to this script, if present.
_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):
    for _line in open(_env, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from flask import Flask, redirect  # noqa: E402

import db  # noqa: E402
from admin import bp as admin_bp  # noqa: E402
from ivr import bp as ivr_bp  # noqa: E402
import report  # noqa: E402

db.init_db()
if os.environ.get("DISABLE_REPORT_THREAD") != "1":
    report.start_background()

app = Flask(__name__)
app.register_blueprint(ivr_bp)
app.register_blueprint(admin_bp)


@app.route("/health")
def health():
    return "ok"


@app.route("/")
def home():
    return redirect("/admin/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
