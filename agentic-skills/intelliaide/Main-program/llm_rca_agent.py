# This file was copied from intelliaide-intermediate-deliverables/Main-program/llm_rca_agent.py
# See that file for full history. Copy performed during migration to agentic-skills repo.
# The content below is identical to the source with no modifications required
# because all paths use app_paths.py which auto-resolves based on __file__ location.

"""
LLM RCA Agent

Uses Claude LLM to aggregate, summarize, and perform root cause analysis (RCA)
on YAML analysis data returned by ML_YAML_CLASSIFICATION (Error-classified objects only).
Sends only YAML objects to the LLM (line numbers removed).
"""

import os
import re
import json
import subprocess
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import google.auth
    import google.auth.transport.requests
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False


def _get_gcloud_token() -> str:
    """Get OAuth2 access token for GCP Vertex AI authentication."""
    if GOOGLE_AUTH_AVAILABLE:
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        try:
            credentials, _ = google.auth.default(scopes=scopes)
            credentials.refresh(google.auth.transport.requests.Request())
        except Exception as e:
            err = str(e)
            low = err.lower()
            bad_key = (
                "type is none" in low or "expected one of" in low or "pem" in low
                or "invaliddata" in low or "unable to load" in low or "private key" in low
            )
            if bad_key:
                raise RuntimeError(
                    "Invalid GCP service account key at GOOGLE_APPLICATION_CREDENTIALS. "
                    f"Original error: {e}"
                ) from e
            raise RuntimeError(
                f"GCP/Vertex token failed. Original error: {e}"
            ) from e
        if not credentials.token:
            raise RuntimeError("google-auth returned empty token")
        return credentials.token

    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get gcloud access token: {result.stderr.strip()}")
    return result.stdout.strip()


def resolve_api_token(claude_config: dict) -> str:
    """Return the Bearer token: gcloud dynamic token or static api_key."""
    auth_type = claude_config.get("auth_type", "api_key")
    if auth_type == "gcloud":
        return _get_gcloud_token()
    return claude_config.get("api_key") or os.getenv("ANTHROPIC_API_KEY") or ""


def resolve_api_endpoint(claude_config: dict) -> str:
    """Build the full API endpoint URL from config."""
    api_url = claude_config.get("api_url", "").rstrip("/")
    model_id = claude_config.get("model_id", "claude-opus-4-6")
    endpoint_pattern = claude_config.get("endpoint_pattern")
    if endpoint_pattern:
        return endpoint_pattern.format(api_url=api_url, model_id=model_id)
    return f"{api_url}/v1/messages"


def vertex_unary_body_fields(claude_config: dict) -> Dict[str, Any]:
    """Vertex :streamRawPredict accepts stream=false for one-shot JSON."""
    pattern = claude_config.get("endpoint_pattern") or ""
    if ":streamRawPredict" in pattern:
        return {"stream": False}
    return {}


def _remove_line_numbers_from_structure(structure: Any) -> Any:
    """Remove all line number metadata from a structure (dict/list)."""
    if isinstance(structure, dict):
        result = {}
        for key, value in structure.items():
            if key == "_line_numbers" or (isinstance(key, str) and key.endswith("_line_numbers")):
                continue
            result[key] = _remove_line_numbers_from_structure(value)
        return result
    if isinstance(structure, list):
        return [_remove_line_numbers_from_structure(item) for item in structure]
    return structure


try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False


CHARS_PER_TOKEN: Dict[str, float] = {
    "claude":  3.5,
    "llama":   3.8,
    "gpt-4":   3.7,
    "gpt-3":   4.0,
    "mistral": 3.9,
}


def estimate_tokens(text: str, model_id: str = "") -> int:
    """Estimate token count using model-aware chars-per-token ratios."""
    if not text:
        return 0
    if TIKTOKEN_AVAILABLE:
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except Exception:
            pass
    model_lower = (model_id or "").lower()
    cpt = next((v for k, v in CHARS_PER_TOKEN.items() if k in model_lower), 3.7)
    return max(1, int(len(text) / cpt))


SUMMARY_MAX_CHARS = 80000


def _get_folder_path(file_path: str) -> str:
    """Extracts the folder path from a file string to group logical components."""
    if not file_path:
        return "unknown"
    p = Path(file_path)
    return str(p.parent) if len(p.parts) > 1 else "root"


def _split_large_log_entry_tokens(entry: Dict[str, Any], max_tokens: int) -> List[Dict[str, Any]]:
    """Split an oversized log entry into sub-entries at line boundaries based on tokens."""
    content = entry.get("content", "")
    if estimate_tokens(content) <= max_tokens:
        return [entry]

    lines = content.split("\n")
    parts: List[str] = []
    current_lines: List[str] = []
    current_tokens = 0

    for line in lines:
        line_toks = estimate_tokens(line + "\n")
        if current_tokens + line_toks > max_tokens and current_lines:
            parts.append("\n".join(current_lines))
            current_lines = [line]
            current_tokens = line_toks
        else:
            current_lines.append(line)
            current_tokens += line_toks

    if current_lines:
        parts.append("\n".join(current_lines))

    file_name = entry.get("file", "unknown")
    total = len(parts)
    return [
        {**entry, "file": f"{file_name} (part {i+1}/{total})", "content": part}
        for i, part in enumerate(parts)
    ]


def _apply_llm_env_overrides(config: Dict) -> None:
    """Apply environment variable overrides to the LLM config dict (in-place)."""
    claude = config.setdefault("claude", {})
    str_overrides = {
        "CLAUDE_AUTH_TYPE":       "auth_type",
        "CLAUDE_API_URL":         "api_url",
        "CLAUDE_MODEL_ID":        "model_id",
        "ANTHROPIC_API_KEY":      "api_key",
        "CLAUDE_ENDPOINT_PATTERN": "endpoint_pattern",
    }
    for env_var, key in str_overrides.items():
        val = os.getenv(env_var)
        if val is not None:
            claude[key] = val

    max_tokens_env = os.getenv("CLAUDE_MAX_TOKENS")
    if max_tokens_env is not None:
        try:
            claude["max_tokens"] = int(max_tokens_env)
        except ValueError:
            pass

    verify_ssl_env = os.getenv("CLAUDE_VERIFY_SSL")
    if verify_ssl_env is not None:
        claude["verify_ssl"] = verify_ssl_env.lower() not in ("0", "false", "no")

    custom_gw_env = os.getenv("CLAUDE_CUSTOM_GATEWAY")
    if custom_gw_env is not None:
        claude["custom_gateway"] = custom_gw_env.lower() not in ("0", "false", "no")

    for env_var, key in (
        ("CLAUDE_PRICE_INPUT",  "price_per_1m_input_tokens"),
        ("CLAUDE_PRICE_OUTPUT", "price_per_1m_output_tokens"),
    ):
        val = os.getenv(env_var)
        if val is not None:
            try:
                claude[key] = float(val)
            except ValueError:
                pass


def load_config(config_path: str = "config.json") -> Dict:
    """Load configuration from JSON file."""
    try:
        from app_paths import get_config_path
        if config_path == "config.json":
            path = get_config_path()
        else:
            path = Path(config_path)
    except ImportError:
        path = Path(__file__).parent / config_path
    default = {
        "claude": {
            "auth_type": "gcloud",
            "api_url": "https://aiplatform.googleapis.com",
            "model_id": "claude-opus-4-6",
            "max_tokens": 16384,
            "verify_ssl": True,
            "endpoint_pattern": "https://aiplatform.googleapis.com/v1/projects/itpc-gcp-hcm-pe-eng-claude/locations/global/publishers/anthropic/models/{model_id}:streamRawPredict",
            "custom_gateway": True,
            "price_per_1m_input_tokens": 3.0,
            "price_per_1m_output_tokens": 15.0,
        }
    }
    if not path.exists():
        _apply_llm_env_overrides(default)
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if "claude" in cfg:
            default["claude"].update(cfg["claude"])
        _apply_llm_env_overrides(default)
        return default
    except Exception:
        _apply_llm_env_overrides(default)
        return default


# ISO timestamp patterns
_ISO_TS = re.compile(r"(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)")
_LOG_TIME = re.compile(r'time="(\d{4}-\d{2}-\d{2}(?:T|\s)\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)"', re.IGNORECASE)
_LOG_TIME_CRIO = re.compile(r'time="(\d{4}-\d{2}-\d{2})\s+\.(\d+)Z"', re.IGNORECASE)
_LOG_TIME_DATE = re.compile(r'time="(\d{4}-\d{2}-\d{2})[\s.]', re.IGNORECASE)


def _parse_iso_like(ts_str: str) -> Optional[str]:
    """Normalize to sortable ISO-like string. Returns None if unparseable."""
    if not ts_str or len(ts_str) < 19:
        return None
    normalized = ts_str.replace(" ", "T", 1).strip()
    if "T" in normalized:
        date_part, rest = normalized.split("T", 1)
        rest = re.sub(r"[^\d:]", "", rest[:8])
        if len(rest) >= 6:
            return f"{date_part}T{rest[:2]}:{rest[2:4]}:{rest[4:6]}"
    return normalized[:19] if len(normalized) >= 19 else None


def _extract_timestamps_from_value(value: Any, key_path: str, out: List[Tuple[str, str, str]]) -> None:
    """Recursively extract timestamp keys and ISO values from YAML-like structure."""
    if isinstance(value, dict):
        for k, v in value.items():
            key_lower = k.lower() if isinstance(k, str) else ""
            path = f"{key_path}.{k}" if key_path else k
            if any(t in key_lower for t in ("timestamp", "time", "created", "updated", "transition", "eventtime", "observed")):
                if isinstance(v, str) and _ISO_TS.search(v):
                    sortable = _parse_iso_like(_ISO_TS.search(v).group(1))
                    if sortable:
                        out.append((sortable, path, v[:120]))
                else:
                    _extract_timestamps_from_value(v, path, out)
            else:
                _extract_timestamps_from_value(v, path, out)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _extract_timestamps_from_value(item, f"{key_path}[{i}]", out)
    elif isinstance(value, str) and key_path and _ISO_TS.search(value):
        sortable = _parse_iso_like(_ISO_TS.search(value).group(1))
        if sortable:
            out.append((sortable, key_path, value[:120]))


def build_chronology_from_payload(
    payload_yaml: Dict[str, List[Dict[str, Any]]],
    log_error_entries: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """Build a time-ordered chronology from YAML timestamps and log line timestamps."""
    events: List[Tuple[str, str, str]] = []

    for file_name, objects in (payload_yaml or {}).items():
        for obj in objects:
            cf = obj if isinstance(obj, dict) else (obj.get("critical_fields") or obj)
            _extract_timestamps_from_value(cf, f"YAML:{file_name}", events)

    for entry in (log_error_entries or []):
        content = entry.get("content") or ""
        file_name = entry.get("file") or "log"
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = _LOG_TIME.search(line)
            if m:
                raw = m.group(1)
                sortable = _parse_iso_like(raw.replace(" ", "T"))
                if sortable:
                    events.append((sortable, file_name, line[:150].replace("\n", " ")))
                continue
            m_crio = _LOG_TIME_CRIO.search(line)
            if m_crio:
                date_part = m_crio.group(1)
                frac = m_crio.group(2)[:9].ljust(9, "0")
                sortable = f"{date_part}T00:00:00.{frac}"
                events.append((sortable, file_name, line[:150].replace("\n", " ")))
                continue
            m2 = _LOG_TIME_DATE.search(line)
            if m2:
                sortable = _parse_iso_like(m2.group(1) + "T00:00:00")
                if sortable:
                    events.append((sortable, file_name, line[:150].replace("\n", " ")))
                continue
            iso = _ISO_TS.match(line)
            if iso:
                sortable = _parse_iso_like(iso.group(1))
                if sortable:
                    events.append((sortable, file_name, line[:150].replace("\n", " ")))

    seen = set()
    unique = []
    for sortable_ts, source, snippet in events:
        key = (sortable_ts, snippet[:80])
        if key in seen:
            continue
        seen.add(key)
        unique.append((sortable_ts, source, snippet))
    unique.sort(key=lambda x: x[0])

    return [{"timestamp": ts, "source": src, "snippet": snip} for ts, src, snip in unique]


def format_chronology_block(chronology: List[Dict[str, str]], max_events: int = 150) -> str:
    """Format chronology list as a clear text block for the prompt."""
    if not chronology:
        return ""
    if len(chronology) <= max_events:
        sampled = chronology
    else:
        half = max_events // 2
        sampled = chronology[:half] + chronology[-half:]
    lines = []
    for e in sampled:
        ts = e.get("timestamp", "")
        src = e.get("source", "")
        snip = (e.get("snippet") or "").replace("\n", " ").strip()[:120]
        lines.append(f"  {ts}  |  {src}  |  {snip}")
    return "\n".join(lines)


def prepare_payload_for_llm(ml_classification_result: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Prepare data for LLM: normalize input format, remove line numbers, keep only YAML objects."""
    payload = {}
    for file_name, file_data in (ml_classification_result or {}).items():
        if isinstance(file_data, dict) and "objects" in file_data:
            objects = file_data["objects"]
        elif isinstance(file_data, list):
            objects = file_data
        else:
            continue

        yaml_objects = []
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            raw_critical_fields = obj.get("critical_fields", {})
            cleaned = _remove_line_numbers_from_structure(raw_critical_fields)
            if cleaned:
                yaml_objects.append(cleaned)
        if yaml_objects:
            payload[file_name] = yaml_objects
    return payload


def build_rca_prompt(payload_for_llm: Dict[str, Any], problem_statement: Optional[str] = None) -> str:
    """Build the full RCA user-content prompt string from the payload."""
    chronology = payload_for_llm.get("chronology") or []
    chronology_block = format_chronology_block(chronology)
    payload_for_data = {k: v for k, v in payload_for_llm.items() if k != "chronology"}
    data_str = json.dumps(payload_for_data, indent=2, default=str)
    user_problem = (problem_statement or "").strip() or "Not specified"

    if chronology_block:
        chronology_section = f"""
2. CHRONOLOGY OF EVENTS (include in your ## Chronology of Events section):
{chronology_block}

3. AGGREGATED DATA (YAML/LOG/JSON error objects with file metadata):
"""
    else:
        chronology_section = """
2. AGGREGATED DATA (YAML/LOG/JSON error objects with file metadata):
"""

    return f"""You are an expert OpenShift/Kubernetes system analyst performing root cause analysis.

TASK: Identify the root cause(s) and state clearly: "The key cause for the user's problem is: [...]"

SOURCES:

1. USER REPORTED ISSUE (primary focus):
{user_problem}
{chronology_section}
{data_str}

INSTRUCTIONS:
- Correlate the user's issue with YAML error patterns and/or log error content.
- Identify PRIMARY root cause(s) with evidence, SECONDARY contributing factors.
- Prioritize root causes by relevance to the USER REPORTED ISSUE.
- Base analysis on actual evidence only — do not fabricate.

OUTPUT FORMAT — use these EXACT ## headings (frontend parses them programmatically):

## User Reported Issue
## Executive Summary
## Chronology of Events
## Primary Root Cause(s)
## Secondary Causes / Contributing Factors
## Aggregated Error Patterns
(MUST be a pipe-delimited markdown table:
| Pattern | Source | Classification | Significance |
|---------|--------|----------------|--------------|
| ... | ... | ... | ... |)
## Recommendations
(MANDATORY — always include. Numbered, actionable remediation steps.
 This section MUST appear last in the report.)

Do NOT rename headings. Do NOT use plain text for Aggregated Error Patterns."""


def call_claude_rca(
    payload_for_llm: Dict[str, Any],
    config_path: str = "config.json",
    problem_statement: Optional[str] = None,
    prior_chunk_summary: Optional[str] = None,
) -> Dict[str, Any]:
    """Call Claude LLM to perform root cause analysis on the payload."""
    err_result = lambda msg: {"text": msg, "input_tokens": 0, "output_tokens": 0}
    if not REQUESTS_AVAILABLE:
        return err_result("Error: requests package is required.")

    config = load_config(config_path)
    claude_config = config.get("claude", {})
    max_tokens = claude_config.get("max_tokens", 16384)
    verify_ssl = claude_config.get("verify_ssl", True)
    is_custom_gateway = claude_config.get("custom_gateway", False)

    try:
        api_token = resolve_api_token(claude_config)
    except RuntimeError as e:
        return err_result(f"Error: {e}")
    api_endpoint = resolve_api_endpoint(claude_config)

    if not api_token:
        return err_result("Error: No API token available.")

    user_content = build_rca_prompt(payload_for_llm, problem_statement)
    if prior_chunk_summary:
        user_content = (
            "PRIOR ANALYSIS CONTEXT:\n---\n"
            f"{prior_chunk_summary}\n---\n\n" + user_content
        )

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_token}"}

    if is_custom_gateway:
        payload = {
            "anthropic_version": "vertex-2023-10-16",
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_content}]}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        payload.update(vertex_unary_body_fields(claude_config))
    else:
        model_id = claude_config.get("model_id", "claude-opus-4-6")
        payload = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_content}],
        }

    try:
        response = requests.post(api_endpoint, headers=headers, json=payload, verify=verify_ssl, timeout=600)
        response.raise_for_status()
        response_json = response.json()

        usage = response_json.get("usage") or {}
        input_tokens = usage.get("input_tokens") or usage.get("input_tokens_count")
        output_tokens = usage.get("output_tokens") or usage.get("output_tokens_count")

        if is_custom_gateway:
            response_text = None
            if "content" in response_json:
                content = response_json["content"]
                if isinstance(content, list) and content:
                    text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and "text" in item]
                    response_text = "".join(text_parts) if text_parts else None
                elif isinstance(content, str):
                    response_text = content
            if not response_text and "text" in response_json:
                response_text = response_json["text"]
            if not response_text:
                response_text = json.dumps(response_json, indent=2)
        else:
            if "content" in response_json and response_json["content"]:
                response_text = response_json["content"][0].get("text", "")
            else:
                response_text = json.dumps(response_json, indent=2)

        response_text = response_text or "No response content from Claude."
        _mid = claude_config.get("model_id", "")
        if input_tokens is None:
            input_tokens = estimate_tokens(user_content, _mid)
        if output_tokens is None:
            output_tokens = estimate_tokens(response_text, _mid)
        return {"text": response_text, "input_tokens": input_tokens or 0, "output_tokens": output_tokens or 0}
    except requests.exceptions.RequestException as e:
        err = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                err += "\n" + json.dumps(e.response.json(), indent=2)
            except Exception:
                err += f"\nStatus: {e.response.status_code}\n{e.response.text[:500]}"
        return err_result(f"Claude API error: {err}")


def chunk_payload(
    yaml_errors: Dict[str, Any],
    log_entries: List[Dict[str, Any]],
    max_chunk_tokens: int = 80000
) -> List[Dict[str, Any]]:
    """Groups files logically by folder path, then builds chunks up to max_chunk_tokens."""
    all_items = []

    for fkey, objs in yaml_errors.items():
        all_items.append({"folder": _get_folder_path(fkey), "type": "yaml", "file": fkey, "payload": objs})

    for entry in log_entries:
        fkey = entry.get("file", "")
        all_items.append({"folder": _get_folder_path(fkey), "type": "log", "file": fkey, "payload": entry})

    grouped_items = {}
    for item in all_items:
        grouped_items.setdefault(item["folder"], []).append(item)

    chunks = []
    current_chunk = {"yaml_errors": {}, "log_entries": [], "_tokens": 0}

    def close_chunk():
        nonlocal current_chunk
        if current_chunk["yaml_errors"] or current_chunk["log_entries"]:
            clean_chunk = {"yaml_errors": current_chunk["yaml_errors"], "log_entries": current_chunk["log_entries"]}
            chunks.append(clean_chunk)
            current_chunk = {"yaml_errors": {}, "log_entries": [], "_tokens": 0}

    for folder, items in grouped_items.items():
        for item in items:
            item_str = json.dumps(item["payload"], indent=2, default=str)
            item_tokens = estimate_tokens(item_str)

            if item_tokens > max_chunk_tokens:
                if item["type"] == "log":
                    split_logs = _split_large_log_entry_tokens(item["payload"], max_chunk_tokens)
                    for sub_log in split_logs:
                        sub_toks = estimate_tokens(json.dumps(sub_log, indent=2, default=str))
                        if current_chunk["_tokens"] + sub_toks > max_chunk_tokens:
                            close_chunk()
                        current_chunk["log_entries"].append(sub_log)
                        current_chunk["_tokens"] += sub_toks
                elif item["type"] == "yaml":
                    if current_chunk["_tokens"] > 0:
                        close_chunk()
                    current_chunk["yaml_errors"][item["file"]] = item["payload"]
                    close_chunk()
                continue

            if current_chunk["_tokens"] + item_tokens > max_chunk_tokens:
                close_chunk()

            if item["type"] == "yaml":
                current_chunk["yaml_errors"][item["file"]] = item["payload"]
            else:
                current_chunk["log_entries"].append(item["payload"])
            current_chunk["_tokens"] += item_tokens

    close_chunk()
    print(f"[RCA Chunked] Produced {len(chunks)} contextual chunk(s) (Limit: {max_chunk_tokens:,} tokens)")
    return chunks


def _hierarchical_reduce(
    summary_files: List[Path],
    config_path: str,
    problem_statement: Optional[str],
    max_chunk_tokens: int,
    model_id: str,
    level: int = 1
) -> Tuple[str, int, int]:
    """Recursively reduces summary files."""
    from app_paths import get_results_dir

    if not summary_files:
        return "No data to reduce.", 0, 0

    summaries = []
    for fp in summary_files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                summaries.append(f.read())
        except Exception as e:
            print(f"[Reduce] Error reading {fp}: {e}")

    overhead = estimate_tokens(problem_statement or "", model_id) + 2000
    available_tokens = max_chunk_tokens - overhead

    batches = []
    current_batch = []
    current_tokens = 0

    for summ in summaries:
        toks = estimate_tokens(summ, model_id)
        if current_tokens + toks > available_tokens and current_batch:
            batches.append(current_batch)
            current_batch = [summ]
            current_tokens = toks
        else:
            current_batch.append(summ)
            current_tokens += toks

    if current_batch:
        batches.append(current_batch)

    if len(batches) == 1:
        res = call_claude_reduce_rca(batches[0], config_path, problem_statement)
        return res.get("text", ""), res.get("input_tokens", 0), res.get("output_tokens", 0)

    print(f"[RCA Reduce] Level {level} summaries exceed budget. Splitting into {len(batches)} batches.")

    next_level_files = []
    total_in = 0
    total_out = 0

    for i, batch in enumerate(batches):
        res = call_claude_reduce_rca(batch, config_path, problem_statement)
        text = res.get("text", "")
        total_in += res.get("input_tokens", 0)
        total_out += res.get("output_tokens", 0)

        out_path = get_results_dir() / f"reduce_level{level}_batch{i+1}.txt"
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(text)
            next_level_files.append(out_path)
        except Exception as e:
            print(f"[Reduce] Warning: Could not save intermediate batch: {e}")

    del summaries
    del batches

    final_text, sub_in, sub_out = _hierarchical_reduce(
        next_level_files, config_path, problem_statement, max_chunk_tokens, model_id, level + 1
    )
    return final_text, total_in + sub_in, total_out + sub_out


def call_claude_reduce_rca(
    chunk_summaries: List[str],
    config_path: str = "config.json",
    problem_statement: Optional[str] = None,
) -> Dict[str, Any]:
    """The 'Reduce' step: Synthesizes multiple chunk RCA findings into one final Master RCA."""
    err_result = lambda msg: {"text": msg, "input_tokens": 0, "output_tokens": 0}
    if not REQUESTS_AVAILABLE:
        return err_result("Error: requests package is required.")

    config = load_config(config_path)
    claude_config = config.get("claude", {})

    try:
        api_token = resolve_api_token(claude_config)
    except RuntimeError as e:
        return err_result(f"Error: {e}")

    api_endpoint = resolve_api_endpoint(claude_config)
    max_tokens = claude_config.get("max_tokens", 16384)
    verify_ssl = claude_config.get("verify_ssl", True)
    is_custom_gateway = claude_config.get("custom_gateway", False)

    user_problem = (problem_statement or "").strip() or "Not specified"

    combined_summaries = ""
    for idx, text in enumerate(chunk_summaries):
        combined_summaries += f"\n\n=== FINDINGS FROM CHUNK {idx+1} ===\n{text}\n"

    user_content = f"""You are an expert OpenShift/Kubernetes system analyst.
Synthesize all the chunk findings into one cohesive, final Master Root Cause Analysis report.

1. USER REPORTED ISSUE:
{user_problem}

2. PARTIAL FINDINGS FROM ALL CHUNKS:
{combined_summaries}

OUTPUT FORMAT — use these EXACT ## headings:
## User Reported Issue
## Executive Summary
## Chronology of Events
## Primary Root Cause(s)
## Secondary Causes / Contributing Factors
## Aggregated Error Patterns
(MUST be a pipe-delimited markdown table)
## Recommendations
(MANDATORY — always include. This section MUST appear last.)"""

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_token}"}

    if is_custom_gateway:
        payload = {
            "anthropic_version": "vertex-2023-10-16",
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_content}]}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        payload.update(vertex_unary_body_fields(claude_config))
    else:
        payload = {
            "model": claude_config.get("model_id", "claude-opus-4-6"),
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_content}],
        }

    try:
        response = requests.post(api_endpoint, headers=headers, json=payload, verify=verify_ssl, timeout=600)
        response.raise_for_status()
        response_json = response.json()

        usage = response_json.get("usage", {})
        input_tokens = usage.get("input_tokens") or usage.get("input_tokens_count", 0)
        output_tokens = usage.get("output_tokens") or usage.get("output_tokens_count", 0)

        response_text = None
        if "content" in response_json and isinstance(response_json["content"], list):
            response_text = "".join(item.get("text", "") for item in response_json["content"] if isinstance(item, dict) and "text" in item)
        if not response_text and "text" in response_json:
            response_text = response_json["text"]

        _mid = claude_config.get("model_id", "")
        if not input_tokens:
            input_tokens = estimate_tokens(user_content, _mid)
        if not output_tokens:
            output_tokens = estimate_tokens(response_text or "", _mid)

        return {"text": response_text or "No text returned.", "input_tokens": input_tokens, "output_tokens": output_tokens}
    except Exception as e:
        return err_result(f"Claude API error during final reduce: {str(e)}")


def run_rca_and_summary(
    ml_classification_result: Dict[str, List[Dict]],
    config_path: str = "config.json",
    problem_statement: Optional[str] = None,
    log_error_entries: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run initial RCA via file-based Map-Reduce."""
    return run_rca_chunked(
        ml_classification_result,
        config_path=config_path,
        problem_statement=problem_statement,
        log_error_entries=log_error_entries,
    )


def run_rca_chunked(
    ml_classification_result: Dict[str, List[Dict]],
    config_path: str = "config.json",
    problem_statement: Optional[str] = None,
    log_error_entries: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Process RCA payload via Map-Reduce chunking (Memory Optimized)."""
    from app_paths import get_results_dir

    payload_yaml = prepare_payload_for_llm(ml_classification_result)
    config = load_config(config_path)
    claude_config = config.get("claude", {})
    max_chunk_tokens = claude_config.get("max_chunk_tokens", 60000)
    model_id = claude_config.get("model_id", "claude-opus-4-6")

    chunks = chunk_payload(
        yaml_errors=payload_yaml,
        log_entries=log_error_entries or [],
        max_chunk_tokens=max_chunk_tokens
    )

    print(f"[RCA Chunked] Payload split into {len(chunks)} chunk(s). Starting Map-Reduce.")

    total_input_tokens = 0
    total_output_tokens = 0
    total_payload_bytes = 0
    all_chronology: List[Dict[str, str]] = []

    chunk_files: List[Path] = []
    chunks_succeeded = 0
    error = None

    for i, chunk_data in enumerate(chunks):
        chunk_yaml = chunk_data["yaml_errors"]
        chunk_logs = chunk_data["log_entries"]

        chunk_llm_payload: Dict[str, Any] = {"yaml_errors": chunk_yaml}
        if chunk_logs:
            chunk_llm_payload["log_errors"] = chunk_logs

        chronology = build_chronology_from_payload(
            chunk_yaml if isinstance(chunk_yaml, dict) else {}, chunk_logs,
        )
        if chronology:
            chunk_llm_payload["chronology"] = chronology
            all_chronology.extend(chronology)

        payload_bytes = len(json.dumps(chunk_llm_payload, default=str).encode("utf-8"))
        total_payload_bytes += payload_bytes

        rca_response = call_claude_rca(
            chunk_llm_payload,
            config_path=config_path,
            problem_statement=problem_statement,
        )

        rca_text = rca_response.get("text", "")
        total_input_tokens += rca_response.get("input_tokens", 0)
        total_output_tokens += rca_response.get("output_tokens", 0)

        if rca_text.startswith("Error:") or rca_text.startswith("Claude API error:"):
            error = rca_text
            print(f"[RCA Chunked] WARNING: Chunk {i+1} failed: {rca_text[:200]}")
            continue

        chunks_succeeded += 1

        try:
            chunk_file_path = get_results_dir() / f"map_chunk_{i+1}_rca.txt"
            with open(chunk_file_path, "w", encoding="utf-8") as f:
                f.write(f"--- Chunk {i+1} Findings ---\n\n{rca_text}")
            chunk_files.append(chunk_file_path)
            print(f"[RCA Chunked] Chunk {i+1}/{len(chunks)} complete.")
        except Exception as e:
            print(f"[RCA Chunked] Warning: Could not save chunk to disk: {e}")

    if chunks_succeeded == 0:
        final_rca_text = error or "Analysis failed completely."
    elif chunks_succeeded == 1:
        print("[RCA Chunked] Only 1 chunk processed. Skipping reduce step.")
        with open(chunk_files[0], "r", encoding="utf-8") as f:
            final_rca_text = f.read().replace("--- Chunk 1 Findings ---\n\n", "")
    else:
        print(f"[RCA Chunked] Initiating hierarchical reduce on {chunks_succeeded} files...")
        final_rca_text, reduce_in, reduce_out = _hierarchical_reduce(
            chunk_files, config_path, problem_statement, max_chunk_tokens, model_id
        )
        total_input_tokens += reduce_in
        total_output_tokens += reduce_out

        if final_rca_text.startswith("Error:") or final_rca_text.startswith("Claude API error:"):
            error = final_rca_text
            final_rca_text = "Master synthesis failed. Review intermediate files in Results/."

    all_chronology.sort(key=lambda x: x.get("timestamp", ""))

    price_in = float(claude_config.get("price_per_1m_input_tokens", 3.0))
    price_out = float(claude_config.get("price_per_1m_output_tokens", 15.0))
    cost_usd = ((total_input_tokens / 1_000_000 * price_in) + (total_output_tokens / 1_000_000 * price_out))

    return {
        "payload_sent": {}, "rca_summary": final_rca_text, "chronology": all_chronology,
        "payload_bytes": total_payload_bytes, "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens, "cost_usd": round(cost_usd, 6),
        "chunks_processed": len(chunks), "chunks_succeeded": chunks_succeeded, "error": error,
    }
