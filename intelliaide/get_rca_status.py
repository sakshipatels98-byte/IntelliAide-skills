#!/usr/bin/env python3
"""Poll the status of a running IntelliAide RCA job.

Calls the IntelliAide MCP server's get_job_status tool and prints the
current job state as JSON.

Usage:
    python get_rca_status.py --job-id <job_id>

Output (stdout):
    {
      "job_id": "...",
      "state": "running",      // queued | running | completed | failed | cancelled
      "phase": "rca_analysis",
      "progress": 85,          // 0-100
      "current_pass": 2,       // 1=High  2=Medium  3=Low priority pass
      "message": "Running LLM analysis pass 2/3"
    }

Poll every 2 minutes until state == "completed".
Stop polling on state "failed" or "cancelled".

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


async def _call_get_status(job_id: str) -> str:
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_job_status", {"job_id": job_id})
            return result.content[0].text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--job-id",
        required=True,
        metavar="ID",
        help="Job ID returned by run_rca.py.",
    )
    args = parser.parse_args()

    try:
        raw = asyncio.run(_call_get_status(args.job_id))
        print(raw)
        try:
            status = json.loads(raw)
            print(
                f"[IntelliAide] state={status.get('state','?')} "
                f"phase={status.get('phase','?')} "
                f"progress={status.get('progress','?')}% "
                f"pass={status.get('current_pass','?')}/3 "
                f"— {status.get('message','')}", file=sys.stderr
            )
        except (json.JSONDecodeError, TypeError):
            pass
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
