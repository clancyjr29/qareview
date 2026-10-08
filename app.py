"""QA Review - rubric-based data review SaaS, sized for Termux."""
import csv
import io
import json
import os

from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

import db
import engine
import fixer
import llm

app = Flask(__name__)
app.secret_key = os.environ.get("QAREVIEW_SECRET", "change-me-in-production")

RUBRIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "rubrics")


# ---------- auth ----------

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return user


def api_user():
    key = request.headers.get("X-API-Key")
    if not key:
        return None
    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE api_key=?",
                        (key,)).fetchone()
    conn.close()
    return user


@app.before_request
def require_login():
    allowed = {"login", "static"}
    if request.endpoint in allowed:
        return None
    if current_user() or api_user():
        return None
    return redirect(url_for("login"))


def user_or_none():
    return api_user() or current_user()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if not username:
            flash("Pick a username.")
            return render_template("login.html")
        conn = db.get_db()
        user = conn.execute("SELECT * FROM users WHERE username=?",
                            (username,)).fetchone()
        if not user:
            user = {
                "id": db.new_id(),
                "username": username,
                "api_key": db.new_id() + db.new_id(),
                "created_at": db.now(),
            }
            conn.execute(
                "INSERT INTO users (id, username, api_key, created_at) "
                "VALUES (:id, :username, :api_key, :created_at)", user)
            conn.commit()
        conn.close()
        session["user_id"] = user["id"]
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- helpers ----------

def load_default_rubrics():
    rubrics = {}
    if os.path.isdir(RUBRIC_DIR):
        for fn in os.listdir(RUBRIC_DIR):
            if fn.endswith(".json"):
                with open(os.path.join(RUBRIC_DIR, fn)) as f:
                    rubrics[fn[:-5]] = json.load(f)
    return rubrics


def parse_csv(text):
    reader = csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader]


def run_review(dataset_id, rubric):
    conn = db.get_db()
    rows = conn.execute(
        "SELECT row_num, data FROM rows WHERE review_id IN "
        "(SELECT id FROM reviews WHERE dataset_id=?) "
        "ORDER BY row_num", (dataset_id,)).fetchall()
    records = []
    for r in rows:
        d = json.loads(r["data"])
        # strip engine keys stored during ingest
        d.pop("__row_num", None)
        records.append(d)
    adjudicator = llm.adjudicate if llm.available() else None
    result = engine.evaluate_dataset(records, rubric,
                                     ai_adjudicator=adjudicator)

    review = conn.execute(
        "SELECT id FROM reviews WHERE dataset_id=? "
        "ORDER BY created_at DESC LIMIT 1", (dataset_id,)).fetchone()
    if review:
        review_id = review["id"]
        conn.execute(
            "UPDATE reviews SET stats=?, escalations=?, feedback=?, "
            "created_at=? WHERE id=?",
            (json.dumps(result["stats"]), json.dumps(result["escalations"]),
             json.dumps(result["feedback"]), db.now(), review_id))
    else:
        review_id = db.new_id()
        conn.execute(
            "INSERT INTO reviews (id, dataset_id, rubric_id, stats, "
            "escalations, feedback, created_at) VALUES (?,?,?,?,?,?,?)",
            (review_id, dataset_id, None, json.dumps(result["stats"]),
             json.dumps(result["escalations"]),
             json.dumps(result["feedback"]), db.now()))

    # Update row statuses + issues + AI judgment
    for rr in result["row_results"]:
        conn.execute(
            "UPDATE rows SET status=?, issues=?, ai=? WHERE review_id IN "
            "(SELECT id FROM reviews WHERE dataset_id=?) AND row_num=?",
            (rr["status"],
             json.dumps([{"rule_id": i.rule_id, "severity": i.severity,
                          "message": i.message, "rationale": i.rationale,
                          "resolution": i.resolution}
                         for i in rr["issues"]]),
             json.dumps({"verdict": rr["ai_verdict"],
                         "rationale": rr["ai_rationale"]}),
             dataset_id, rr["row_num"]))
    conn.commit()
    conn.close()
    return review_id, result


def dataset_rows_and_issues(dataset_id):
    """Latest review's rows: [(row_num, data, issues), ...] in order."""
    conn = db.get_db()
    review = conn.execute(
        "SELECT id FROM reviews WHERE dataset_id=? "
        "ORDER BY created_at DESC LIMIT 1", (dataset_id,)).fetchone()
    if not review:
        conn.close()
        return None
    rows = conn.execute(
        "SELECT row_num, data, issues FROM rows WHERE review_id=? "
        "ORDER BY row_num", (review["id"],)).fetchall()
    conn.close()
    return [(r["row_num"], json.loads(r["data"]),
             json.loads(r["issues"])) for r in rows]


def dataset_rubric(ds):
    """Resolve the rubric used by a dataset (by stored name, else first)."""
    rubrics = load_default_rubrics()
    keys = ds.keys()
    name = ds["rubric_name"] if "rubric_name" in keys else None
    if name and name in rubrics:
        return rubrics[name]
    return rubrics[sorted(rubrics)[0]] if rubrics else None


def apply_fixes(dataset_id, use_ai=False, options=None):
    """Fix all flagged issues in a dataset, creating a corrected copy.

    Returns (new_dataset_id, fix_result).
    """
    user = user_or_none()
    conn = db.get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id=? AND user_id=?",
                      (dataset_id, user["id"])).fetchone()
    conn.close()
    if not ds:
        raise ValueError("dataset not found")
    rubric = dataset_rubric(ds)
    if not rubric:
        raise ValueError("no rubric available")

    triplets = dataset_rows_and_issues(dataset_id)
    if not triplets:
        raise ValueError("no reviewed rows")
    rows = [t[1] for t in triplets]
    issues_by_row = {t[0]: t[2] for t in triplets if t[2]}

    opts = {"trim": True, "clamp": True, "drop_duplicates": True}
    if options:
        opts.update(options)
    ai_fixer = llm.suggest_fixes if (use_ai and llm.available()) else None
    result = fixer.fix_dataset(rows, issues_by_row, rubric, options=opts,
                               ai_fixer=ai_fixer, engine_module=engine)

    # store corrected copy as a new dataset
    conn = db.get_db()
    new_id = db.new_id()
    conn.execute(
        "INSERT INTO datasets (id, user_id, name, rubric_id, rubric_name, "
        "source_id, created_at) VALUES (?,?,?,?,?,?,?)",
        (new_id, user["id"], ds["name"] + " (fixed)", None,
         ds["rubric_name"], ds["id"], db.now()))
    review_id = db.new_id()
    conn.execute(
        "INSERT INTO reviews (id, dataset_id, rubric_id, stats, "
        "escalations, feedback, created_at) VALUES (?,?,?,?,?,?,?)",
        (review_id, new_id, None, json.dumps({}), json.dumps([]),
         json.dumps([]), db.now()))
    conn.executemany(
        "INSERT INTO rows (id, review_id, row_num, status, data, issues) "
        "VALUES (?,?,?,?,?,?)",
        [(db.new_id(), review_id, i, "pending", json.dumps(r), "[]")
         for i, r in enumerate(result["rows"], start=1)])
    conn.executemany(
        "INSERT INTO fixes (id, dataset_id, row_num, field, old_value, "
        "new_value, method, rule_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(db.new_id(), new_id, e["row_num"], e["field"], e["old"],
          e["new"], e["method"], e["rule_id"], db.now())
         for e in result["fix_log"]])
    conn.commit()
    conn.close()

    run_review(new_id, rubric)
    return new_id, result


def get_latest_review(dataset_id):
    conn = db.get_db()
    review = conn.execute(
        "SELECT * FROM reviews WHERE dataset_id=? "
        "ORDER BY created_at DESC LIMIT 1", (dataset_id,)).fetchone()
    conn.close()
    return review


# ---------- web UI ----------

@app.route("/")
def dashboard():
    user = user_or_none()
    conn = db.get_db()
    datasets = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM rows r JOIN reviews v "
        "ON r.review_id=v.id WHERE v.dataset_id=d.id AND r.status='flagged') "
        "AS flagged, (SELECT COUNT(*) FROM rows r JOIN reviews v "
        "ON r.review_id=v.id WHERE v.dataset_id=d.id) AS total "
        "FROM datasets d WHERE d.user_id=? ORDER BY d.created_at DESC",
        (user["id"],)).fetchall()
    escalations = conn.execute(
        "SELECT d.name, v.escalations, v.created_at FROM reviews v "
        "JOIN datasets d ON v.dataset_id=d.id WHERE d.user_id=? "
        "ORDER BY v.created_at DESC LIMIT 20", (user["id"],)).fetchall()
    conn.close()
    esc_list = []
    for e in escalations:
        for item in json.loads(e["escalations"]):
            esc_list.append({"dataset": e["name"], **item})
    return render_template("dashboard.html", datasets=datasets,
                           escalations=esc_list[:10], user=user)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        name = request.form.get("name", "").strip() or "Untitled dataset"
        rubric_name = request.form.get("rubric")
        text = request.form.get("csv_text", "")
        f = request.files.get("csv_file")
        if f and f.filename:
            text = f.read().decode("utf-8", errors="replace")
        if not text.strip():
            flash("Upload a CSV file or paste CSV text.")
            return render_template("upload.html", rubrics=rubric_list())
        rows = parse_csv(text)
        if not rows:
            flash("No data rows found in the CSV.")
            return render_template("upload.html", rubrics=rubric_list())
        rubric = load_default_rubrics().get(rubric_name)
        if not rubric:
            flash("Unknown rubric.")
            return render_template("upload.html", rubrics=rubric_list())
        if request.form.get("use_ai"):
            if not llm.available():
                flash("AI judgment requested but no API key found. "
                      "Set QA_LLM_API_KEY (and optionally QA_LLM_BASE_URL / "
                      "QA_LLM_MODEL) and restart. Reviewed without AI.")
            else:
                rubric = dict(rubric)
                rubric["ai"] = dict(rubric.get("ai") or {})
                rubric["ai"]["review_flagged"] = True

        user = user_or_none()
        conn = db.get_db()
        ds_id = db.new_id()
        conn.execute(
            "INSERT INTO datasets (id, user_id, name, rubric_id, rubric_name,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (ds_id, user["id"], name, None, rubric_name, db.now()))
        review_id = db.new_id()
        conn.execute(
            "INSERT INTO reviews (id, dataset_id, rubric_id, stats, "
            "escalations, feedback, created_at) VALUES (?,?,?,?,?,?,?)",
            (review_id, ds_id, None, json.dumps({}), json.dumps([]),
             json.dumps([]), db.now()))
        conn.executemany(
            "INSERT INTO rows (id, review_id, row_num, status, data, issues) "
            "VALUES (?,?,?,?,?,?)",
            [(db.new_id(), review_id, i, "pending", json.dumps(r), "[]")
             for i, r in enumerate(rows, start=1)])
        conn.commit()
        conn.close()
        run_review(ds_id, rubric)
        return redirect(url_for("dataset", dataset_id=ds_id))
    return render_template("upload.html", rubrics=rubric_list())


def rubric_list():
    return sorted(load_default_rubrics().keys())


@app.route("/datasets/<dataset_id>")
def dataset(dataset_id):
    user = user_or_none()
    conn = db.get_db()
    ds = conn.execute(
        "SELECT * FROM datasets WHERE id=? AND user_id=?",
        (dataset_id, user["id"])).fetchone()
    if not ds:
        conn.close()
        flash("Dataset not found.")
        return redirect(url_for("dashboard"))
    review = get_latest_review(dataset_id)
    stats = json.loads(review["stats"]) if review else {}
    rows = conn.execute(
        "SELECT * FROM rows WHERE review_id=? ORDER BY row_num",
        (review["id"],)).fetchall() if review else []
    conn.close()

    f_rule = request.args.get("rule")
    f_sev = request.args.get("severity")
    f_status = request.args.get("status")

    conn = db.get_db()
    fixes = conn.execute(
        "SELECT * FROM fixes WHERE dataset_id=? ORDER BY row_num, field",
        (dataset_id,)).fetchall() if ds else []
    source = conn.execute(
        "SELECT name FROM datasets WHERE id=?",
        (ds["source_id"],)).fetchone() if "source_id" in ds.keys() \
        and ds["source_id"] else None
    conn.close()
    fix_rows = [{"row_num": f["row_num"], "field": f["field"],
                 "old": f["old_value"], "new": f["new_value"],
                 "method": f["method"], "rule_id": f["rule_id"]}
                for f in fixes]

    out = []
    rules_seen = set()
    for r in rows:
        issues = json.loads(r["issues"])
        for i in issues:
            rules_seen.add(i["rule_id"])
        if f_rule and not any(i["rule_id"] == f_rule for i in issues):
            continue
        if f_sev and not any(i["severity"] == f_sev for i in issues):
            continue
        if f_status == "flagged" and r["status"] != "flagged":
            continue
        if f_status == "pass" and r["status"] != "pass":
            continue
        ai = json.loads(r["ai"]) if r["ai"] else {}
        out.append({"row_num": r["row_num"], "status": r["status"],
                    "data": json.loads(r["data"]), "issues": issues,
                    "ai_verdict": ai.get("verdict"),
                    "ai_rationale": ai.get("rationale")})
    return render_template("dataset.html", ds=ds, stats=stats, rows=out,
                           rules_seen=sorted(rules_seen),
                           feedback=json.loads(review["feedback"])
                           if review else [],
                           escalations=json.loads(review["escalations"])
                           if review else [],
                           fix_rows=fix_rows,
                           source_name=source["name"] if source else None,
                           ai_available=llm.available())


@app.route("/datasets/<dataset_id>/feedback")
def dataset_feedback(dataset_id):
    user = user_or_none()
    conn = db.get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id=? AND user_id=?",
                      (dataset_id, user["id"])).fetchone()
    review = get_latest_review(dataset_id) if ds else None
    conn.close()
    if not review:
        return jsonify({"error": "not found"}), 404
    return render_template("feedback.html", ds=ds,
                           feedback=json.loads(review["feedback"]))


@app.route("/datasets/<dataset_id>/export")
def export(dataset_id):
    user = user_or_none()
    conn = db.get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id=? AND user_id=?",
                      (dataset_id, user["id"])).fetchone()
    review = get_latest_review(dataset_id) if ds else None
    rows = conn.execute(
        "SELECT * FROM rows WHERE review_id=? ORDER BY row_num",
        (review["id"],)).fetchall() if review else []
    conn.close()
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(["row_num", "status", "issues", "data"])
    for r in rows:
        issues = "; ".join(
            f"[{i['severity']}] {i['rule_id']}: {i['message']}"
            for i in json.loads(r["issues"]))
        writer.writerow([r["row_num"], r["status"], issues,
                         json.dumps(json.loads(r["data"]))])
    return (si.getvalue(), 200,
            {"Content-Type": "text/csv",
             "Content-Disposition":
             f"attachment; filename={ds['name']}-review.csv"})


@app.route("/datasets/<dataset_id>/fix", methods=["POST"])
def fix_route(dataset_id):
    use_ai = bool(request.form.get("use_ai"))
    # checkboxes: present = on, absent = off (rendered checked by default)
    options = {"trim": bool(request.form.get("trim")),
               "clamp": bool(request.form.get("clamp")),
               "drop_duplicates": bool(request.form.get("drop_duplicates"))}
    try:
        new_id, result = apply_fixes(dataset_id, use_ai=use_ai,
                                    options=options)
    except ValueError as e:
        flash(f"Cannot fix this dataset: {e}")
        return redirect(url_for("dataset", dataset_id=dataset_id))
    s = result["summary"]
    flash(f"Fixed {s['changes_made']} issue(s) across {s['rows_changed']} "
          f"row(s); {s['rows_dropped']} duplicate row(s) dropped; "
          f"{s['still_flagged']} row(s) still flagged after fixes.")
    return redirect(url_for("dataset", dataset_id=new_id))


@app.route("/datasets/<dataset_id>/export-data")
def export_data(dataset_id):
    """Raw CSV of the dataset's (corrected) data."""
    user = user_or_none()
    conn = db.get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id=? AND user_id=?",
                      (dataset_id, user["id"])).fetchone()
    review = get_latest_review(dataset_id) if ds else None
    rows = conn.execute(
        "SELECT row_num, data FROM rows WHERE review_id=? ORDER BY row_num",
        (review["id"],)).fetchall() if review else []
    conn.close()
    if not rows:
        flash("Nothing to export.")
        return redirect(url_for("dataset", dataset_id=dataset_id))
    data = [json.loads(r["data"]) for r in rows]
    fieldnames = []
    for d in data:
        for k in d:
            if k not in fieldnames:
                fieldnames.append(k)
    si = io.StringIO()
    writer = csv.DictWriter(si, fieldnames=fieldnames, restval="")
    writer.writeheader()
    for d in data:
        writer.writerow(d)
    return (si.getvalue(), 200,
            {"Content-Type": "text/csv",
             "Content-Disposition":
             f"attachment; filename={ds['name']}-data.csv"})


# ---------- JSON API (for programmatic use) ----------

@app.route("/api/datasets/<dataset_id>/issues")
def api_issues(dataset_id):
    user = user_or_none()
    conn = db.get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id=? AND user_id=?",
                      (dataset_id, user["id"])).fetchone()
    review = get_latest_review(dataset_id) if ds else None
    if not review:
        conn.close()
        return jsonify({"error": "not found"}), 404
    rows = conn.execute(
        "SELECT row_num, status, issues FROM rows WHERE review_id=? "
        "ORDER BY row_num", (review["id"],)).fetchall()
    conn.close()
    return jsonify({
        "stats": json.loads(review["stats"]),
        "escalations": json.loads(review["escalations"]),
        "rows": [{"row_num": r["row_num"], "status": r["status"],
                  "issues": json.loads(r["issues"])} for r in rows
                 if r["status"] == "flagged"],
    })


@app.route("/api/datasets/<dataset_id>/fix", methods=["POST"])
def api_fix(dataset_id):
    """Fix all flagged issues; creates a corrected copy dataset."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        new_id, result = apply_fixes(
            dataset_id, use_ai=bool(body.get("use_ai")),
            options=body.get("options") or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"fixed_dataset_id": new_id,
                    "summary": result["summary"],
                    "fix_log": result["fix_log"],
                    "ai_available": llm.available()})


@app.route("/api/review", methods=["POST"])
def api_review():
    """Stateless endpoint: POST {rubric, rows} -> full review result."""
    body = request.get_json(force=True, silent=True) or {}
    rubric = body.get("rubric") or {}
    rows = body.get("rows") or []
    problems = engine.validate_rubric(rubric)
    if problems:
        return jsonify({"error": "invalid rubric", "details": problems}), 400
    if not rows:
        return jsonify({"error": "no rows provided"}), 400
    use_ai = bool(body.get("use_ai")) and llm.available()
    adjudicator = llm.adjudicate if use_ai else None
    result = engine.evaluate_dataset(rows, rubric,
                                     ai_adjudicator=adjudicator)
    result.pop("row_results")
    result["ai_available"] = llm.available()
    if body.get("use_ai") and not use_ai:
        result["ai_note"] = ("use_ai requested but no LLM configured; "
                             "set QA_LLM_API_KEY / QA_LLM_BASE_URL.")
    return jsonify(result)


# ---------- start ----------

db.init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  QA Review running on http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
