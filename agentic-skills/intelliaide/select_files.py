#!/usr/bin/env python3
"""
select_files.py — Step 2 of IntelliAide pure-skills pipeline.

Uses an LLM call to select which extracted cluster files are most relevant to
the problem query, then checks which files actually exist in the extracted
cluster directory. Buckets results into high / medium / low priority.

Usage:
    python /app/skills/intelliaide/select_files.py --job-dir /tmp/intelliaide/<job_id>

Reads:
    <job_dir>/state.json          (query + cluster_dir from extract_cluster.py)

Writes:
    <job_dir>/file_selection.json

Output (stdout JSON):
    {
      "high_count":          N,
      "medium_count":        N,
      "low_count":           N,
      "file_selection_path": "<job_dir>/file_selection.json",
      "problem_category":    "..."
    }
"""

import argparse
import json
import sys
from pathlib import Path

# All IntelliAide engine code lives alongside this script in the same folder.
# At runtime (in the sandbox container) this resolves to /app/skills/intelliaide/
_SKILL_DIR = Path(__file__).resolve().parent
for _p in (
    str(_SKILL_DIR / "vendor"),        # vendored packages (anthropic, etc.)
    str(_SKILL_DIR / "Main-program"),
    str(_SKILL_DIR / "python-client"),
    str(_SKILL_DIR),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from must_gather_file_selector import MustGatherFileSelector   # noqa: E402
from data_analyzer import DataAnalyzer                          # noqa: E402
from app_paths import get_must_gather_docs_dir, get_config_path # noqa: E402


def _log_pod(msg: str) -> None:
    """Write a progress line directly to the container log stream (PID 1 stdout)."""
    line = f"[intelliaide] {msg}\n"
    try:
        with open("/proc/1/fd/1", "a") as fh:
            fh.write(line)
    except Exception:
        sys.stderr.write(line)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", required=True, help="Path to the shared job directory")
    args = parser.parse_args()

    job_dir = Path(args.job_dir)
    state   = json.loads((job_dir / "state.json").read_text())
    query       = state["query"]
    cluster_dir = state["cluster_dir"]

    docs_dir = str(get_must_gather_docs_dir())
    _log_pod(f"Step 2/4 — select_files  query={query[:60]!r}")
    print(f"[select_files] query={query[:80]!r}", file=sys.stderr)
    print(f"[select_files] docs_dir={docs_dir}", file=sys.stderr)
    print(f"[select_files] cluster_dir={cluster_dir}", file=sys.stderr)

    # --- LLM file selection ---
    config_path = str(get_config_path())
    print(f"[select_files] config_path={config_path}", file=sys.stderr)
    selector = MustGatherFileSelector(config_path=config_path)
    result   = selector.suggest_files(query, docs_dir)

    if "error" in result and not result.get("suggested_files"):
        print(
            json.dumps({"error": result["error"], "high_count": 0, "medium_count": 0, "low_count": 0}),
        )
        sys.exit(1)

    suggested_files = result.get("suggested_files", [])
    print(
        f"[select_files] LLM suggested {len(suggested_files)} files "
        f"(category={result.get('problem_category', 'Unknown')})",
        file=sys.stderr,
    )

    # --- Resolve which files actually exist in the cluster extraction ---
    analyzer     = DataAnalyzer(must_gather_base_dir=cluster_dir)
    all_paths    = [f["path"] for f in suggested_files]
    availability = analyzer.report_files_availability(all_paths)

    # Map original path → resolved path for files that were found
    found = {
        e["original"]: e["resolved"]
        for e in availability.get("found_in_supplied_dir", [])
    }
    print(
        f"[select_files] {len(found)}/{len(all_paths)} files resolved in {cluster_dir}",
        file=sys.stderr,
    )

    # --- Bucket by priority ---
    by_priority: dict[str, list] = {"high": [], "medium": [], "low": []}
    for f in suggested_files:
        prio = f.get("priority", "low")
        if prio not in by_priority:
            prio = "low"
        orig     = f["path"]
        resolved = found.get(orig, orig)
        by_priority[prio].append({
            "original": orig,
            "resolved": resolved,
            "found":    orig in found,
            "reason":   f.get("reason", ""),
        })

    selection = {
        "query":            query,
        "cluster_dir":      cluster_dir,
        "problem_category": result.get("problem_category", "Unknown"),
        "high":             by_priority["high"],
        "medium":           by_priority["medium"],
        "low":              by_priority["low"],
        "input_tokens":     result.get("input_tokens", 0),
        "output_tokens":    result.get("output_tokens", 0),
    }

    selection_path = str(job_dir / "file_selection.json")
    Path(selection_path).write_text(json.dumps(selection, indent=2))

    print(
        f"[select_files] high={len(by_priority['high'])} "
        f"medium={len(by_priority['medium'])} "
        f"low={len(by_priority['low'])}",
        file=sys.stderr,
    )

    _log_pod(
        f"Step 2/4 — select_files done  "
        f"high={len(by_priority['high'])} medium={len(by_priority['medium'])} "
        f"low={len(by_priority['low'])}  category={result.get('problem_category', 'Unknown')}"
    )
    print(json.dumps({
        "high_count":          len(by_priority["high"]),
        "medium_count":        len(by_priority["medium"]),
        "low_count":           len(by_priority["low"]),
        "file_selection_path": selection_path,
        "problem_category":    result.get("problem_category", "Unknown"),
    }))


if __name__ == "__main__":
    main()
