#!/usr/bin/env python3
"""
extract_cluster.py — Step 1 of IntelliAide pure-skills pipeline.

Runs live-cluster-extraction.py as a subprocess using the sandbox pod's
in-cluster service account token to collect live cluster state into a
shared job directory.

Usage:
    python /app/skills/intelliaide/extract_cluster.py --query "etcd pods not ready"
    python /app/skills/intelliaide/extract_cluster.py --query "..." --job-dir /tmp/intelliaide/abc123

Output (stdout JSON):
    {
      "job_id":     "<8-char id>",
      "job_dir":    "/tmp/intelliaide/<job_id>",
      "cluster_dir": "/tmp/intelliaide/<job_id>/cluster",
      "success":    true,
      "return_code": 0
    }

On failure the script still prints JSON with success=false so subsequent steps
can detect partial extraction and attempt to continue with what's available.
"""

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


def _log_pod(msg: str) -> None:
    """Write a progress line directly to the container log stream (PID 1 stdout).

    Skill scripts run as subprocesses of the Claude Code CLI whose stdout is
    piped internally — it never reaches the pod log stream.  Opening PID 1's
    stdout directly is the only way to surface progress in `oc logs`.
    Falls back silently if the file is not accessible.
    """
    line = f"[intelliaide] {msg}\n"
    try:
        with open("/proc/1/fd/1", "a") as fh:
            fh.write(line)
    except Exception:
        sys.stderr.write(line)


# Paths inside the skills image mount (/app/skills/ in the sandbox container)
_APP          = "/app/skills/app"
_EXTRACTOR    = f"{_APP}/Main-program/live-cluster-extraction.py"
_PATHS_FILE   = f"{_APP}/Main-program/file_paths.md"
_JOB_BASE     = "/tmp/intelliaide"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True,
                        help="Problem statement for RCA (passed through to state.json)")
    parser.add_argument("--job-dir", default=None,
                        help="Reuse an existing job dir (skips extraction, updates state.json)")
    args = parser.parse_args()

    # --- Job directory setup ---
    if args.job_dir:
        job_dir = Path(args.job_dir)
        job_id  = job_dir.name
    else:
        job_id  = str(uuid.uuid4())[:8]
        job_dir = Path(_JOB_BASE) / job_id

    cluster_dir = job_dir / "cluster"
    cluster_dir.mkdir(parents=True, exist_ok=True)

    # Write state.json so all subsequent skill scripts know the query + paths
    state = {
        "job_id":      job_id,
        "job_dir":     str(job_dir),
        "cluster_dir": str(cluster_dir),
        "query":       args.query,
    }
    (job_dir / "state.json").write_text(json.dumps(state, indent=2))

    _log_pod(f"Step 1/4 — extract_cluster  job_id={job_id}")
    _log_pod(f"Step 1/4 — extracting live cluster data → {cluster_dir}")
    print(f"[extract_cluster] job_id={job_id}", file=sys.stderr)
    print(f"[extract_cluster] Extracting live cluster data → {cluster_dir}", file=sys.stderr)

    # --- Run extraction ---
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = ":".join(filter(None, [
        f"{_APP}/intelliaide_deps",
        f"{_APP}/Main-program",
        _APP,
        existing_pp,
    ]))

    cmd = [
        sys.executable, _EXTRACTOR,
        "--paths-file", _PATHS_FILE,
        "--output",     str(cluster_dir),
    ]
    proc = subprocess.run(cmd, cwd=_APP, env=env)

    success = proc.returncode == 0
    if not success:
        print(
            f"[extract_cluster] WARNING: extraction exited {proc.returncode}. "
            "Partial data may still be usable.",
            file=sys.stderr,
        )

    _log_pod(f"Step 1/4 — extract_cluster done  success={success}  rc={proc.returncode}")
    print(json.dumps({
        "job_id":      job_id,
        "job_dir":     str(job_dir),
        "cluster_dir": str(cluster_dir),
        "success":     success,
        "return_code": proc.returncode,
    }))


if __name__ == "__main__":
    main()
