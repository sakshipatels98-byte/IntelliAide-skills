#!/usr/bin/env python3
"""Start an IntelliAide RCA job on the live cluster.

Calls the IntelliAide MCP server's run_rca tool with the given problem
statement and prints a JSON object containing the job_id.

The RCA job runs asynchronously in IntelliAide. Use get_rca_status.py to
poll progress and get_rca_result.py to retrieve the final report.

Usage:
    python run_rca.py --query "etcd pods not ready in openshift-etcd"

Output (stdout):
    {"job_id": "<uuid>", "state": "queued"}

Environment:
    INTELLIAIDE_MCP_URL   Override the IntelliAide MCP endpoint
                          (default: in-cluster Service URL)
"""

import argparse
import asyncio
import json
import os
import sys

# ---------------------------------------------------------------------------
# IntelliAide MCP server URL — in-cluster Service DNS by default
# ---------------------------------------------------------------------------
MCP_URL = os.environ.get(
    "INTELLIAIDE_MCP_URL",
    "http://intelliade-mcp-server.intelliade-mcp-server.svc:8001/mcp",
)


async def _call_run_rca(user_query: str) -> str:
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("run_rca", {"user_query": user_query})
            return result.content[0].text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query",
        required=True,
        metavar="TEXT",
        help="Problem statement to analyse (passed to IntelliAide as user_query).",
    )
    args = parser.parse_args()

    print(f"[IntelliAide] Starting RCA for: {args.query}", file=sys.stderr)
    try:
        raw = asyncio.run(_call_run_rca(args.query))
        if raw.startswith("{"):
            parsed = json.loads(raw)
            print(raw)
        else:
            lines = dict(
                line.split(": ", 1) for line in raw.strip().splitlines() if ": " in line
            )
            parsed = {"job_id": lines.get("job_id", ""), "state": lines.get("state", "queued")}
            print(json.dumps(parsed))
        print(f"[IntelliAide] RCA job created: job_id={parsed.get('job_id','?')} state={parsed.get('state','?')}", file=sys.stderr)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
