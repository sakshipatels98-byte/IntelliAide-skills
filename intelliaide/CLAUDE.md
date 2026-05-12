# IntelliAide RCA Skills

You have access to four tools that call the IntelliAide root cause analysis
service. Use them to investigate the issue described in the Proposal request.

---

## CRITICAL RULES — READ FIRST

1. **DO NOT spawn subagents, background tasks, or parallel processes.**
   Execute every step yourself, sequentially, in your main process.
   Never use Task(), subprocess, or any form of delegation.

2. **THINK OUT LOUD at every step.** Before and after each tool call,
   write a brief progress message explaining what you are doing and why.
   This is critical for observability — your thinking messages are the
   only progress indication visible in the logs.

3. **DO NOT skip polling steps.** You must call get_rca_status yourself
   each time. Do not try to batch, skip, or optimize the polling loop.

---

## Workflow — always follow this order

**Step 1 — Start the RCA job**

Say: "Starting IntelliAide RCA job for: <problem summary>"

Call `run_rca` with the problem statement as `--query`.
It returns a `job_id` immediately. The RCA runs in the background.

Say: "RCA job started with job_id: <id>. Will poll status every 2 minutes."

**Step 2 — Poll status (repeat until done)**

A full 3-pass RCA (High → Medium → Low priority) takes **10–30 minutes**.

Before each poll, say: "Polling IntelliAide RCA status (attempt N)..."

Call `get_rca_status` with `--job-id`.

After each poll, say: "RCA status: state=<state>, phase=<phase>, progress=<N>%, pass=<M>/3, message=<msg>"

If state is `"running"` or `"queued"`: wait **2 minutes**, then poll again.
If state is `"completed"`: proceed to Step 3.
If state is `"failed"`: stop and report the `message` field as the failure reason.

**Step 3 — Retrieve results**

Say: "RCA job completed. Retrieving full report..."

Call `get_rca_result` with `--job-id`.
This returns the full structured report.

Say: "RCA report retrieved. Report contains <N> evidence files. Mapping to output schema..."

**Step 4 — Map to output schema**

Map the report to RemediationOptions (see **Output mapping** below).

Say: "Mapped <N> root causes to remediation options. Generating final response."

If you need to abort a job: call `cancel_rca` with `--job-id`.

---

## Tool: run_rca

Start an IntelliAide RCA job on the live cluster.

```
python /skills/intelliaide/run_rca.py --query "<problem statement>"
```

**Output** (JSON on stdout):
```json
{"job_id": "uuid", "state": "queued"}
```

---

## Tool: get_rca_status

Poll the progress of a running RCA job.

```
python /skills/intelliaide/get_rca_status.py --job-id <job_id>
```

**Output** (JSON on stdout):
```json
{
  "job_id": "...",
  "state": "running",           // queued | running | completed | failed | cancelled
  "phase": "rca_analysis",
  "progress": 85,               // 0-100
  "current_pass": 2,            // 1=High 2=Medium 3=Low priority pass
  "message": "Running LLM analysis pass 2/3"
}
```

Keep polling until `state == "completed"`. On `"failed"` stop and report the
`message` field as the failure reason.

---

## Tool: get_rca_result

Fetch the full RCA report for a completed job.

```
python /skills/intelliaide/get_rca_result.py --job-id <job_id>
```

**Output** (JSON on stdout) — key fields:
```json
{
  "job_id": "...",
  "rca_text": "## Executive Summary\n...",   // full Markdown report
  "rca_structured": {
    "user_reported_issue":       "...",
    "executive_summary":         "...",      // one-paragraph overview
    "chronology_of_events":      "...",      // timeline Markdown
    "primary_root_causes":       "...",      // root causes Markdown
    "secondary_causes":          "...",      // contributing factors Markdown
    "aggregated_error_patterns": "...",      // error patterns Markdown
    "recommendations":           "..."       // recommended actions Markdown
  },
  "pass_results": [...],                     // per-pass (High/Med/Low) summaries
  "evidence_files": ["..."],                 // cluster files analysed
  "total_cost_usd": 0.12
}
```

---

## Tool: cancel_rca

Abort a running or queued job.

```
python /skills/intelliaide/cancel_rca.py --job-id <job_id>
```

**Output** (JSON on stdout):
```json
{"job_id": "...", "state": "cancelling"}
```

---

## Output mapping — RemediationOptions

After calling `get_rca_result`, map the `rca_structured` fields to the
operator's required output schema:

### options[] — one entry per distinct root cause

Parse `rca_structured.primary_root_causes` (Markdown) into one
`RemediationOption` per root cause bullet / section.

| RemediationOption field        | Source in rca_structured                           |
|-------------------------------|-----------------------------------------------------|
| `title`                        | Short label for the root cause                     |
| `summary`                      | One-line summary of the root cause                 |
| `diagnosis.rootCause`          | The identified root cause                          |
| `diagnosis.summary`            | `executive_summary` + `chronology_of_events`       |
| `diagnosis.confidence`         | High if evidence is direct; Medium if inferred     |
| `proposal.description`         | Corresponding entry from `recommendations`         |
| `proposal.actions[]`           | Individual recommended steps (one per action item) |
| `proposal.risk`                | High for destructive actions, Medium otherwise     |
| `proposal.reversible`          | Reversible unless action is destructive            |
| `verification.description`     | "Verify the recommended action resolved the issue" |
| `verification.steps[]`         | Derived from `recommendations` check items        |

### rcaSummary — verbatim IntelliAide output

Populate `rcaSummary` in the output directly from `rca_structured`:

| rcaSummary field            | Source field                              |
|-----------------------------|-------------------------------------------|
| `executiveSummary`           | `rca_structured.executive_summary`       |
| `chronologyOfEvents`         | `rca_structured.chronology_of_events`    |
| `primaryRootCauses`          | `rca_structured.primary_root_causes`     |
| `secondaryCauses`            | `rca_structured.secondary_causes`        |
| `aggregatedErrorPatterns`    | `rca_structured.aggregated_error_patterns` |
| `recommendations`            | `rca_structured.recommendations`         |
| `evidenceFiles`              | `evidence_files` (array)                 |
| `totalCostUsd`               | `total_cost_usd`                         |
