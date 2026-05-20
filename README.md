# IntelliAide Agentic Skills

Pure-skills packaging of IntelliAide for the
[lightspeed-agentic-operator](https://github.com/openshift/lightspeed-agentic-operator).

## What this is

IntelliAide is an RCA (Root Cause Analysis) tool for OpenShift clusters.
This repository packages it as **agentic skills** — self-contained Python scripts
that the operator's Claude Code sandbox calls directly.  There is no MCP server,
no FastAPI process, and no external service dependency.

All LLM reasoning is performed by the **orchestrating agent** (Lightspeed's Claude,
managed by the operator's `LLMProvider` CR).  The Python skill scripts handle only
pure computation — data extraction, ML classification, chunking, and prompt building.
No script makes an independent LLM call or requires external credentials.

## Architecture

```
Operator creates Proposal CR
        │
        ▼
Operator launches analysis sandbox pod (Claude Code + skills image mounted)
        │
        ▼
Claude reads /app/skills/intelliaide/SKILL.md
        │
        ├─ Step 1 → python /app/skills/intelliaide/extract_cluster.py
        │           (live cluster extraction via kubernetes Python client)
        │
        ├─ Step 2 → python /app/skills/intelliaide/select_files.py
        │           (writes file-selection prompt; Claude reasons over it)
        │
        ├─ Step 3 → python /app/skills/intelliaide/analyze_data.py
        │           (ML-based YAML + log classification via Drain3)
        │
        └─ Step 4 → python /app/skills/intelliaide/perform_rca.py
                    (writes RCA prompt chunks; Claude reasons over each chunk)
```

## Skills image layout

```
/intelliaide/                      ← image root; mounted at /app/skills/ in sandbox
  SKILL.md                         Orchestration instructions for Claude Code
  extract_cluster.py               Step 1: live cluster data extraction
  select_files.py                  Step 2: file priority selection (prompt builder)
  analyze_data.py                  Step 3: ML YAML + log analysis
  perform_rca.py                   Step 4: RCA chunk builder
  app_paths.py                     Path helpers (auto-resolves relative to __file__)
  requirements.txt
  vendor/                          Vendored Python packages (built by podman build)
  Main-program/                    IntelliAide core engine (llm_rca_agent, data_analyzer, …)
  Machine-learning/                Drain3 ML classifiers
  Config/                          config.json (optional tuning), yaml_processing.yaml, …
  DataSource/                      MUST_GATHER_*.md topology documentation
```

## Building the image

```bash
# 1. Build (vendoring happens inside the build)
podman build --no-cache -f Containerfile \
  -t quay.io/<your-org>/lightspeed-skills:latest .

# 2. Push
podman push quay.io/<your-org>/lightspeed-skills:latest
```

## Configuration

`intelliaide/Config/config.json` is **not committed** (see `.gitignore`).
Copy `config.json.template` to `config.json` if you want to tune chunking behaviour:

| Field | Description |
|---|---|
| `claude.max_chunk_tokens` | RCA chunking budget per chunk (default 80000) |
| `agent.max_iterations` | Max tool-call iterations for the ReAct loop (default 25) |
| `agent.max_deepening_rounds` | Max HIGH→MEDIUM→LOW deepening rounds (default 3) |

No credentials, API keys, or endpoint URLs are read from `config.json` — the
orchestrating LLM is configured entirely via the operator's `LLMProvider` CR.

## Python dependencies

All dependencies are vendored into `/intelliaide/vendor/` at build time
(see `Containerfile`).  The skill scripts add `vendor/` to `sys.path` at
startup so they work under the sandbox's Python interpreter.

Key packages: `kubernetes`, `PyYAML`, `drain3`, `odfpy`, `python-docx`
