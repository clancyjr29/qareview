"""QA Review command-line interface.

Manage the app's SQLite database directly from the terminal - handy on
Termux where the browser is a hassle. Shares the same qareview.db as the
web app, so anything imported here shows up in the UI.

Examples:
    python cli.py init
    python cli.py users add clancy
    python cli.py import sample_data.csv --name "Batch 14" --user clancy
    python cli.py datasets list --user clancy
    python cli.py analyze <dataset_id> --user clancy
    python cli.py report <dataset_id> --file report.md --user clancy
    python cli.py export <dataset_id> --file clean.csv --user clancy
"""
import argparse
import csv
import io
import json
import os
import sys

import analyzer
import db
import engine


# ---------- helpers ----------

def load_rubric(name):
    if not name:
        name = "general-task-qa"
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "rubrics", name + ".json")
    if not os.path.isfile(path):
        avail = [f[:-5] for f in os.listdir(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "rubrics"))
            if f.endswith(".json")]
        die(f"Rubric '{name}' not found. Available: {', '.join(avail)}")
    with open(path) as f:
        return json.load(f)


def die(msg, code=1):
    print(f"Error: {msg}")
    sys.exit(code)


def get_user(conn, username):
    user = conn.execute("SELECT * FROM users WHERE username=?",
                        (username,)).fetchone()
    if not user:
        die(f"User '{username}' not found. Create it: "
            f"python cli.py users add {username}")
    return user


def review_rows(conn, dataset_id, rubric, records):
    """Run the engine over stored rows and update statuses (no AI)."""
    result = engine.evaluate_dataset(records, rubric)
    review = conn.execute(
        "SELECT id FROM reviews WHERE dataset_id=? "
        "ORDER BY created_at DESC LIMIT 1", (dataset_id,)).fetchone()
    review_id = review["id"] if review else None
    if not review_id:
        review_id = db.new_id()
        conn.execute(
            "INSERT INTO reviews (id, dataset_id, rubric_id, stats, "
            "escalations, feedback, created_at) VALUES (?,?,?,?,?,?,?)",
            (review_id, dataset_id, None, "{}", "[]", "[]", db.now()))
    conn.execute(
        "UPDATE reviews SET stats=?, escalations=?, feedback=? WHERE id=?",
        (json.dumps(result["stats"]), json.dumps(result["escalations"]),
         json.dumps(result["feedback"]), review_id))
    for rr in result["row_results"]:
        conn.execute(
            "UPDATE rows SET status=?, issues=? WHERE review_id=? "
            "AND row_num=?",
            (rr["status"],
             json.dumps([{"rule_id": i.rule_id, "severity": i.severity,
                          "message": i.message, "rationale": i.rationale,
                          "resolution": i.resolution}
                         for i in rr["issues"]]),
             review_id, rr["row_num"]))
    conn.commit()
    return result


# ---------- commands ----------

def cmd_init(args):
    db.init_db()
    print(f"Database ready at {db.DB_PATH}")


def cmd_users(args):
    conn = db.get_db()
    if args.action == "add":
        existing = conn.execute("SELECT * FROM users WHERE username=?",
                                (args.username,)).fetchone()
        if existing:
            die(f"User '{args.username}' already exists "
                f"(api key: {existing['api_key']})")
        user = {"id": db.new_id(), "username": args.username,
                "api_key": db.new_id() + db.new_id(), "created_at": db.now()}
        conn.execute("INSERT INTO users (id, username, api_key, created_at) "
                     "VALUES (:id, :username, :api_key, :created_at)", user)
        conn.commit()
        print(f"User created: {args.username}")
        print(f"API key:     {user['api_key']}")
    else:
        for u in conn.execute("SELECT username, api_key FROM users"):
            print(f"{u['username']:<20} {u['api_key']}")
    conn.close()


def cmd_import(args):
    db.init_db()
    rubric = load_rubric(args.rubric)
    problems = engine.validate_rubric(rubric)
    if problems:
        die(f"Invalid rubric: {problems}")
    with open(args.file, newline="", encoding="utf-8",
              errors="replace") as f:
        records = [dict(r) for r in csv.DictReader(f)]
    if not records:
        die("No data rows found in the CSV.")
    name = args.name or os.path.basename(args.file)

    conn = db.get_db()
    user = get_user(conn, args.user)
    ds_id = db.new_id()
    conn.execute(
        "INSERT INTO datasets (id, user_id, name, rubric_id, rubric_name, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (ds_id, user["id"], name, None, args.rubric or "general-task-qa",
         db.now()))
    review_id = db.new_id()
    conn.execute(
        "INSERT INTO reviews (id, dataset_id, rubric_id, stats, escalations, "
        "feedback, created_at) VALUES (?,?,?,?,?,?,?)",
        (review_id, ds_id, None, "{}", "[]", "[]", db.now()))
    conn.executemany(
        "INSERT INTO rows (id, review_id, row_num, status, data, issues) "
        "VALUES (?,?,?,?,?,?)",
        [(db.new_id(), review_id, i, "pending", json.dumps(r), "[]")
         for i, r in enumerate(records, start=1)])
    conn.commit()

    result = review_rows(conn, ds_id, rubric, records)
    s = result["stats"]
    conn.close()
    print(f"Imported '{name}': {s['total_rows']} rows, "
          f"{s['flagged_rows']} flagged, pass rate {s['pass_rate']}%")
    print(f"Dataset id: {ds_id}")
    for e in result["escalations"]:
        print(f"  ESCALATION: {e['rule_id']} - {e['occurrences']} "
              f"occurrences ({e['pct_of_dataset']}%)")


def cmd_datasets(args):
    conn = db.get_db()
    user = get_user(conn, args.user)
    rows = conn.execute(
        "SELECT d.id, d.name, d.created_at, "
        "(SELECT COUNT(*) FROM rows r JOIN reviews v ON r.review_id=v.id "
        "WHERE v.dataset_id=d.id) AS total, "
        "(SELECT COUNT(*) FROM rows r JOIN reviews v ON r.review_id=v.id "
        "WHERE v.dataset_id=d.id AND r.status='flagged') AS flagged "
        "FROM datasets d WHERE d.user_id=? ORDER BY d.created_at DESC",
        (user["id"],)).fetchall()
    conn.close()
    if not rows:
        print("No datasets yet. Import one: python cli.py import <file.csv>")
        return
    for r in rows:
        print(f"{r['id']:<14} {r['name']:<30} {r['total']:>5} rows  "
              f"{r['flagged']:>3} flagged")


def _load_dataset(user, dataset_id):
    conn = db.get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id=? AND user_id=?",
                      (dataset_id, user["id"])).fetchone()
    if not ds:
        conn.close()
        die(f"Dataset '{dataset_id}' not found for user '{user['username']}'")
    review = conn.execute(
        "SELECT * FROM reviews WHERE dataset_id=? ORDER BY created_at DESC "
        "LIMIT 1", (dataset_id,)).fetchone()
    rows = conn.execute(
        "SELECT row_num, data, issues FROM rows WHERE review_id=? "
        "ORDER BY row_num", (review["id"],)).fetchall()
    conn.close()
    return ds, review, rows


def cmd_analyze(args):
    conn = db.get_db()
    user = get_user(conn, args.user)
    conn.close()
    ds, review, rows = _load_dataset(user, args.dataset_id)
    records = [json.loads(r["data"]) for r in rows]
    issues_by_row = {r["row_num"]: json.loads(r["issues"])
                     for r in rows if json.loads(r["issues"])}
    stats = json.loads(review["stats"])
    a = analyzer.analyze(records, stats, issues_by_row)
    print(f"Dataset:  {ds['name']}")
    print(f"Shape:    {a['data_shape']['rows']} rows x "
          f"{a['data_shape']['columns']} columns")
    print(f"Quality:  {a['quality_score']}/100 "
          f"({stats.get('pass_rate', 0)}% rows pass)")
    print("Insights:")
    for i in a["insights"]:
        print(f"  - {i}")
    if args.json:
        print(json.dumps(a, indent=2, default=str))


def cmd_report(args):
    conn = db.get_db()
    user = get_user(conn, args.user)
    conn.close()
    ds, review, rows = _load_dataset(user, args.dataset_id)
    records = [json.loads(r["data"]) for r in rows]
    issues_by_row = {r["row_num"]: json.loads(r["issues"])
                     for r in rows if json.loads(r["issues"])}
    stats = json.loads(review["stats"])
    a = analyzer.analyze(records, stats, issues_by_row)
    md = analyzer.build_markdown_report(
        ds["name"], ds["rubric_name"] or "default", a, stats,
        json.loads(review["escalations"]), json.loads(review["feedback"]))
    if args.file:
        with open(args.file, "w") as f:
            f.write(md)
        print(f"Report written to {args.file}")
    else:
        print(md)


def cmd_export(args):
    conn = db.get_db()
    user = get_user(conn, args.user)
    conn.close()
    ds, review, rows = _load_dataset(user, args.dataset_id)
    data = [json.loads(r["data"]) for r in rows]
    fieldnames = []
    for d in data:
        for k in d:
            if k not in fieldnames:
                fieldnames.append(k)
    if args.file:
        with open(args.file, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, restval="")
            w.writeheader()
            for d in data:
                w.writerow(d)
        print(f"Exported {len(data)} rows to {args.file}")
    else:
        si = io.StringIO()
        w = csv.DictWriter(si, fieldnames=fieldnames, restval="")
        w.writeheader()
        for d in data:
            w.writerow(d)
        print(si.getvalue(), end="")


# ---------- wiring ----------

def main():
    p = argparse.ArgumentParser(
        prog="cli.py", description="QA Review database manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database/tables")

    u = sub.add_parser("users", help="add or list users")
    u.add_argument("action", choices=["add", "list"])
    u.add_argument("username", nargs="?")

    i = sub.add_parser("import", help="import a CSV and review it")
    i.add_argument("file")
    i.add_argument("--name", help="dataset name (default: filename)")
    i.add_argument("--rubric", help="rubric name (default: general-task-qa)")
    i.add_argument("--user", required=True)

    d = sub.add_parser("datasets", help="list datasets")
    d.add_argument("--user", required=True)

    a = sub.add_parser("analyze", help="analysis for a dataset")
    a.add_argument("dataset_id")
    a.add_argument("--user", required=True)
    a.add_argument("--json", action="store_true")

    r = sub.add_parser("report", help="Markdown report for a dataset")
    r.add_argument("dataset_id")
    r.add_argument("--user", required=True)
    r.add_argument("--file", help="write to file instead of stdout")

    e = sub.add_parser("export", help="export a dataset's data as CSV")
    e.add_argument("dataset_id")
    e.add_argument("--user", required=True)
    e.add_argument("--file", help="write to file instead of stdout")

    args = p.parse_args()
    {"init": cmd_init, "users": cmd_users, "import": cmd_import,
     "datasets": cmd_datasets, "analyze": cmd_analyze, "report": cmd_report,
     "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    main()
