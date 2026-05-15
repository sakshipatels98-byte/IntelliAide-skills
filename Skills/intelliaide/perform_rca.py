#!/usr/bin/env python3
"""
perform_rca.py — Step 4 (per-priority pass) of IntelliAide pure-skills pipeline.

Calls the LLM-based root-cause analysis engine on the ML-compressed cluster data
produced by analyze_data.py.

  • First pass (no --previous-priority):  calls run_rca_chunked()
  • Continuation pass (--previous-priority <x>): calls run_rca_and_summary_continued()
    with the previous RCA text as context.

Usage:
    # First pass — high priority files
    python /app/skills/intelliaide/perform_rca.py \\
        --job-dir /tmp/intelliaide/<job_id> --priority high

    # Continuation — medium priority, building on high-priority RCA
    python /app/skills/intelliaide/perform_rca.py \\
        --job-dir /tmp/intelliaide/<job_id> --priority medium --previous-priority high

    # Continuation — low priority, building on medium-priority RCA
    python /app/skills/intelliaide/perform_rca.py \\
        --job-dir /tmp/intelliaide/<job_id> --priority low --previous-priority medium

Reads:
    <job_dir>/state.json                   (query)
    <job_dir>/analysis_<priority>.json     (yaml_errors + log_entries)
    <job_dir>/rca_<previous>.json          (previous RCA text, if --previous-priority given)
    <job_dir>/file_selection.json          (to determine has_medium / has_low flags)

Writes:
    <job_dir>/rca_<priority>.json

Output (stdout JSON):
    {
      "priority":   "high",
      "cost_usd":   0.28,
      "has_medium": true,
      "has_low":    true,
      "rca_path":   "<job_dir>/rca_high.json"
    }
"""

import argparse
import json
import sys
from pathlib import Path

_APP = "/app/skills/app"
for _p in (f"{_APP}/intelliaide_deps", f"{_APP}/Main-program", _APP):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from llm_rca_agent import run_rca_chunked, run_rca_and_summary_continued   # noqa: E402
from app_paths import get_config_path                                        # noqa: E402
import app_paths as _ap                                                      # noqa: E402


def _log_pod(msg: str) -> None:
    """Write a progress line directly to the container log stream (PID 1 stdout)."""
    line = f"[intelliaide] {msg}\n"
    try:
        with open("/proc/1/fd/1", "a") as fh:
            fh.write(line)
    except Exception:
        sys.stderr.write(line)

# The skills image volume is mounted read-only by Kubernetes.
# llm_rca_agent.py writes chunk RCA files via lazy `from app_paths import get_results_dir`.
# Redirect to /tmp which is always writable.
_WRITABLE_RESULTS = Path("/tmp/intelliaide-app/Results")
_WRITABLE_RESULTS.mkdir(parents=True, exist_ok=True)
_ap.get_results_dir = lambda: _WRITABLE_RESULTS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir",           required=True)
    parser.add_argument("--priority",          required=True, choices=["high", "medium", "low"])
    parser.add_argument("--previous-priority", default=None,  choices=["high", "medium"],
                        help="Priority tier of the previous RCA pass (omit for first pass)")
    args = parser.parse_args()

    job_dir     = Path(args.job_dir)
    state       = json.loads((job_dir / "state.json").read_text())
    query       = state["query"]
    config_path = str(get_config_path())

    # --- Load analysis data ---
    analysis_file = job_dir / f"analysis_{args.priority}.json"
    analysis      = json.loads(analysis_file.read_text())
    yaml_errors   = analysis.get("yaml_errors", {})
    log_entries   = analysis.get("log_entries", []) or None

    _log_pod(
        f"Step 4/4 — perform_rca  priority={args.priority}  "
        f"yaml_keys={len(yaml_errors)}  log_entries={len(log_entries) if log_entries else 0}"
    )
    print(
        f"[perform_rca] pass={args.priority}  "
        f"yaml_keys={len(yaml_errors)}  log_entries={len(log_entries) if log_entries else 0}",
        file=sys.stderr,
    )

    # --- Run RCA ---
    if args.previous_priority:
        prev_rca_path = job_dir / f"rca_{args.previous_priority}.json"
        prev_rca      = json.loads(prev_rca_path.read_text())
        prev_summary  = prev_rca.get("rca_text", "")
        print(
            f"[perform_rca] Continuing from {args.previous_priority} RCA "
            f"({len(prev_summary)} chars)",
            file=sys.stderr,
        )
        result = run_rca_and_summary_continued(
            ml_classification_result=yaml_errors,
            config_path=config_path,
            problem_statement=query,
            log_error_entries=log_entries,
            previous_rca_summary=prev_summary,
            previous_priority_stage=args.previous_priority,
            new_priority_stage=args.priority,
        )
    else:
        result = run_rca_chunked(
            ml_classification_result=yaml_errors,
            config_path=config_path,
            problem_statement=query,
            log_error_entries=log_entries,
        )

    # Both functions return {"rca_summary": ..., "cost_usd": ..., ...}
    rca_text      = result.get("rca_summary", "")
    cost_usd      = result.get("cost_usd", 0.0)
    input_tokens  = result.get("input_tokens", 0)
    output_tokens = result.get("output_tokens", 0)
    error         = result.get("error")

    if error and not rca_text:
        print(json.dumps({"error": error, "priority": args.priority, "cost_usd": cost_usd}))
        sys.exit(1)

    # --- Determine whether more priority passes are available ---
    file_selection = json.loads((job_dir / "file_selection.json").read_text())
    has_medium     = len(file_selection.get("medium", [])) > 0
    has_low        = len(file_selection.get("low",    [])) > 0

    rca_data = {
        "priority":      args.priority,
        "rca_text":      rca_text,
        "cost_usd":      cost_usd,
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "has_medium":    has_medium,
        "has_low":       has_low,
        "error":         error,
    }

    rca_path = str(job_dir / f"rca_{args.priority}.json")
    Path(rca_path).write_text(json.dumps(rca_data, indent=2, ensure_ascii=False))

    _log_pod(
        f"Step 4/4 — perform_rca done  priority={args.priority}  "
        f"cost=${cost_usd:.4f}  chars={len(rca_text)}"
    )
    print(
        f"[perform_rca] Done. cost=${cost_usd:.4f} "
        f"chars={len(rca_text)} has_medium={has_medium} has_low={has_low}",
        file=sys.stderr,
    )

    print(json.dumps({
        "priority":   args.priority,
        "cost_usd":   cost_usd,
        "has_medium": has_medium,
        "has_low":    has_low,
        "rca_path":   rca_path,
    }))


if __name__ == "__main__":
    main()
