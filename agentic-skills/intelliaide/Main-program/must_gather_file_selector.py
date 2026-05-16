# Copied from intelliaide-intermediate-deliverables/Main-program/must_gather_file_selector.py
# No path changes needed — app_paths.py auto-resolves all paths based on __file__ location.

"""
Must-Gather File Selector Module

Analyzes user problem statements and suggests which files from a must-gather
collection need to be analyzed based on the must-gather structure documentation.

Uses an LLM to intelligently route problem statements to relevant files.
"""

import os
import json
import re
import time
from typing import Dict, List, Optional
from pathlib import Path


try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    print("Warning: requests package is required. Install with: pip install requests urllib3")

try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False
    ollama = None

CONFIG_FILE = "config-geminiflash.json"


def load_config_by_name(config_file_name: Optional[str] = None) -> Dict:
    """Resolve config file by name, load it, and return the full config dict."""
    if config_file_name is None:
        config_file_name = CONFIG_FILE
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if os.path.isabs(config_file_name) and os.path.isfile(config_file_name):
        candidates.append(config_file_name)
    else:
        try:
            from app_paths import get_config_dir
            candidates.append(str(get_config_dir() / config_file_name))
        except ImportError:
            pass
        candidates.append(os.path.join(script_dir, config_file_name))
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                raw = f.read()
            text = raw.decode("utf-8-sig").strip()
            if not text:
                continue
            return json.loads(text)
        except (json.JSONDecodeError, Exception):
            continue
    print(f"Warning: Could not load config from {config_file_name}.")
    return {}


def load_config(config_path: Optional[str] = None) -> Dict:
    """Load configuration. Delegates to load_config_by_name."""
    return load_config_by_name(config_path)


def _resolve_must_gather_docs_dir() -> str:
    """Resolve the must-gather docs directory from config.json, app_paths, or fallback."""
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _project_root = os.path.dirname(_script_dir)

    try:
        from app_paths import get_config_path
        cfg_path = str(get_config_path())
    except ImportError:
        cfg_path = os.path.join(_project_root, "Config", "config.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            configured = cfg.get("must_gather_docs_dir", "")
            if configured:
                if not os.path.isabs(configured):
                    configured = os.path.join(_project_root, configured)
                if os.path.isdir(configured):
                    return configured
        except Exception:
            pass

    try:
        from app_paths import get_must_gather_docs_dir as _get_docs_dir
        docs_dir = str(_get_docs_dir())
        if os.path.isdir(docs_dir):
            return docs_dir
    except ImportError:
        pass

    return os.path.join(_project_root, "DataSource")


MUST_GATHER_DOCS_DIR_DEFAULT = _resolve_must_gather_docs_dir()


def load_must_gather_documentation(must_gather_docs_dir: str) -> Dict[str, str]:
    """Load all must-gather structure documentation files."""
    docs = {}
    doc_files = [
        'MUST_GATHER_STRUCTURE.md',
        'MUST_GATHER_INDEX.md',
        'MUST_GATHER_ROUTING_GUIDE.md',
        'MUST_GATHER_DOCUMENTATION_README.md',
    ]
    docs_path = Path(must_gather_docs_dir)
    if not docs_path.exists():
        raise FileNotFoundError(f"Must-gather documentation directory not found: {must_gather_docs_dir}")
    for doc_file in doc_files:
        doc_path = docs_path / doc_file
        if doc_path.exists():
            try:
                with open(doc_path, 'r', encoding='utf-8') as f:
                    docs[doc_file] = f.read()
            except Exception as e:
                print(f"Warning: Could not read {doc_file}: {e}")
        else:
            print(f"Warning: Documentation file not found: {doc_file}")
    return docs


class MustGatherFileSelector:
    """
    Selects relevant must-gather files based on user problem statements.
    Uses the LLM configured in the selected config file.
    """

    def __init__(self, api_key: Optional[str] = None, api_url: Optional[str] = None,
                 model_id: Optional[str] = None, max_tokens: Optional[int] = None,
                 config_path: Optional[str] = None):
        if config_path is None:
            config_path = CONFIG_FILE
        config = load_config(config_path)
        llm_config = next(
            (v for k, v in config.items() if isinstance(v, dict) and ('api_key' in v or 'model_id' in v or 'provider' in v)),
            {}
        )
        self._llm_config = llm_config
        self._use_ollama = llm_config.get('provider') == 'ollama' or llm_config.get('use_ollama', False)
        if self._use_ollama:
            if not OLLAMA_AVAILABLE:
                raise ImportError("Ollama provider requires the ollama package.")
            self.model_id = model_id or llm_config.get('model_id', 'llama8B')
            self.ollama_model = llm_config.get('ollama_model', 'llama3.1')
            self.max_tokens = max_tokens if max_tokens is not None else llm_config.get('max_tokens', 4096)
            opts = llm_config.get('ollama_options', {})
            self._ollama_options = {
                'temperature': opts.get('temperature', 0.0),
                'num_ctx': opts.get('num_ctx', 8192),
                'top_p': opts.get('top_p', 0.9),
                'repeat_penalty': opts.get('repeat_penalty', 1.2),
                'seed': opts.get('seed', 42),
            }
            self.api_key = self.api_url = None
            self.api_endpoint = None
            self._is_custom_gateway = self._is_openai_compatible = self._is_completions_api = False
            self.verify_ssl = True
            self._auth_header = None
            return
        if not REQUESTS_AVAILABLE:
            raise ImportError("requests package is required.")
        self._auth_type = llm_config.get('auth_type', 'api_key')
        if self._auth_type == 'gcloud':
            try:
                from llm_rca_agent import _get_gcloud_token
                self.api_key = _get_gcloud_token()
            except Exception as e:
                raise RuntimeError(f"Failed to get gcloud access token: {e}") from e
        else:
            api_key_env = llm_config.get('api_key_env', 'LLM_API_KEY')
            self.api_key = (
                api_key or llm_config.get('api_key')
                or os.getenv(api_key_env) or os.getenv('ANTHROPIC_API_KEY') or os.getenv('USER_KEY')
            )
            if not self.api_key:
                raise ValueError(f"API key is required. Set it in the config file or environment variable ({api_key_env}).")
        self.api_url = (api_url or llm_config.get('api_url') or '').strip().rstrip('/')
        self.model_id = model_id or llm_config.get('model_id')
        self.max_tokens = max_tokens if max_tokens is not None else llm_config.get('max_tokens')
        if not self.api_url:
            raise ValueError("'api_url' must be set in the config file or passed as argument.")
        if not self.model_id:
            raise ValueError("'model_id' must be set in the config file or passed as argument.")
        if self.max_tokens is None:
            raise ValueError("'max_tokens' must be set in the config file or passed as argument.")
        endpoint_pattern = llm_config.get('endpoint_pattern')
        api_ver = llm_config.get('api_version', '')
        is_openai_compatible = False
        if endpoint_pattern:
            self.api_endpoint = endpoint_pattern.format(api_url=self.api_url, model_id=self.model_id)
            is_custom_gateway = True
        else:
            path_template = llm_config.get('path_template')
            if path_template:
                self.api_endpoint = path_template.format(api_url=self.api_url, model_id=self.model_id)
                is_custom_gateway = True
            else:
                if api_ver == 'v1beta':
                    self.api_endpoint = f"{self.api_url}/v1beta/openai/chat/completions"
                    is_custom_gateway = True
                    is_openai_compatible = True
                else:
                    self.api_endpoint = f"{self.api_url}/v1/messages"
                    is_custom_gateway = False
        if 'custom_gateway' in llm_config:
            is_custom_gateway = bool(llm_config['custom_gateway'])
        self._is_custom_gateway = is_custom_gateway
        self._is_openai_compatible = is_openai_compatible
        self._is_completions_api = '/v1/completions' in self.api_endpoint
        self._auth_header = llm_config.get('auth_header', '').strip() or None
        verify_ssl_env = os.getenv("VERIFY_SSL", "").lower()
        if verify_ssl_env == "true":
            self.verify_ssl = True
        elif verify_ssl_env == "false":
            self.verify_ssl = False
        else:
            self.verify_ssl = llm_config.get('verify_ssl', True)
        self._request_timeout = llm_config.get('request_timeout', 300)
        self._temperature = llm_config.get('temperature', 0)

    def _call_llm_api(self, prompt: str):
        print(f"Calling LLM (model: {self.model_id})", flush=True)
        if self._use_ollama:
            num_predict = self.max_tokens if self.max_tokens else 8192
            options = {**self._ollama_options, 'num_predict': num_predict}
            response = ollama.chat(
                model=self.ollama_model,
                messages=[{"role": "user", "content": prompt}],
                options=options,
            )
            msg = response.get("message") or {}
            text = msg.get("content") or ""
            eval_count = response.get("eval_count") or 0
            return (text.strip(), {"input_tokens": 0, "output_tokens": eval_count})
        if self._auth_type == 'gcloud':
            try:
                from llm_rca_agent import _get_gcloud_token
                self.api_key = _get_gcloud_token()
            except Exception as e:
                raise RuntimeError(f"Failed to refresh gcloud access token: {e}") from e
        if self._auth_header:
            headers = {"Content-Type": "application/json", self._auth_header: self.api_key}
        else:
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        api_version = self._llm_config.get('anthropic_version', self._llm_config.get('api_version', 'vertex-2023-10-16'))
        if self._is_completions_api:
            payload = {"model": self.model_id, "prompt": prompt, "max_tokens": self.max_tokens, "temperature": self._temperature}
        elif self._is_openai_compatible:
            payload = {"model": self.model_id, "max_tokens": self.max_tokens, "messages": [{"role": "user", "content": prompt}], "temperature": self._temperature}
        elif self._is_custom_gateway:
            payload = {
                "anthropic_version": api_version,
                "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                "max_tokens": self.max_tokens,
                "temperature": self._temperature,
            }
            try:
                from llm_rca_agent import vertex_unary_body_fields
                payload.update(vertex_unary_body_fields(self._llm_config))
            except ImportError:
                pass
        else:
            payload = {"model": self.model_id, "max_tokens": self.max_tokens, "messages": [{"role": "user", "content": prompt}], "temperature": self._temperature}
        max_tries = 5
        last_response = None
        try:
            for attempt in range(max_tries):
                try:
                    response = requests.post(
                        self.api_endpoint, headers=headers, json=payload,
                        verify=self.verify_ssl, timeout=self._request_timeout
                    )
                except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout) as e:
                    if attempt < max_tries - 1:
                        wait_sec = 2 ** attempt
                        print(f"  Read timeout, retry in {wait_sec}s (attempt {attempt + 1}/{max_tries})...", flush=True)
                        time.sleep(wait_sec)
                        continue
                    raise RuntimeError(f"LLM API read timeout after {max_tries} attempts: {e}") from e
                last_response = response
                if response.status_code == 429:
                    if attempt < max_tries - 1:
                        wait_sec = 2 ** attempt
                        time.sleep(wait_sec)
                        continue
                response.raise_for_status()
                response_json = response.json()
                usage = response_json.get("usage") or {}
                input_tokens = usage.get("input_tokens") or usage.get("input_tokens_count")
                output_tokens = usage.get("output_tokens") or usage.get("output_tokens_count")
                if self._is_openai_compatible or self._is_completions_api:
                    try:
                        choice = (response_json.get('choices') or [{}])[0]
                        response_text = choice.get('message', {}).get('content', '') or choice.get('text', '') or ''
                    except (IndexError, KeyError, TypeError):
                        response_text = json.dumps(response_json, indent=2)
                    usage2 = response_json.get('usage') or {}
                    input_tokens = usage2.get('prompt_tokens') or usage2.get('input_tokens') or input_tokens
                    output_tokens = usage2.get('completion_tokens') or usage2.get('output_tokens') or output_tokens
                    return (response_text, {"input_tokens": input_tokens or self.estimate_token_count(prompt), "output_tokens": output_tokens or self.estimate_token_count(response_text)})
                if self._is_custom_gateway:
                    response_text = None
                    if 'content' in response_json:
                        if isinstance(response_json['content'], list) and len(response_json['content']) > 0:
                            text_parts = [item.get('text', '') for item in response_json['content'] if isinstance(item, dict) and 'text' in item]
                            if text_parts:
                                response_text = ''.join(text_parts)
                        elif isinstance(response_json['content'], str):
                            response_text = response_json['content']
                    if not response_text and 'text' in response_json:
                        response_text = response_json['text']
                    if not response_text:
                        response_text = json.dumps(response_json, indent=2)
                    return (response_text, {"input_tokens": input_tokens or self.estimate_token_count(prompt), "output_tokens": output_tokens or self.estimate_token_count(response_text)})
                response_text = response_json['content'][0].get('text', '') if response_json.get('content') else json.dumps(response_json, indent=2)
                return (response_text, {"input_tokens": input_tokens or self.estimate_token_count(prompt), "output_tokens": output_tokens or self.estimate_token_count(response_text)})
            if last_response is not None:
                last_response.raise_for_status()
        except requests.exceptions.RequestException as e:
            error_msg = f"LLM API connection error: {e}"
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_msg += f"\nResponse: {json.dumps(e.response.json(), indent=2)}"
                except Exception:
                    error_msg += f"\nResponse status: {e.response.status_code}"
            raise RuntimeError(error_msg)

    def estimate_token_count(self, text: str) -> int:
        try:
            import tiktoken
            return len(tiktoken.get_encoding("cl100k_base").encode(text))
        except ImportError:
            return len(text) // 4

    def suggest_files(self, problem_statement: str, must_gather_docs_dir: str) -> Dict:
        try:
            docs = load_must_gather_documentation(must_gather_docs_dir)
        except FileNotFoundError:
            docs = {}
        if not docs:
            default_yaml_paths = [
                "/quay*/cluster-scoped-resources/config.openshift.io/clusteroperators.yaml",
                "/quay*/cluster-scoped-resources/config.openshift.io/clusterversions.yaml",
                "/quay*/namespaces/openshift-cluster-version/core/events.yaml",
            ]
            return {
                'suggested_files': [{'path': p, 'priority': 'high', 'reason': 'Default list (no docs)'} for p in default_yaml_paths],
                'reasoning': f"No must-gather documentation found in {must_gather_docs_dir}.",
                'problem_category': 'Unknown',
                'priority': {},
                'input_tokens': 0,
                'output_tokens': 0,
            }
        problem_statement = problem_statement.strip()
        prompt = self._create_file_selection_prompt(problem_statement, docs)
        try:
            response_text, usage = self._call_llm_api(prompt)
        except Exception as e:
            return {'error': str(e), 'suggested_files': [], 'reasoning': '', 'problem_category': 'Unknown', 'priority': {}, 'input_tokens': 0, 'output_tokens': 0}
        result = self._parse_llm_response(response_text)
        result['input_tokens'] = usage.get('input_tokens', 0)
        result['output_tokens'] = usage.get('output_tokens', 0)
        return result

    def _create_file_selection_prompt(self, problem_statement: str, docs: Dict[str, str]) -> str:
        docs_context = ""
        for doc_name, doc_content in docs.items():
            docs_context += f"\n\n=== {doc_name} ===\n{doc_content}\n"
        return f"""You are an expert OpenShift/Kubernetes system analyst. Analyze the user's problem statement and suggest which files from a must-gather collection need to be analyzed.

MUST-GATHER DOCUMENTATION:
{docs_context}

USER PROBLEM STATEMENT:
{problem_statement}

TASK: Suggest specific file paths from the must-gather collection.

OUTPUT FORMAT — CRITICAL: Problem category line, then numbered list with [high]/[medium]/[low] priority tags.

Problem category: <category>
1. [high] path/to/file — reason
2. [medium] path/to/file — reason
3. [low] path/to/file — reason

Rules:
- First line must be exactly "Problem category: <category>"
- Each list line: number, period, space, [high] or [medium] or [low], space, path, then " — " and a short reason
- Always include both current.log and previous.log for pod logs
- Base suggestions on the must-gather structure documentation above
- Output NOTHING else. No JSON. No markdown."""

    def _parse_llm_response(self, response: str) -> Dict:
        response = response.strip()
        if response.startswith('```'):
            first_newline = response.find('\n')
            if first_newline != -1:
                response = response[first_newline:].strip()
            if response.endswith('```'):
                response = response[:-3].strip()
        problem_category = 'Unknown'
        lines = response.splitlines()
        if lines:
            first = lines[0].strip()
            m_cat = re.match(r'^Problem\s+category\s*:\s*(.+)$', first, re.IGNORECASE)
            if m_cat:
                problem_category = m_cat.group(1).strip()
                lines = lines[1:]
                response = '\n'.join(lines)
        suggested_files = []
        for line in response.splitlines():
            line = line.strip()
            if not line:
                continue
            path = None
            priority = 'medium'
            m = re.match(r'^\s*\d+\.\s*\[(high|medium|low)\]\s*(.+)$', line, re.IGNORECASE)
            if m:
                priority = m.group(1).lower()
                path = m.group(2).strip()
            else:
                m = re.match(r'^\s*\d+\.\s*(.+)$', line)
                if m:
                    path = m.group(1).strip()
                    if path.startswith('[high]'):
                        priority, path = 'high', path[6:].strip()
                    elif path.startswith('[medium]'):
                        priority, path = 'medium', path[9:].strip()
                    elif path.startswith('[low]'):
                        priority, path = 'low', path[5:].strip()
            if not path:
                for prefix in ('- ', '* ', '• '):
                    if line.startswith(prefix) and '/' in line:
                        path = line[len(prefix):].strip()
                        break
            if path and not path.startswith(('http://', 'https://')):
                reason = ''
                if ' — ' in path:
                    path, reason = path.split(' — ', 1)
                    path = path.strip()
                    reason = reason.strip()
                suggested_files.append({'path': path, 'priority': priority, 'reason': reason})
        if not suggested_files and response:
            for line in response.splitlines():
                line = line.strip()
                if not line or len(line) < 5 or '/' not in line:
                    continue
                if line.startswith(('http', '#', '##')):
                    continue
                if line.startswith(('- ', '* ', '• ')):
                    line = line[2:].strip()
                if re.match(r'^[\w\-.*/\[\]()]+$', line):
                    suggested_files.append({'path': line, 'priority': 'medium', 'reason': ''})
        out = {
            'suggested_files': suggested_files,
            'reasoning': '',
            'problem_category': problem_category,
            'priority': {f.get('path', ''): f.get('priority', 'medium') for f in suggested_files},
        }
        if not suggested_files and response:
            out['raw_response'] = response
        return out

    def print_file_suggestions(self, result: Dict):
        if 'error' in result:
            print(f"\nError: {result['error']}")
            return
        print("\n" + "="*100 + "\nMUST-GATHER FILE SUGGESTIONS\n" + "="*100)
        print(f"\nProblem Category: {result.get('problem_category', 'Unknown')}")
        for i, f in enumerate(result.get('suggested_files', []), 1):
            print(f"  {i}. [{f.get('priority', '')}] {f.get('path', '')}")
            if f.get('reason'):
                print(f"     Reason: {f['reason']}")
        print("\n" + "="*100)


def main():
    user_problem_statement = "Kubelet configuration directory is not created on scaled out node."
    must_gather_docs_dir = MUST_GATHER_DOCS_DIR_DEFAULT
    try:
        selector = MustGatherFileSelector()
        print("Analyzing problem statement and suggesting must-gather files...")
        result = selector.suggest_files(user_problem_statement, must_gather_docs_dir)
        selector.print_file_suggestions(result)
        return result
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return None


if __name__ == "__main__":
    main()
