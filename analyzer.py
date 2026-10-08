"""Dataset analysis layer: turns raw rows + review results into usable
insights without any heavy dependencies (pure stdlib).

Produces:
- a 0-100 quality score with a transparent formula
- per-column profiles (type inference, missingness, distributions)
- cross-tab breakdowns (e.g. flag rate by status or worker)
- plain-language insights ("rows with status=fail get flagged 4x more")
"""
import math
import statistics
from collections import Counter, defaultdict


def _num(v):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _is_missing(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def profile_columns(rows):
    """One profile dict per column in first-seen order."""
    if not rows:
        return []
    columns = []
    names = []
    for r in rows:
        for k in r:
            if k not in names:
                names.append(k)

    n = len(rows)
    for name in names:
        values = [r.get(name) for r in rows]
        present = [v for v in values if not _is_missing(v)]
        missing = n - len(present)
        distinct = len({str(v).strip() for v in present})

        nums = [x for x in (_num(v) for v in present) if x is not None]
        numeric_pct = (len(nums) / len(present)) if present else 0

        col = {"name": name, "count": n, "missing": missing,
               "missing_pct": round(100 * missing / n, 1) if n else 0,
               "distinct": distinct}

        if numeric_pct >= 0.8 and nums:
            col["type"] = "numeric"
            col.update({
                "min": min(nums), "max": max(nums),
                "mean": round(statistics.fmean(nums), 2),
                "median": statistics.median(nums),
                "std": round(statistics.pstdev(nums), 2) if len(nums) > 1 else 0,
            })
            # outliers: beyond 3 standard deviations
            if col["std"] > 0:
                mu, sd = col["mean"], col["std"]
                col["outliers"] = [v for v in nums
                                   if abs(v - mu) > 3 * sd]
            else:
                col["outliers"] = []
        else:
            avg_len = (statistics.fmean(len(str(v)) for v in present)
                       if present else 0)
            mostly_unique = distinct >= 0.8 * max(len(present), 1)
            col["type"] = "categorical" if (
                (distinct <= 20 or distinct / max(n, 1) < 0.5)
                and not (mostly_unique and avg_len > 20)
            ) else "text"
            top = Counter(str(v).strip() for v in present).most_common(5)
            col["top_values"] = [{"value": v, "count": c,
                                  "pct": round(100 * c / max(n, 1), 1)}
                                 for v, c in top]
            if present:
                lens = [len(str(v)) for v in present]
                col["avg_length"] = round(statistics.fmean(lens), 1)
        columns.append(col)
    return columns


def quality_score(stats):
    """Transparent 0-100 score: pass rate minus severity penalties."""
    total = stats.get("total_rows") or 0
    flagged = stats.get("flagged_rows") or 0
    sev = stats.get("issues_by_severity", {})
    if total == 0:
        return 100
    base = 100 * (total - flagged) / total
    penalty = min(40, sev.get("critical", 0) * 10
                  + sev.get("high", 0) * 3
                  + sev.get("medium", 0) * 1
                  + sev.get("low", 0) * 0.5)
    return round(max(0, min(100, base - penalty)), 1)


def _flag_rate_by_group(rows, flagged_rows, col_name, cap=12):
    """Flag rate per value of a categorical column."""
    groups = defaultdict(lambda: {"total": 0, "flagged": 0})
    for i, r in enumerate(rows, start=1):
        key = str(r.get(col_name, "")).strip() or "(blank)"
        g = groups[key]
        g["total"] += 1
        if i in flagged_rows:
            g["flagged"] += 1
    out = []
    for value, g in groups.items():
        if g["total"] < 2:
            continue
        out.append({"group_by": col_name, "value": value,
                    "total": g["total"], "flagged": g["flagged"],
                    "flag_pct": round(100 * g["flagged"] / g["total"], 1)})
    out.sort(key=lambda d: -d["flag_pct"])
    return out[:cap]


def _group_means(rows, cat_col, num_col, cap=8):
    """Mean of a numeric column per value of a categorical column."""
    groups = defaultdict(list)
    for r in rows:
        key = str(r.get(cat_col, "")).strip() or "(blank)"
        num = _num(r.get(num_col))
        if num is not None:
            groups[key].append(num)
    out = [{"group_by": cat_col, "value": v, "mean_of": num_col,
            "mean": round(statistics.fmean(vals), 2), "count": len(vals)}
           for v, vals in groups.items() if len(vals) >= 2]
    out.sort(key=lambda d: -d["mean"])
    return out[:cap]


def breakdowns(rows, flagged_rows, columns):
    """Cross-tabs worth looking at. Bounded to keep pages readable."""
    out = []
    cats = [c["name"] for c in columns if c["type"] == "categorical"]
    nums = [c["name"] for c in columns if c["type"] == "numeric"]
    for cat in cats[:4]:
        rates = _flag_rate_by_group(rows, flagged_rows, cat)
        if rates and len(rates) >= 2:
            out.append({"kind": "flag_rate", "title":
                        f"Flag rate by {cat}", "rows": rates,
                        "cols": ["value", "total", "flagged", "flag_pct"]})
    for cat in cats[:3]:
        for num in nums[:3]:
            means = _group_means(rows, cat, num)
            if means and len(means) >= 2:
                spread = means[0]["mean"] - means[-1]["mean"]
                if abs(spread) > 0.01:
                    out.append({"kind": "group_mean", "title":
                                f"Average {num} by {cat}", "rows": means,
                                "cols": ["value", "mean", "count"]})
    return out


def insights(stats, columns, tabs, flagged_rows):
    """Plain-language observations, most important first."""
    out = []
    total = stats.get("total_rows") or 0
    flagged = stats.get("flagged_rows") or 0

    # 1. error concentration from flag-rate tabs
    overall = 100 * flagged / total if total else 0
    for t in tabs:
        if t["kind"] != "flag_rate":
            continue
        worst = t["rows"][0]
        if worst["flag_pct"] >= max(1.8 * overall, overall + 15):
            out.append(
                f"'{worst['group_by']}' = '{worst['value']}' concentrates "
                f"errors: {worst['flag_pct']}% of those rows are flagged vs "
                f"{overall:.0f}% overall "
                f"({worst['flagged']} of {worst['total']} rows).")

    # 2. column health
    for c in columns:
        if c["missing_pct"] >= 30:
            out.append(f"Column '{c['name']}' is missing in "
                       f"{c['missing_pct']}% of rows.")
        if c["distinct"] == 1 and c["count"] > 3:
            out.append(f"Column '{c['name']}' has a single constant value "
                       "and carries no information.")
        if c["type"] == "numeric" and c.get("outliers"):
            out.append(f"Column '{c['name']}' has {len(c['outliers'])} "
                       f"statistical outlier(s), e.g. {c['outliers'][:3]}.")

    # 3. spread observations from group means
    for t in tabs:
        if t["kind"] == "group_mean" and len(t["rows"]) >= 2:
            top, bot = t["rows"][0], t["rows"][-1]
            out.append(f"{top['group_by']}='{top['value']}' averages "
                       f"{top['mean']} on {top['mean_of']} vs "
                       f"{bot['mean']} for '{bot['value']}'.")

    if not out:
        out.append("No strong patterns detected; the data looks uniform.")
    return out[:10]


def analyze(rows, stats, issues_by_row=None):
    """Full analysis of one dataset's rows + its latest review stats."""
    issues_by_row = issues_by_row or {}
    flagged_rows = {num for num, issues in issues_by_row.items() if issues}
    cols = profile_columns(rows)
    tabs = breakdowns(rows, flagged_rows, cols)
    return {
        "data_shape": {"rows": len(rows),
                       "columns": len(cols)},
        "quality_score": quality_score(stats),
        "columns": cols,
        "breakdowns": tabs,
        "insights": insights(stats, cols, tabs, flagged_rows),
    }


def build_markdown_report(ds_name, rubric_name, analysis, stats,
                          escalations, feedback, narrative=None):
    """A self-contained, shareable Markdown report."""
    a = analysis
    lines = [f"# QA Analysis Report: {ds_name}",
             f"Rubric: {rubric_name}", "",
             "## Quality score",
             f"**{a['quality_score']}/100** "
             f"({stats.get('pass_rate', 0)}% of rows pass, "
             f"{stats.get('flagged_rows', 0)} of "
             f"{stats.get('total_rows', 0)} flagged)", ""]

    if narrative:
        lines += ["## Executive summary", narrative, ""]

    lines += ["## Insights"]
    lines += [f"- {i}" for i in a["insights"]]
    lines += ["", "## Column profiles"]
    lines.append("| Column | Type | Missing | Distinct | Notes |")
    lines.append("|---|---|---|---|---|")
    for c in a["columns"]:
        if c["type"] == "numeric":
            notes = (f"min {c['min']}, max {c['max']}, "
                     f"mean {c['mean']}, median {c['median']}")
        else:
            top = c.get("top_values", [])
            notes = ("top: " + ", ".join(
                f"{t['value']} ({t['pct']}%)" for t in top[:3])
                if top else "-")
        lines.append(f"| {c['name']} | {c['type']} | {c['missing_pct']}% | "
                     f"{c['distinct']} | {notes} |")

    for t in a["breakdowns"]:
        lines += ["", f"## {t['title']}",
                  "| " + " | ".join(t["cols"]) + " |",
                  "| " + " | ".join("---" for _ in t["cols"]) + " |"]
        for r in t["rows"]:
            lines.append("| " + " | ".join(
                str(r.get(c, "")) for c in t["cols"]) + " |")

    if escalations:
        lines += ["", "## Escalations"]
        for e in escalations:
            lines.append(f"- **{e['rule_id']}**: {e['occurrences']} "
                         f"occurrences ({e['pct_of_dataset']}%) - "
                         f"{e['recommendation']}")

    if feedback:
        lines += ["", "## Feedback for workers"]
        lines += [f"- {f}" for f in feedback[:20]]

    lines += ["", "---", "Pass rate reflects the latest automated review "
             "against the rubric; the quality score also weighs issue "
             "severity (critical > high > medium > low)."]
    return "\n".join(lines)
