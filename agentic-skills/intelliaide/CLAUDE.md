# IntelliAide RCA Skills

You are the orchestrator of the IntelliAide root-cause analysis pipeline for OpenShift clusters.
Unlike the old single-script approach, you now call each IntelliAide component as a separate skill
and decide what to run next based on what each step reports.

---

## When to use IntelliAide

Use IntelliAide whenever the task involves any of the following:
- Root cause analysis (RCA) of a cluster issue
- Investigating why pods are failing, crashing, or not ready
- Analysing degraded cluster components (etcd, API server, networking, storage, etc.)
- Processing AlertManager alerts, ACS findings, or user-reported problems
- Generating a structured remediation report

---

## Progress Reporting to the Console

At the **start of the pipeline** (before Step 1), derive the Proposal name and namespace from
the sandbox pod hostname, then annotate the Proposal so progress is visible in the console and
in `oc describe proposal`:

```bash
# Derive proposal name — pod is always named ls-analysis-<proposal-name>
_PROPOSAL=$(hostname | sed 's/^ls-[^-]*-analysis-//;s/^ls-analysis-//')
_NS="openshift-lightspeed"
```

After **each step** completes, update the progress annotation:

```bash
oc annotate proposal "$_PROPOSAL" -n "$_NS" \
  intelliaide.io/progress="<message>" --overwrite 2>/dev/null || true
```

Use these messages at each step:
- Before Step 1: `"IntelliAide: starting cluster data extraction (live)"`
- After Step 1:  `"IntelliAide: cluster data extracted — selecting relevant files"`
- After Step 2:  `"IntelliAide: file selection complete (high=N, medium=N, low=N) — running ML analysis"`
- After Step 3 analyze: `"IntelliAide: ML analysis complete — generating RCA (high priority)"`
- After Step 3 rca:     `"IntelliAide: high-priority RCA complete — cost=$N"`
- After Step 4 (if run): `"IntelliAide: medium-priority RCA complete"`
- After Step 5 (if run): `"IntelliAide: low-priority RCA complete"`
- At the very end: `"IntelliAide: pipeline complete — mapping to output schema"`

If `oc annotate` fails (RBAC or other), ignore it and continue — it is non-critical.

---

## Step-by-Step Orchestration

Work through the steps below **in order**. After each command, parse the JSON line printed to
stdout (the last line of output) and decide whether to continue.

### Step 1 — Extract live cluster data

```
python /app/skills/intelliaide/extract_cluster.py --query "<problem statement>"
```

Capture `job_dir` from the output JSON. Even if `success=false`, continue — partial data is
usually enough for RCA.

### Step 2 — Select relevant files (all priorities at once)

```
python /app/skills/intelliaide/select_files.py --job-dir <job_dir>
```

Capture `high_count`, `medium_count`, `low_count`. If all three are 0, fall back to standard
`kubectl`/`oc` investigation and note that IntelliAide could not identify relevant files.

### Step 3 — Analyze + RCA for high-priority files

```
python /app/skills/intelliaide/analyze_data.py --job-dir <job_dir> --priority high
python /app/skills/intelliaide/perform_rca.py   --job-dir <job_dir> --priority high
```

After `perform_rca.py`, read `has_medium` and `has_low` from the output JSON.

### Step 4 (only if `has_medium=true`) — Medium-priority pass

```
python /app/skills/intelliaide/analyze_data.py --job-dir <job_dir> --priority medium
python /app/skills/intelliaide/perform_rca.py   --job-dir <job_dir> --priority medium --previous-priority high
```

After this step, read `has_low` from the output JSON.

### Step 5 (only if `has_low=true` after step 4) — Low-priority pass

```
python /app/skills/intelliaide/analyze_data.py --job-dir <job_dir> --priority low
python /app/skills/intelliaide/perform_rca.py   --job-dir <job_dir> --priority low --previous-priority medium
```

### Step 6 — Map final RCA to output schema

Read the final `rca_<last_priority>.json` file from disk:

```
cat <job_dir>/rca_<last_priority>.json
```

The `rca_text` field contains the full markdown RCA report with headings such as:
`## Root Cause`, `## Key Findings`, `## Recommendations`, `## Chronology`.

Parse those sections and map them to the output schema as described in the **Output Schema
Mapping** section below.

---

## Error Handling

| Situation | What to do |
|---|---|
| `extract_cluster.py` exits non-zero | Continue with available data — partial extraction is common |
| `select_files.py` returns all-zero counts | Fall back to `kubectl`/`oc` investigation; note in summary |
| `analyze_data.py` exits non-zero | Skip this priority tier, try the next one |
| `perform_rca.py` prints `{"error": "..."}` | If high-priority RCA failed entirely, fall back to `kubectl`/`oc`; otherwise use what's available |
| Any step times out | Note the partial progress in the summary; use whatever data is available |

Always aim to produce a best-effort RCA even if some steps fail. Never silently skip the
analysis — report what worked and what did not.

---

## Output Schema Mapping

After all steps complete, map the parsed RCA sections to the `rcaSummary` object in the
output schema:

| RCA section heading (in `rca_text`)      | Output schema field                  |
|------------------------------------------|--------------------------------------|
| `## Root Cause` / summary paragraph      | `rcaSummary.executiveSummary`        |
| `## Root Cause` / primary causes         | `rcaSummary.primaryRootCauses`       |
| `## Recommendations` section             | `rcaSummary.recommendations`         |
| Sum of all `cost_usd` from `rca_*.json`  | `rcaSummary.totalCostUsd` (if in schema) |

For the `options` array (if in the output schema), produce one option per major root cause
identified, with:
- `title`: short name for the issue
- `diagnosis.summary`: 1–2 sentence description
- `diagnosis.confidence`: `High` / `Medium` / `Low` based on evidence quality
- `diagnosis.rootCause`: the specific underlying cause
- `proposal.description`: what IntelliAide recommends
- `proposal.actions`: concrete remediation steps
- `proposal.risk`: `Low` / `Medium` / `High` / `Critical`
- `proposal.reversible`: `Reversible` / `Irreversible` / `Partial`

> **Verification signal**: If the total `cost_usd` across all RCA passes is > 0, IntelliAide
> ran successfully. If cost is 0 and `rca_text` is empty, the run failed — fall back and report.

---

## Environment Variables (set by the operator — no action required)

| Variable                         | Purpose                                      |
|----------------------------------|----------------------------------------------|
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP service account key for Vertex AI        |
| `ANTHROPIC_VERTEX_PROJECT_ID`    | GCP project ID                               |
| `CLOUD_ML_REGION`                | Vertex AI region                             |
