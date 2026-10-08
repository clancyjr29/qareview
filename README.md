# QA Review

A rubric-based data review SaaS in pure Python + Flask + SQLite. Built to run
comfortably on Termux.

## What it does

- Ingest CSV datasets (upload file or paste text) and review every row against
  a structured rubric.
- Flag inconsistencies, errors, and outputs that deviate from the rubric, each
  with a severity, a rationale, and a suggested resolution path.
- Apply the same rubric consistently across arbitrarily large datasets.
- Detect recurring issues (patterns) and escalate them as systemic problems
  with an escalation recommendation, instead of treating them one by one.
- Generate clear, actionable written feedback for every flagged row.
- Dashboard stats, filters by rule/severity/status, CSV export of results.
- Multi-user with automatic API keys, plus a stateless JSON API.

## Run on Termux

```bash
pkg update && pkg install python
# copy the qareview folder to your phone, then:
cd qareview
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000 in your phone's browser. On the same Wi-Fi, other
devices can reach it at http://<your-phone-ip>:5000 (find your IP with
`ifconfig wlan0`).

## Rubrics

Rubrics live in `rubrics/*.json`. Rules support:

| Type            | What it checks                                    |
|-----------------|---------------------------------------------------|
| required_fields | listed fields exist and are non-empty             |
| non_empty       | a single field is non-empty                       |
| regex           | field matches a pattern (emails, ids, formats)     |
| allowed_values  | field value is in an approved list                |
| numeric_range   | number between min and max                        |
| unique          | no duplicate values (dedup / traceability)         |
| cross_field     | if field X equals V, then fields Y,Z must be set  |

Every rule carries a `severity` (low/medium/high/critical), and the rubric
carries `escalation` settings (`min_count`, `threshold_pct`): when a rule fails
often enough, the engine raises a systemic escalation instead of just flagging
individual rows.

## Optional LLM judgment hook (AI layer)

Flagged rows can be adjudicated by an LLM: it **confirms** genuine
deviations, **dismisses** false positives (the row then passes), and marks
**unclear** cases where the guidelines genuinely don't cover the scenario -
always with a written rationale and a suggested resolution. That covers the
"exercise sound judgment and document rationale in ambiguous scenarios"
requirement.

Setup via environment variables (all optional):

```bash
export QA_LLM_API_KEY=sk-...          # any OpenAI-compatible key
export QA_LLM_BASE_URL=https://api.openai.com/v1   # default
export QA_LLM_MODEL=gpt-4o-mini       # default
python app.py
```

Then either tick "Run AI judgment on flagged rows" on the upload page, set
`"ai": {"review_flagged": true}` in a rubric, or pass `"use_ai": true` to
`POST /api/review`. Per-rubric reviewer instructions can be added as
`rubric["ai"]["instructions"]`, e.g. "treat timeout-related failures as
upstream issues."

Works with any OpenAI-compatible endpoint: OpenAI, Groq, OpenRouter, or a
local model (Ollama, llama.cpp server) - local endpoints don't need a key:

```bash
export QA_LLM_BASE_URL=http://localhost:11434/v1   # Ollama
export QA_LLM_MODEL=llama3.2
```

Failure behavior is deliberate: if the LLM call fails, rows keep their
rule-based verdicts and the review still completes.

## Auto-fixing flagged datasets

The app can correct flagged issues instead of just reporting them. On a
dataset page, use the "Auto-fix flagged issues" panel:

- **Safe deterministic fixes** (on by default): trim stray whitespace,
  clamp out-of-range numbers into the rubric's min/max, drop duplicate
  rows, case-normalize or map allowed values, and fill configured defaults.
- **AI corrections** (checkbox, needs an API key): anything still flagged
  is sent to the LLM, which proposes minimal corrections for the flagged
  fields only.

Fixing never modifies the original dataset: it creates a `<name> (fixed)`
copy, re-reviews it against the same rubric, and records a before/after
fix log (row, field, old value, new value, method, rule). "Export data CSV"
on the fixed dataset gives you the clean corrected file.

Rubric hooks: rules accept an optional `"fixes"` object:

```json
{"id": "failure_needs_reason", "type": "cross_field",
 "if": {"field": "status", "equals": "fail"},
 "then_required": ["failure_reason"],
 "fixes": {"default": "No failure reason documented."}}
```

and `allowed_values` rules accept `"fixes": {"map": {"passed": "pass"}}`
to auto-correct known typos.

API equivalent:

```bash
curl -X POST -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"use_ai": true, "options": {"clamp": true}}' \
  http://localhost:5000/api/datasets/<dataset_id>/fix
```

Returns the new fixed dataset id, a change summary, and the full fix log.

## Data analysis

Every dataset has an **Analysis** page (button on the dataset page) that
turns the data into something usable:

- **Quality score (0-100)** - pass rate minus severity-weighted penalties,
  so critical issues hurt more than minor ones. The formula is in
  `analyzer.py` and shown on the page.
- **Column profiles** - type inference (numeric/categorical/text),
  missingness, distinct counts, min/max/mean/median, outliers, top values.
- **Breakdowns** - flag rate by group ("rows with status=fail get flagged
  80% of the time") and average-of-numeric by group (e.g. average score
  per worker).
- **Plain-language insights** - error concentration, high-missingness
  columns, constant columns, outliers.
- **AI executive summary** (optional, one call, only when you click the
  button) - a written overview with recommended next actions.
- **Downloadable report** - one Markdown file combining the score,
  narrative, insights, column profiles, breakdowns, escalations and
  feedback. Shareable as-is.

API: `GET /api/datasets/<id>/analysis` (add `?ai=1` for the narrative).
Report: `GET /datasets/<id>/report` (add `?ai=1`).

## JSON API

Log in once via the web UI to create your user, then use the key from the
`users` table (or add a `GET /api/whoami` if you extend this).

Stateless review (no storage), great for scripting:

```bash
curl -s localhost:5000/api/review -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" -d '{
    "rubric": {"rules": [{"id": "req", "type": "non_empty",
                          "field": "email", "severity": "high"}],
               "escalation": {"min_count": 2, "threshold_pct": 50}},
    "rows": [{"email": "a@b.com"}, {"email": ""}, {"email": ""}]
  }'
```

Stored-dataset issues for a dataset you uploaded:

```
GET /api/datasets/<dataset_id>/issues     (X-API-Key header)
```

Returns stats, escalations, and all flagged rows with issue details.

## Files

```
app.py          Flask app: web UI + JSON API
engine.py       rubric engine: evaluate, escalate, generate feedback
db.py           SQLite schema and helpers
templates/      web UI
rubrics/        rubric definitions (JSON)
sample_data.csv demo dataset to try it out
```

## Notes

- Database is a single SQLite file (`qareview.db`) - easy to back up or move.
- For real deployment set `QAREVIEW_SECRET` env var to a long random string.
- Change `PORT` env var to run on another port.
