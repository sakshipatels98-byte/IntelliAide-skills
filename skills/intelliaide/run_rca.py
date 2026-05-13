#!/usr/bin/env python3
"""IntelliAide RCA — single-command skill for Claude.

Spawn → Poll → Retrieve → Destroy, all internally.

This script:
  1. Starts the IntelliAide MCP server as a background subprocess on localhost:8001
  2. Waits up to 60 s for the server to become ready
  3. Submits the RCA job via the MCP run_rca tool
  4. Polls get_job_status every 60 s until the job reaches a terminal state
  5. Retrieves the full structured report via get_job_result
  6. Terminates the subprocess
  7. Prints the flattened JSON report to stdout (for Claude to map to the output schema)

Claude calls this script ONCE and waits for it to finish.
All lifecycle management is handled here — Claude is not involved in polling or subprocess management.

Usage:
    python /skills/intelliaide/run_rca.py --query "etcd pods not ready in openshift-etcd"

Output (stdout) — JSON:
    {
      "job_id":                    "<uuid>",
      "executive_summary":         "<text>",
      "chronology_of_events":      "<text>",
      "primary_root_causes":       "<text>",
      "secondary_causes":          "<text>",
      "aggregated_error_patterns": "<text>",
      "recommendations":           "<text>",
      "evidence_files":            ["<path>", ...],
      "total_cost_usd":            0.14,
      "rca_text":                  "<full markdown report>"
    }

Environment variables (all inherited from sandbox pod, operator sets them):
    GOOGLE_APPLICATION_CREDENTIALS   GCP service account key for Vertex AI
    ANTHROPIC_VERTEX_PROJECT_ID      GCP project ID
    CLOUD_ML_REGION                  Vertex AI region
    CLUSTER_EXTRACT_DIR              Scratch dir for cluster data (default: /tmp/cluster-extract)
    INTELLIAIDE_SERVER_PATH          Override path to mcp_server.py (default: /app/skills/app/mcp_server.py)
    INTELLIAIDE_PYTHON               Override Python binary used to run mcp_server.py (default: sys.executable)
    INTELLIAIDE_SITE_PACKAGES        Override deps dir added to PYTHONPATH (default: /app/skills/app/intelliaide_deps)
    INTELLIAIDE_MCP_PORT             Override MCP port (default: 8001)
    INTELLIAIDE_POLL_INTERVAL        Override poll interval in seconds (default: 60)
    INTELLIAIDE_MAX_WAIT_MINUTES     Override maximum wait in minutes (default: 90)
"""

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Configuration — overridable via environment variables
# ---------------------------------------------------------------------------
# Path to the IntelliAide MCP server script inside the mounted skills image.
# The skills image is mounted at /app/skills/ in the sandbox container, so the
# image's /app/ directory appears at /app/skills/app/ from the container's view.
_SERVER_PATH = os.environ.get("INTELLIAIDE_SERVER_PATH", "/app/skills/app/mcp_server.py")

# Python interpreter used to spawn mcp_server.py.
# Both the skills image and the sandbox container are ubi9/python-312, so
# sys.executable is binary-compatible (same glibc, same Python version).
_SKILLS_PYTHON = os.environ.get("INTELLIAIDE_PYTHON", sys.executable)

# IntelliAide packages were installed with --target /app/intelliaide_deps in
# the Dockerfile (under /app/ which is writable for the non-root UBI9 user).
# When the skills image is volume-mounted at /app/skills/, that directory
# becomes /app/skills/app/intelliaide_deps in the sandbox container.
_SKILLS_SITE_PACKAGES = os.environ.get(
    "INTELLIAIDE_SITE_PACKAGES",
    "/app/skills/app/intelliaide_deps",
)

# Make IntelliAide packages (mcp, kubernetes, etc.) importable by THIS script too.
# run_rca.py runs with the sandbox Python which doesn't have these packages
# installed system-wide, so we add the skills image's deps to sys.path here.
if _SKILLS_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, _SKILLS_SITE_PACKAGES)

_MCP_PORT = int(os.environ.get("INTELLIAIDE_MCP_PORT", "8001"))
_MCP_URL = f"http://localhost:{_MCP_PORT}/mcp"
_POLL_INTERVAL = int(os.environ.get("INTELLIAIDE_POLL_INTERVAL", "60"))
_MAX_WAIT_MINUTES = int(os.environ.get("INTELLIAIDE_MAX_WAIT_MINUTES", "90"))
_SERVER_READY_TIMEOUT = 60  # seconds to wait for server port to open


# ---------------------------------------------------------------------------
# Server lifecycle helpers
# ---------------------------------------------------------------------------

def _start_server() -> subprocess.Popen:
    """Start the IntelliAide MCP server as a background process on localhost."""
    env = os.environ.copy()
    env["MCP_TRANSPORT"] = "streamable-http"
    env["MCP_PORT"] = str(_MCP_PORT)
    env["MCP_HOST"] = "127.0.0.1"  # loopback only — no external exposure
    env.setdefault("CLUSTER_EXTRACT_DIR", "/tmp/cluster-extract")

    # Ensure the skills Python can find its own site-packages regardless of how
    # Python resolves its prefix when invoked from an unusual path.
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        _SKILLS_SITE_PACKAGES + (":" + existing_pythonpath if existing_pythonpath else "")
    )

    os.makedirs(env["CLUSTER_EXTRACT_DIR"], exist_ok=True)

    proc = subprocess.Popen(
        [_SKILLS_PYTHON, _SERVER_PATH],
        cwd=os.path.dirname(_SERVER_PATH),  # /app/skills/app — needed for relative imports
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,  # surface errors to sandbox logs
    )
    print(f"[IntelliAide] Server process started (pid={proc.pid})", file=sys.stderr)
    print(f"[IntelliAide] Using Python: {_SKILLS_PYTHON}", file=sys.stderr)
    return proc


def _wait_for_port(host: str, port: int, timeout: int) -> bool:
    """Return True when port is accepting connections, False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(2)
    return False


def _stop_server(proc: subprocess.Popen) -> None:
    """Gracefully stop the server subprocess."""
    if proc.poll() is not None:
        return  # already exited
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    print("[IntelliAide] Server process stopped", file=sys.stderr)


# ---------------------------------------------------------------------------
# MCP client calls (each is a short-lived async session)
# ---------------------------------------------------------------------------

async def _call_tool(tool_name: str, args: dict) -> str:
    """Call one MCP tool and return the text content."""
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    async with streamablehttp_client(_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, args)
            return result.content[0].text


def _mcp(tool_name: str, args: dict) -> str:
    """Synchronous wrapper for _call_tool."""
    return asyncio.run(_call_tool(tool_name, args))


# ---------------------------------------------------------------------------
# Job submission and polling
# ---------------------------------------------------------------------------

def _submit_job(query: str) -> str:
    """Submit the RCA job and return the job_id."""
    print(f"[IntelliAide] Submitting RCA job: {query[:120]}", file=sys.stderr)
    raw = _mcp("run_rca", {"user_query": query})

    # Response is "job_id: <uuid>\nstate: queued"
    job_id = None
    for line in raw.strip().splitlines():
        if line.startswith("job_id:"):
            job_id = line.split(":", 1)[1].strip()
            break
    if not job_id:
        # Fallback: try JSON parse
        try:
            job_id = json.loads(raw).get("job_id", "")
        except (json.JSONDecodeError, AttributeError):
            pass

    if not job_id:
        raise RuntimeError(f"Failed to parse job_id from run_rca response: {raw!r}")

    print(f"[IntelliAide] Job submitted — job_id={job_id}", file=sys.stderr)
    return job_id


def _poll_until_done(job_id: str) -> None:
    """Block until job reaches a terminal state (completed / failed / cancelled)."""
    max_polls = (_MAX_WAIT_MINUTES * 60) // _POLL_INTERVAL
    for attempt in range(1, max_polls + 1):
        print(
            f"[IntelliAide] Polling status (attempt {attempt}/{max_polls}) "
            f"— waiting {_POLL_INTERVAL}s…",
            file=sys.stderr,
        )
        time.sleep(_POLL_INTERVAL)

        raw = _mcp("get_job_status", {"job_id": job_id})
        try:
            status = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[IntelliAide] Could not parse status response: {raw!r}", file=sys.stderr)
            continue

        state = status.get("state", "unknown")
        phase = status.get("phase", "")
        progress = status.get("progress", 0)
        current_pass = status.get("current_pass", 0)
        message = status.get("message", "")

        print(
            f"[IntelliAide] state={state} phase={phase} progress={progress}% "
            f"pass={current_pass}/3 — {message}",
            file=sys.stderr,
        )

        if state == "completed":
            print("[IntelliAide] RCA job completed.", file=sys.stderr)
            return
        if state in ("failed", "cancelled"):
            raise RuntimeError(
                f"IntelliAide RCA {state}: {message or status.get('error', 'no details')}"
            )

    raise RuntimeError(
        f"IntelliAide RCA job did not complete within {_MAX_WAIT_MINUTES} minutes "
        f"(job_id={job_id})"
    )


def _retrieve_result(job_id: str) -> dict:
    """Fetch the final structured result and flatten it for Claude."""
    print("[IntelliAide] Retrieving final RCA report…", file=sys.stderr)
    raw = _mcp("get_job_result", {"job_id": job_id})
    result = json.loads(raw)

    if "error" in result:
        raise RuntimeError(f"get_job_result error: {result['error']}")

    # Flatten rca_structured into top-level keys for easy CLAUDE.md mapping
    rca_structured = result.get("rca_structured") or {}
    flattened = {
        "job_id": job_id,
        "executive_summary":         rca_structured.get("executive_summary", ""),
        "chronology_of_events":      rca_structured.get("chronology_of_events", ""),
        "primary_root_causes":       rca_structured.get("primary_root_causes", ""),
        "secondary_causes":          rca_structured.get("secondary_causes", ""),
        "aggregated_error_patterns": rca_structured.get("aggregated_error_patterns", ""),
        "recommendations":           rca_structured.get("recommendations", ""),
        "evidence_files":            result.get("evidence_files", []),
        "total_cost_usd":            result.get("total_cost_usd") or result.get("cumulative_total_cost_usd") or 0.0,
        "rca_text":                  result.get("rca_text", ""),
    }

    evidence_count = len(flattened["evidence_files"])
    cost = flattened["total_cost_usd"]
    print(
        f"[IntelliAide] Report retrieved — {evidence_count} evidence files, "
        f"cost=${cost:.4f} USD",
        file=sys.stderr,
    )
    return flattened


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query",
        required=True,
        metavar="TEXT",
        help="Problem statement to analyse (e.g. 'etcd pods not ready in openshift-etcd').",
    )
    args = parser.parse_args()

    proc = None
    try:
        # 1. Spawn IntelliAide
        proc = _start_server()

        # 2. Wait for server to be ready
        print(
            f"[IntelliAide] Waiting up to {_SERVER_READY_TIMEOUT}s for server on "
            f"localhost:{_MCP_PORT}…",
            file=sys.stderr,
        )
        if not _wait_for_port("localhost", _MCP_PORT, _SERVER_READY_TIMEOUT):
            raise RuntimeError(
                f"IntelliAide server did not open port {_MCP_PORT} within "
                f"{_SERVER_READY_TIMEOUT}s. Check /app/mcp_server.py exists in the image."
            )
        print("[IntelliAide] Server is ready.", file=sys.stderr)

        # 3. Submit RCA job
        job_id = _submit_job(args.query)

        # 4. Poll until done (all polling is inside run_rca.py — Claude is not involved)
        _poll_until_done(job_id)

        # 5. Retrieve result
        result = _retrieve_result(job_id)

        # 6. Print JSON to stdout — Claude reads this and maps to the output schema
        print(json.dumps(result, indent=2))

    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)

    finally:
        # 7. Destroy IntelliAide subprocess
        if proc is not None:
            _stop_server(proc)


if __name__ == "__main__":
    main()
