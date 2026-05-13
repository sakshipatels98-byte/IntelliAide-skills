# IntelliAide RCA Skills

You have access to the IntelliAide root-cause analysis engine for OpenShift clusters.

## When to use IntelliAide

Use IntelliAide whenever the task involves any of the following:
- Root cause analysis (RCA) of a cluster issue
- Investigating why pods are failing, crashing, or not ready
- Analysing degraded cluster components (etcd, API server, networking, storage, etc.)
- Processing AlertManager alerts, ACS findings, or user-reported problems
- Generating a structured remediation report

## How to run IntelliAide — ONE command, ONE call

Call the following script and **wait for it to return**:

```
python /app/skills/intelliaide/run_rca.py --query "<problem statement>"
```

**Replace `<problem statement>`** with the exact issue described in the Proposal request,
for example:
  - `--query "etcd pods not ready in openshift-etcd, endpoint health is degraded"`
  - `--query "JVM OOMKill: pod jvm-oomkill-demo in namespace lightspeed-demo is crash-looping"`
  - `--query "OpenShift upgrade failed at 4.14 to 4.15, cluster operators reporting degraded"`

### What the script does internally (you do NOT manage this)

The script handles the complete IntelliAide lifecycle without any input from you:

1. Starts the IntelliAide engine as a local process on localhost
2. Submits the RCA job
3. Polls status every 60 seconds (this runs for 10–30 minutes — this is normal)
4. Retrieves the full structured report when the job is complete
5. Stops the engine
6. Prints the JSON report to stdout

**Do NOT call any other scripts. Do NOT try to poll status yourself. Do NOT spawn subagents.**
Just call `run_rca.py` once and wait.

### Progress messages

While the script runs, you will see log lines like:
```
[IntelliAide] state=running phase=log_processing progress=65% pass=2/3 — Processing logs
```
These are status updates — they mean the RCA is in progress. Keep waiting.

---

## Output — JSON report

When the script finishes, it prints a JSON object. Do NOT print this JSON verbatim.
Instead, **map each field to the output schema** as described below.

```json
{
  "job_id":                    "<uuid>",
  "executive_summary":         "The root cause is ...",
  "chronology_of_events":      "T+0s: pod failed ...",
  "primary_root_causes":       "1. Memory limit exceeded ...",
  "secondary_causes":          "1. Resource quota too tight ...",
  "aggregated_error_patterns": "OOMKill appeared 14 times ...",
  "recommendations":           "1. Increase memory limit ...",
  "evidence_files":            ["/tmp/cluster-extract/<job>/logs/pod.log", ...],
  "total_cost_usd":            0.14,
  "rca_text":                  "<full markdown report>"
}
```

---

## Output Schema Mapping

Map the JSON fields to the `rcaSummary` object in the output schema:

| JSON field                  | Output schema field                   |
|-----------------------------|---------------------------------------|
| `executive_summary`         | `rcaSummary.executiveSummary`         |
| `chronology_of_events`      | `rcaSummary.chronologyOfEvents`       |
| `primary_root_causes`       | `rcaSummary.primaryRootCauses`        |
| `secondary_causes`          | `rcaSummary.secondaryCauses`          |
| `aggregated_error_patterns` | `rcaSummary.aggregatedErrorPatterns`  |
| `recommendations`           | `rcaSummary.recommendations`          |
| `evidence_files`            | `rcaSummary.evidenceFiles`            |
| `total_cost_usd`            | `rcaSummary.totalCostUsd`             |

> **Verification signal**: If `total_cost_usd > 0`, IntelliAide ran successfully.
> If `total_cost_usd == 0` or the `error` key is present, the run failed — report the error.

---

## Error handling

If the script prints `{"error": "..."}` or exits with a non-zero code:

1. Report the error message in your analysis
2. Fall back to standard kubectl/oc-based investigation
3. Note in `rcaSummary.executiveSummary` that IntelliAide was unavailable

---

## Environment variables (set by the operator — no action required)

| Variable                       | Purpose                                 |
|--------------------------------|-----------------------------------------|
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP service account key for Vertex AI |
| `ANTHROPIC_VERTEX_PROJECT_ID`  | GCP project ID                          |
| `CLOUD_ML_REGION`              | Vertex AI region                        |
| `INTELLIAIDE_MAX_WAIT_MINUTES` | Maximum wait (default: 90 min)          |
| `INTELLIAIDE_POLL_INTERVAL`    | Poll frequency (default: 60 s)          |
