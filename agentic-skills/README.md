# IntelliAide Agentic Skills

Pure-skills packaging of [IntelliAide](https://gitlab.cee.redhat.com/intelliaide-debug/intelliaide-intermediate-deliverables)
for the [lightspeed-agentic-operator](https://github.com/openshift/lightspeed-agentic-operator).

## What this is

IntelliAide is an RCA (Root Cause Analysis) tool for OpenShift clusters.
This repository packages it as **agentic skills** — self-contained Python scripts
that the operator's Claude Code sandbox calls directly.  There is no MCP server,
no FastAPI process, and no external service dependency.

## Architecture

```
Operator creates Proposal CR
        │
        ▼
Operator launches analysis sandbox pod (Claude Code + skills image mounted)
        │
        ▼
Claude reads /app/skills/intelliaide/CLAUDE.md
        │
        ├─ Step 1 → python /app/skills/intelliaide/extract_cluster.py
        │           (live cluster extraction via kubernetes Python client)
        │
        ├─ Step 2 → python /app/skills/intelliaide/select_files.py
        │           (LLM-assisted file priority selection)
        │
        ├─ Step 3 → python /app/skills/intelliaide/analyze_data.py
        │           (ML-based YAML + log classification via Drain3)
        │
        └─ Step 4 → python /app/skills/intelliaide/perform_rca.py
                    (LLM-based root cause analysis via Claude on Vertex AI)
```

## Skills image layout

```
/intelliaide/                      ← image root; mounted at /app/skills/ in sandbox
  CLAUDE.md                        Orchestration instructions for Claude Code
  extract_cluster.py               Step 1: live cluster data extraction
  select_files.py                  Step 2: file priority selection
  analyze_data.py                  Step 3: ML YAML + log analysis
  perform_rca.py                   Step 4: LLM-based RCA
  app_paths.py                     Path helpers (auto-resolves relative to __file__)
  requirements.txt
  vendor/                          Vendored Python packages (built by podman build)
  Main-program/                    IntelliAide core engine (llm_rca_agent, data_analyzer, …)
  python-client/                   Kubernetes live-extraction helpers
  Machine-learning/                Drain3 ML classifiers
  Config/                          config.json (from secret), yaml_processing.yaml, …
  DataSource/                      MUST_GATHER_*.md topology documentation
```

## Building the image

```bash
# 1. Copy your GCP config
cp intelliaide/Config/config.json.template intelliaide/Config/config.json
#    Edit config.json: replace YOUR_GCP_PROJECT_ID, set auth_type, model_id, etc.

# 2. Build (vendoring happens inside the build)
podman build --no-cache -f Containerfile \
  -t quay.io/<your-org>/lightspeed-skills:latest .

# 3. Push
podman push quay.io/<your-org>/lightspeed-skills:latest
```

## Configuration

`intelliaide/Config/config.json` is **not committed** (see `.gitignore`).  
Copy `config.json.template` to `config.json` and fill in your values:

| Field | Description |
|---|---|
| `claude.auth_type` | `gcloud` (Vertex AI via ADC) or `api_key` |
| `claude.endpoint_pattern` | Full Vertex AI endpoint URL with your GCP project ID |
| `claude.model_id` | e.g. `claude-opus-4-6` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account key (set as env var in sandbox) |

Environment variable overrides (set by the operator in the sandbox pod):

| Variable | Purpose |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP service account key for Vertex AI |
| `CLAUDE_AUTH_TYPE` | Override `auth_type` in config.json |
| `CLAUDE_ENDPOINT_PATTERN` | Override endpoint URL |
| `ANTHROPIC_API_KEY` | Use direct Anthropic key instead of Vertex |

## Python dependencies

All dependencies are vendored into `/intelliaide/vendor/` at build time
(see `Containerfile`).  The skill scripts add `vendor/` to `sys.path` at
startup so they work under the sandbox's Python interpreter.

Key packages: `kubernetes`, `google-auth`, `requests`, `PyYAML`, `drain3`, `odfpy`, `python-docx`

## Two LLM calls per run

Note that each run involves **two separate Claude API calls**:

1. **Orchestrator** — the operator's `LLMProvider` CR (e.g. Vertex AI via `llm-credentials` secret).
   Claude Code reads `CLAUDE.md` and calls the four skill scripts.

2. **IntelliAide RCA** — IntelliAide's own Claude call (configured via `Config/config.json`).
   `perform_rca.py` sends the analyzed cluster data to Claude for the actual RCA report.

Both can point to the same GCP project / Vertex AI endpoint.
