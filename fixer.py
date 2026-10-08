"""Auto-fix layer: corrects flagged issues in datasets.

Deterministic, safe fixes run first (trim whitespace, clamp numbers,
case-normalize or map allowed values, fill configured defaults, drop
duplicate rows). Whatever remains flagged can then be corrected by the
optional AI fixer. Every change is logged as a before/after entry so
nothing is ever silently altered.
"""
import copy


def _issue_get(issue, key, default=None):
    if isinstance(issue, dict):
        return issue.get(key, default)
    return getattr(issue, key, default)


def _rule_for(rubric, rule_id):
    for r in rubric.get("rules", []):
        if r.get("id", r.get("type", "unnamed")) == rule_id:
            return r
    return None


def _fields_for_rule(rule, row):
    """Which fields does this rule implicate for this row."""
    t = rule.get("type")

    def _missing(v):
        return v is None or (isinstance(v, str) and v.strip() == "")

    if t == "required_fields":
        return [f for f in rule.get("fields", []) if _missing(row.get(f))]
    if t in ("non_empty", "regex", "allowed_values", "numeric_range",
             "unique"):
        return [rule.get("field")]
    if t == "cross_field":
        return list(rule.get("then_required", []))
    return []


def _fmt_number(n, like):
    """Format a number, mirroring the style of the original value."""
    if isinstance(like, str):
        like_ = like.strip()
        if "." not in like_:
            return str(int(round(n)))
        return str(round(n, 2))
    return n


def _log(log, row_num, field, old, new, method, rule_id):
    log.append({"row_num": row_num, "field": field,
                "old": "" if old is None else str(old),
                "new": "" if new is None else str(new),
                "method": method, "rule_id": rule_id})


def _deterministic_fixes(row, row_num, issues, rubric, options, log):
    """Apply all safe fixes to one row. Mutates row, appends to log."""
    for issue in issues:
        rid = _issue_get(issue, "rule_id")
        rule = _rule_for(rubric, rid)
        if not rule:
            continue
        fixes = rule.get("fixes") or {}
        fields = _fields_for_rule(rule, row)
        for f in fields:
            if f not in row:
                continue
            val = row[f]

            # 1. trim stray whitespace on flagged string fields (safe)
            if options.get("trim", True) and isinstance(val, str) \
                    and val != val.strip() and val.strip() != "":
                row[f] = val.strip()
                _log(log, row_num, f, val, row[f], "trim", rid)

            val = row[f]

            # 2. configured default for missing fields
            if "default" in fixes and (val is None or
                                      (isinstance(val, str) and
                                       val.strip() == "")):
                row[f] = fixes["default"]
                _log(log, row_num, f, val, row[f], "default", rid)
                continue

            # 3. allowed values: case-normalize or explicit map
            if rule.get("type") == "allowed_values" and isinstance(val, str):
                vals = [str(v) for v in rule.get("values", [])]
                mapped = None
                for wrong, right in (fixes.get("map") or {}).items():
                    if str(val).strip().lower() == str(wrong).strip().lower():
                        mapped = right
                        break
                if mapped is None:
                    for v in vals:
                        if val.strip().lower() == v.strip().lower() \
                                and val != v:
                            mapped = v
                            break
                if mapped is not None and mapped != val:
                    row[f] = mapped
                    _log(log, row_num, f, val, mapped, "map", rid)
                    continue

            # 4. numeric range: clamp out-of-range numbers
            if rule.get("type") == "numeric_range" \
                    and options.get("clamp", True):
                try:
                    n = float(val)
                    lo = rule.get("min")
                    hi = rule.get("max")
                    clamped = min(max(n, lo if lo is not None else n),
                                  hi if hi is not None else n)
                    if clamped != n:
                        row[f] = _fmt_number(clamped, val)
                        _log(log, row_num, f, val, row[f], "clamp", rid)
                except (TypeError, ValueError):
                    pass  # non-numeric: leave for the AI fixer


def fix_dataset(rows, issues_by_row, rubric, options=None, ai_fixer=None,
                engine_module=None):
    """Correct flagged issues across a dataset.

    rows:            original row dicts, list order preserved
    issues_by_row:   {row_num: [issue, ...]} from the latest review
    options:         {"trim": True, "clamp": True, "drop_duplicates": True}
    ai_fixer:        optional callable(flagged_payload, rubric) ->
                     {row_num: {field: new_value}}

    Returns {"rows": corrected_rows, "fix_log": [...], "summary": {...}}.
    Dropped duplicate rows are removed from the returned rows and logged.
    """
    options = options or {}
    corrected = copy.deepcopy([r for r in rows])
    log = []
    dropped = []

    # --- pass 1: deterministic fixes + duplicate removal ---
    for i, row in enumerate(corrected, start=1):
        issues = issues_by_row.get(i) or []
        if not issues:
            continue
        _deterministic_fixes(row, i, issues, rubric, options, log)
        for issue in issues:
            rid = _issue_get(issue, "rule_id")
            rule = _rule_for(rubric, rid)
            if rule and rule.get("type") == "unique" \
                    and options.get("drop_duplicates", True):
                dropped.append(i)
                break

    if dropped:
        dropped_set = set(dropped)
        corrected = [r for i, r in enumerate(corrected, start=1)
                     if i not in dropped_set]
        for i in dropped:
            for issue in issues_by_row.get(i) or []:
                rid = _issue_get(issue, "rule_id")
                rule = _rule_for(rubric, rid)
                if rule and rule.get("type") == "unique":
                    _log(log, i, rule.get("field"), "", "",
                         "dropped_duplicate", rid)
                    break

    # --- pass 2: figure out what is still flagged ---
    def still_flagged(rows_):
        if engine_module is None:
            return {}
        res = engine_module.evaluate_dataset(rows_, rubric)
        return {rr["row_num"]: rr for rr in res["row_results"]
                if rr["status"] == "flagged"}

    flagged = still_flagged(corrected)

    # --- pass 3: AI fixes for the remainder ---
    ai_applied = 0
    if ai_fixer and flagged:
        payload = []
        for num, rr in flagged.items():
            fields = sorted({f for issue in rr["issues"]
                             for f in _fields_for_rule(
                                 _rule_for(rubric,
                                           _issue_get(issue, "rule_id")) or
                                 {}, rr["row"])})
            if fields:
                payload.append({"row_num": num, "row": rr["row"],
                                "flagged_fields": fields})
        if payload:
            suggestions = ai_fixer(payload, rubric) or {}
            for num, fixes_by_field in suggestions.items():
                if num not in flagged:
                    continue
                for f, new_val in fixes_by_field.items():
                    if f not in flagged[num]["row"]:
                        continue
                    old = flagged[num]["row"][f]
                    if str(new_val) == str(old):
                        continue
                    flagged[num]["row"][f] = new_val
                    _log(log, num, f, old, new_val, "ai", "ai_fix")
                    ai_applied += 1

    # --- pass 4: final state ---
    final_flagged = still_flagged(corrected)
    by_method = {}
    for e in log:
        by_method[e["method"]] = by_method.get(e["method"], 0) + 1

    return {
        "rows": corrected,
        "fix_log": log,
        "summary": {
            "rows_changed": len({e["row_num"] for e in log
                                if e["method"] != "dropped_duplicate"}),
            "changes_made": len(log),
            "rows_dropped": len(dropped),
            "ai_fixes": ai_applied,
            "still_flagged": len(final_flagged),
            "by_method": by_method,
        },
    }
