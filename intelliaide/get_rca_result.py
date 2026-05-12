#!/usr/bin/env python3
"""Fetch the full RCA report for a completed IntelliAide job.

Calls the IntelliAide MCP server's get_job_result tool. Only call this
after get_rca_status.py reports state == "completed".

Usage:
    python get_rca_result.py --job-id <job_id>

Output (stdout) — JSON with these key fields:
    {
      "job_id": "...",
      "session_id": "...",
      "rca_text": "## Executive Summary\\n...",   // full Markdown report
      "rca_structured": {
        "user_reported_issue":       "...",
        "executive_summary":         "...",
        "chronology_of_events":      "...",
        "primary_root_causes":       "...",        // root causes Markdown
        "secondary_causes":          "...",
        "aggregated_error_patterns": "...",
        "recommendations":           "..."         // recommended actions Markdown
      },
      "pass_results": [...],                       // per-pass summaries
      "evidence_files": ["..."],                   // cluster files analysed
      "total_cost_usd": 0.12,
      "cumulative_total_cost_usd": 0.12
    }

Map rca_structured fields to RemediationOptions using CLAUDE.md as guide.

Environment:
    INTELLIAIDE_MCP_URL   Override the IntelliAide MCP endpoint
                          (default: in-cluster Service URL)
"""

import argparse
import asyncio
import json
import os
import sys

MCP_URL = os.environ.get(
    "INTELLIAIDE_MCP_URL",
    "http://intelliade-mcp-server.intelliade-mcp-server.svc:8001/mcp",
)


async def _call_get_result(job_id: str) -> str:
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_job_result", {"job_id": job_id})
            return result.content[0].text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--job-id",
        required=True,
        metavar="ID",
        help="Job ID returned by run_rca.py (must be in completed state).",
    )
    args = parser.parse_args()

    print(f"[IntelliAide] Retrieving RCA report for job: {args.job_id}", file=sys.stderr)
    try:
        raw = asyncio.run(_call_get_result(args.job_id))
        parsed = json.loads(raw)
        print(json.dumps(parsed, ensure_ascii=False))
        ev_count = len(parsed.get("evidence_files", []))
        cost = parsed.get("total_cost_usd", 0)
        has_rca = "rca_structured" in parsed
        print(f"[IntelliAide] Report retrieved: evidence_files={ev_count} cost=${cost:.4f} structured={has_rca}", file=sys.stderr)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
