#!/usr/bin/env python3
"""Cancel a running or queued IntelliAide RCA job.

Calls the IntelliAide MCP server's cancel_job tool. Use when a job is taking
too long or is no longer needed.

Usage:
    python cancel_rca.py --job-id <job_id>

Output (stdout):
    {"job_id": "...", "state": "cancelling"}   // or "cancelled" / "not_found"

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


async def _call_cancel(job_id: str) -> str:
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("cancel_job", {"job_id": job_id})
            return result.content[0].text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--job-id",
        required=True,
        metavar="ID",
        help="Job ID to cancel.",
    )
    args = parser.parse_args()

    try:
        raw = asyncio.run(_call_cancel(args.job_id))
        print(raw)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
