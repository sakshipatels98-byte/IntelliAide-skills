#!/usr/bin/env python3
"""
extract_cluster.py — Step 1 of IntelliAide pure-skills pipeline.

Runs `oc adm must-gather` against the live cluster and uses the resulting
bundle as the data source.  The must-gather output is stored under
<job_dir>/must-gather-raw/ and cluster_dir is set to the content root
inside that bundle (the subdirectory that contains namespaces/,
cluster-scoped-resources/, etc.).

Usage:
    python /app/skills/intelliaide/extract_cluster.py --query "etcd pods not ready"

    # Reuse an existing job dir (skips extraction entirely)
    python /app/skills/intelliaide/extract_cluster.py --query "..." --job-dir /tmp/intelliaide/abc123

Output (stdout JSON):
    {
      "job_id":      "<8-char id>",
      "job_dir":     "/tmp/intelliaide/<job_id>",
      "cluster_dir": "/tmp/intelliaide/<job_id>/must-gather-raw/...",
      "mode":        "must-gather",
      "success":     true,
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
import time
import uuid
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent
_JOB_BASE  = "/tmp/intelliaide"


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


def _find_must_gather_content_root(raw_dir: Path) -> Path:
    """Return the content root directory of an oc adm must-gather bundle.

    oc adm must-gather creates:
        <raw_dir>/
          must-gather.local.XXXXXXXX/   ← outer timestamp dir
            <content-folder>/           ← actual data root (namespaces/, etc.)
              namespaces/
              cluster-scoped-resources/
              events.yaml
              ...

    We identify the content-folder as the directory whose children include
    'namespaces/' or 'cluster-scoped-resources/'.  Using the first child
    sorted alphabetically (the old behaviour) is wrong because
    'cluster-scoped-resources' sorts before 'namespaces', so iterdir() can
    return a subdirectory *inside* the content folder instead of the content
    folder itself.
    """
    outer_dirs = [p for p in raw_dir.iterdir() if p.is_dir()]
    if not outer_dirs:
        return raw_dir
    outer = outer_dirs[0]

    content_dirs = [p for p in outer.iterdir() if p.is_dir()]
    if not content_dirs:
        return outer

    # Find the directory whose immediate children include the well-known
    # must-gather top-level directories.
    for candidate in content_dirs:
        try:
            children = {p.name for p in candidate.iterdir() if p.is_dir()}
        except OSError:
            continue
        if "namespaces" in children or "cluster-scoped-resources" in children:
            return candidate

    # Fallback: original behaviour (first entry) if structure is unexpected.
    return content_dirs[0]


_MUST_GATHER_POLL_INTERVAL = 10   # seconds between readiness checks
_MUST_GATHER_READY_FILES  = 3    # minimum namespace dirs under namespaces/ to be considered populated
_MUST_GATHER_TIMEOUT_SECS = 300  # 10 minutes — must-gather typically takes 3-7 min


def _must_gather_content_ready(content_root: Path) -> bool:
    """Return True once the must-gather content directory has meaningful data.

    oc adm must-gather writes a summary/metadata file in the outer directory
    almost immediately, so a simple entry-count check triggers too early.
    We require the `namespaces/` subdirectory to exist and contain at least
    _MUST_GATHER_READY_FILES namespace directories, which only appears once the
    collector pod has actually started exporting cluster data.
    """
    if not content_root.exists():
        return False
    namespaces_dir = content_root / "namespaces"
    if not namespaces_dir.exists():
        return False
    try:
        ns_entries = list(namespaces_dir.iterdir())
    except OSError:
        return False
    return len(ns_entries) >= _MUST_GATHER_READY_FILES


def _run_must_gather(job_dir: Path) -> "tuple[Path, bool, int]":
    """Run oc adm must-gather and return (cluster_dir, success, return_code).

    `oc adm must-gather` itself can take 5-10 minutes.  The LLM agent that
    invokes this script via a bash tool call has a ~2-minute per-command timeout.
    To avoid that timeout we launch must-gather with Popen (non-blocking), return
    from the subprocess.Popen call immediately, and then poll until the bundle is
    populated or _MUST_GATHER_TIMEOUT_SECS is reached.

    The actual bundle data is written asynchronously by the must-gather collector
    pod; polling the destination directory is the correct way to detect completion.
    """
    raw_dir = job_dir / "must-gather-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    cmd = ["oc", "adm", "must-gather", f"--dest-dir={raw_dir}"]
    log_path = job_dir / "must-gather.log"
    print(f"[extract_cluster] Launching (background): {' '.join(cmd)}", file=sys.stderr)
    print(f"[extract_cluster] must-gather stdout/stderr → {log_path}", file=sys.stderr)

    # Use Popen so we don't block waiting for must-gather to exit.
    # must-gather exits after the collector pod is launched; the pod keeps
    # writing data for several minutes.  We poll the destination directory
    # instead of waiting for the process to exit.
    #
    # IMPORTANT: redirect both stdout and stderr to a log file.
    # Without this, oc adm must-gather writes megabytes of progress output
    # directly to the script's inherited stdout, which Claude Code's bash tool
    # accumulates as the tool-call result. After ~290 s this exceeds the SDK's
    # 1 MB JSON message buffer limit and crashes the agent session.
    log_fh = open(log_path, "w")  # noqa: WPS515 — kept open for subprocess lifetime
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh)

    # --- Wait for the collector pod to finish writing the bundle ---
    deadline = time.monotonic() + _MUST_GATHER_TIMEOUT_SECS
    elapsed = 0
    cluster_dir = _find_must_gather_content_root(raw_dir)

    while time.monotonic() < deadline:
        cluster_dir = _find_must_gather_content_root(raw_dir)
        if _must_gather_content_ready(cluster_dir):
            print(
                f"[extract_cluster] Must-gather bundle ready after ~{elapsed}s "
                f"({len(list(cluster_dir.iterdir()))} entries).",
                file=sys.stderr,
            )
            break
        print(
            f"[extract_cluster] Waiting for must-gather bundle… "
            f"elapsed={elapsed}s content_root={cluster_dir}",
            file=sys.stderr,
        )
        _log_pod(
            f"Step 1/4 — must-gather: waiting for bundle ({elapsed}s elapsed)…"
        )
        time.sleep(_MUST_GATHER_POLL_INTERVAL)
        elapsed += _MUST_GATHER_POLL_INTERVAL
    else:
        print(
            f"[extract_cluster] WARNING: must-gather bundle not fully populated "
            f"after {_MUST_GATHER_TIMEOUT_SECS}s. Proceeding with partial data.",
            file=sys.stderr,
        )

    # Reap the background process (may still be running — that is expected).
    rc = proc.poll()
    if rc is None:
        # Process still running; that is fine — we have the data we need.
        rc = 0
    success = rc == 0

    log_fh.flush()
    log_fh.close()

    print(f"[extract_cluster] Must-gather content root: {cluster_dir}", file=sys.stderr)
    return cluster_dir, success, rc


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

    job_dir.mkdir(parents=True, exist_ok=True)

    mode = "must-gather"
    _log_pod(f"Step 1/4 — extract_cluster  job_id={job_id}  mode=must-gather")
    _log_pod(f"Step 1/4 — running oc adm must-gather → {job_dir}/must-gather-raw/")
    print(f"[extract_cluster] job_id={job_id}  mode=must-gather", file=sys.stderr)

    cluster_dir, success, rc = _run_must_gather(job_dir)

    # --- Write state.json so all subsequent skill scripts know the query + paths ---
    state = {
        "job_id":      job_id,
        "job_dir":     str(job_dir),
        "cluster_dir": str(cluster_dir),
        "query":       args.query,
        "mode":        mode,
    }
    (job_dir / "state.json").write_text(json.dumps(state, indent=2))

    _log_pod(f"Step 1/4 — extract_cluster done  mode={mode}  success={success}  rc={rc}")
    print(json.dumps({
        "job_id":      job_id,
        "job_dir":     str(job_dir),
        "cluster_dir": str(cluster_dir),
        "mode":        mode,
        "success":     success,
        "return_code": rc,
    }))


if __name__ == "__main__":
    main()
