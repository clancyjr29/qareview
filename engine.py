"""Rubric-based data review engine.

Evaluates datasets against structured rubrics, flags issues with severity and
rationale, detects recurring patterns for escalation, and generates
actionable written feedback.
"""
import re
from collections import defaultdict

PASS = "pass"
FLAG = "flagged"

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def validate_rubric(rubric):
    """Return a list of problems with a rubric definition (empty = valid)."""
    problems = []
    if not isinstance(rubric, dict):
        return ["Rubric must be a JSON object."]
    if not isinstance(rubric.get("rules"), list) or not rubric["rules"]:
        problems.append("Rubric needs a non-empty 'rules' list.")
        return problems
    for i, rule in enumerate(rubric["rules"]):
        if not isinstance(rule, dict):
            problems.append(f"Rule {i} is not an object.")
            continue
        rtype = rule.get("type")
        valid_types = {"required_fields", "regex", "allowed_values",
                       "numeric_range", "unique", "cross_field", "non_empty"}
        if rtype not in valid_types:
            problems.append(f"Rule {i}: unknown type '{rtype}'.")
        if rtype == "regex":
            try:
                re.compile(rule.get("pattern", ""))
            except re.error as e:
                problems.append(f"Rule {i}: bad regex ({e}).")
        if rtype == "numeric_range":
            for key in ("min", "max"):
                if key in rule and not isinstance(rule[key], (int, float)):
                    problems.append(f"Rule {i}: '{key}' must be a number.")
    return problems


def _to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_missing(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


class RuleResult:
    __slots__ = ("rule_id", "severity", "message", "rationale", "resolution")

    def __init__(self, rule_id, severity, message, rationale, resolution):
        self.rule_id = rule_id
        self.severity = severity
        self.message = message
        self.rationale = rationale
        self.resolution = resolution


def evaluate_row(row, row_num, rubric, seen_values):
    """Evaluate one row against every rule. Returns list of RuleResult."""
    issues = []
    for rule in rubric.get("rules", []):
        rid = rule.get("id", rule.get("type", "unnamed"))
        sev = rule.get("severity", "medium")
        rtype = rule.get("type")

        if rtype == "required_fields":
            missing = [f for f in rule["fields"] if _is_missing(row.get(f))]
            if missing:
                issues.append(RuleResult(
                    rid, sev,
                    f"Missing required field(s): {', '.join(missing)}.",
                    "Required fields guarantee record completeness; a record "
                    "without them cannot be verified downstream.",
                    f"Populate {', '.join(missing)} or confirm the source "
                    "system exports them."))

        elif rtype == "non_empty":
            f = rule["field"]
            if _is_missing(row.get(f)):
                issues.append(RuleResult(
                    rid, sev, f"Field '{f}' is empty.",
                    "Empty values in this field break the review contract.",
                    f"Provide a value for '{f}' or explain the gap."))

        elif rtype == "regex":
            f = rule.get("field", "")
            val = row.get(f)
            if not _is_missing(val) and not re.match(rule["pattern"], str(val)):
                issues.append(RuleResult(
                    rid, sev, f"Field '{f}' failed format check (value: '{val}').",
                    f"Value does not match the expected pattern "
                    f"({rule['pattern']}).",
                    f"Correct '{f}' to match the required format, or update "
                    "the rubric if the pattern is too strict."))

        elif rtype == "allowed_values":
            f = rule["field"]
            val = row.get(f)
            if not _is_missing(val) and val not in rule["values"]:
                issues.append(RuleResult(
                    rid, sev,
                    f"Field '{f}' has unexpected value '{val}' "
                    f"(allowed: {', '.join(map(str, rule['values']))}).",
                    "Values outside the approved list indicate a deviation "
                    "from the provided instructions.",
                    f"Change '{f}' to one of the allowed values, or extend "
                    "the rubric if this is a legitimate new value."))

        elif rtype == "numeric_range":
            f = rule["field"]
            val = row.get(f)
            if not _is_missing(val):
                num = _to_number(val)
                lo, hi = rule.get("min"), rule.get("max")
                out_of_range = (
                    (num is None) or
                    (lo is not None and num < lo) or
                    (hi is not None and num > hi))
                if out_of_range:
                    issues.append(RuleResult(
                        rid, sev, f"Field '{f}' out of range (value: {val}).",
                        f"Expected a number between {lo} and {hi}.",
                        f"Correct '{f}' or verify the measurement source."))

        elif rtype == "unique":
            f = rule["field"]
            val = row.get(f)
            if not _is_missing(val):
                if val in seen_values[f]:
                    issues.append(RuleResult(
                        rid, sev,
                        f"Duplicate value '{val}' in field '{f}' "
                        f"(first seen on row {seen_values[f][val]}).",
                        "Duplicate identifiers undermine traceability and "
                        "skew counts.",
                        "Deduplicate the record or assign a unique value."))
                else:
                    seen_values[f][val] = row_num

        elif rtype == "cross_field":
            cond = rule.get("if", {})
            cf, cv = cond.get("field"), cond.get("equals")
            if row.get(cf) == cv:
                for tf in rule.get("then_required", []):
                    if _is_missing(row.get(tf)):
                        issues.append(RuleResult(
                            rid, sev,
                            f"'{tf}' must be filled when '{cf}' is '{cv}'.",
                            "Consistency rule: dependent fields must "
                            "carry an explanation to stay actionable.",
                            f"Fill in '{tf}' to explain the state, or "
                            f"reconsider the value of '{cf}'."))

    return issues


def build_feedback(row_num, row, issues, ai_verdict=None,
                    ai_rationale=None):
    """Generate clear, actionable written feedback for a flagged row."""
    worst = max((i.severity for i in issues),
                key=lambda s: SEVERITY_ORDER.get(s, 1))
    if ai_verdict == "dismissed":
        head = (f"Row {row_num} - AI judgment: FALSE POSITIVE "
                f"(originally flagged {worst}, reviewed and dismissed)")
    elif ai_verdict == "unclear":
        head = (f"Row {row_num} - flagged ({worst} severity) with "
                f"{len(issues)} issue(s); AI judgment: UNCLEAR - "
                f"rationale documented below")
    elif ai_verdict == "confirmed":
        head = (f"Row {row_num} - flagged ({worst} severity) with "
                f"{len(issues)} issue(s); AI judgment: CONFIRMED")
    else:
        head = (f"Row {row_num} - flagged ({worst} severity) "
                f"with {len(issues)} issue(s):")
    lines = [head]
    for i in issues:
        lines.append(f"  - [{i.severity}] {i.rule_id}: {i.message}")
        lines.append(f"    Resolution: {i.resolution}")
    if ai_verdict and ai_rationale:
        lines.append(f"  AI rationale: {ai_rationale}")
    return "\n".join(lines)


def evaluate_dataset(rows, rubric, ai_adjudicator=None):
    """Run the full review pipeline over a list of row dicts.

    If ai_adjudicator is provided (and the rubric enables it with
    rubric["ai"]["review_flagged"]), flagged rows are sent to the LLM for
    judgment: confirmed flags stay, dismissed ones are reclassified, and
    unclear cases get a documented rationale.

    Returns a dict with per-row results, aggregate stats, escalation
    candidates, and generated feedback.
    """
    seen_values = defaultdict(dict)
    row_results = []

    for idx, row in enumerate(rows, start=1):
        issues = evaluate_row(row, idx, rubric, seen_values)
        status = PASS if not issues else FLAG
        row_results.append({
            "row_num": idx,
            "status": status,
            "issues": issues,
            "row": row,
            "ai_verdict": None,
            "ai_rationale": None,
        })

    # --- optional AI judgment pass ---
    ai_enabled = ai_adjudicator is not None and         bool((rubric.get("ai") or {}).get("review_flagged"))
    ai_notes = {}
    if ai_enabled:
        flagged = [r for r in row_results if r["status"] == FLAG]
        if flagged:
            ai_notes = ai_adjudicator(flagged, rubric)
        for r in row_results:
            note = ai_notes.get(r["row_num"])
            if not note:
                continue
            r["ai_verdict"] = note["verdict"]
            r["ai_rationale"] = note["rationale"]
            if note["resolution"]:
                for i in r["issues"]:
                    i.resolution = (i.resolution + " AI resolution: "
                                    + note["resolution"])
            if note["verdict"] == "dismissed":
                r["status"] = PASS

    # --- stats over final statuses ---
    rule_counts = defaultdict(int)
    for r in row_results:
        for i in r["issues"]:
            rule_counts[i.rule_id] += 1

    all_feedback = []
    for r in row_results:
        if r["issues"]:
            all_feedback.append(build_feedback(
                r["row_num"], r["row"], r["issues"],
                ai_verdict=r["ai_verdict"], ai_rationale=r["ai_rationale"]))

    total = len(rows)
    flagged = sum(1 for r in row_results if r["status"] == FLAG)
    stats = {
        "total_rows": total,
        "flagged_rows": flagged,
        "pass_rate": round((total - flagged) / total * 100, 2) if total else 0.0,
        "issues_by_rule": dict(rule_counts),
        "issues_by_severity": {
            sev: sum(1 for r in row_results for i in r["issues"]
                     if i.severity == sev)
            for sev in ("low", "medium", "high", "critical")},
        "ai_judged": sum(1 for r in row_results if r["ai_verdict"]),
        "ai_dismissed": sum(1 for r in row_results
                            if r["ai_verdict"] == "dismissed"),
        "ai_unclear": sum(1 for r in row_results
                          if r["ai_verdict"] == "unclear"),
    }

    # Pattern / recurring issue detection -> escalation
    esc_cfg = rubric.get("escalation", {})
    min_count = esc_cfg.get("min_count", 5)
    threshold_pct = esc_cfg.get("threshold_pct", 10)
    escalations = []
    for rule_id, count in rule_counts.items():
        pct = count / total * 100 if total else 0
        if count >= min_count or pct >= threshold_pct:
            escalations.append({
                "rule_id": rule_id,
                "occurrences": count,
                "pct_of_dataset": round(pct, 2),
                "recommendation": (
                    f"Rule '{rule_id}' failed {count} time(s) "
                    f"({pct:.1f}% of {total} rows). This is a systemic "
                    f"pattern, not an individual error: escalate to the "
                    f"data owner / upstream process for root-cause analysis "
                    f"instead of fixing records one by one."),
            })
    escalations.sort(key=lambda e: -e["occurrences"])

    return {
        "row_results": row_results,
        "stats": stats,
        "escalations": escalations,
        "feedback": all_feedback,
    }
