"""Configuration for the knowledge-graph build pipeline.

Settings can be provided three ways (later wins):

1. a YAML config file (``--config config.yaml``) whose layout mirrors these
   models;
2. environment variables (prefix ``KGB_``, nested keys separated by ``__``,
   e.g. ``KGB_LLM__API_KEY``);
3. explicit keyword overrides passed to :func:`settings_for`.

Semantic enrichment is performed **exclusively by an LLM** (OpenAI-compatible,
via LangChain). There is no heuristic/offline fallback: if enrichment is enabled
but the model is not configured, the build raises a clear error.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class CodeSettings(BaseModel):
    """Source-code collector settings."""

    enabled: bool = True
    # Languages to analyse. "auto" -> detect from file extensions.
    languages: list[str] = Field(default_factory=lambda: ["auto"])
    # Directory names always skipped.
    exclude_dirs: list[str] = Field(
        default_factory=lambda: [
            "node_modules", ".git", "venv", ".venv", "__pycache__",
            "dist", "build", "target", ".idea", ".vscode", "vendor",
        ]
    )
    # File extensions considered source files (used for language auto-detection).
    max_file_bytes: int = 512_000


class CodeAnalysisSettings(BaseModel):
    """Open-source-toolkit code collector (scip-python + radon + tree-sitter).

    When enabled, this collector replaces the legacy ``CodeCollector`` entirely.
    Hard-failure semantics: if any toolkit is unavailable, collection aborts
    with a clear error rather than silently degrading.
    """

    enabled: bool = True
    java_enabled: bool = False


class DocumentSettings(BaseModel):
    """Document collector settings (architecture / interface / config docs)."""

    enabled: bool = True
    # Recognised documentation extensions.
    extensions: list[str] = Field(
        default_factory=lambda: [".md", ".markdown", ".rst", ".txt", ".yaml", ".yml", ".json", ".toml", ".ini", ".properties"]
    )
    max_file_bytes: int = 1_000_000


class CloudProductSettings(BaseModel):
    """Generic cloud product toggle with optional region override."""

    enabled: bool = False
    region: Optional[str] = None  # override top-level aliyun_region


class RdsSettings(CloudProductSettings):
    """RDS collector settings."""

    instance_ids: list[str] = Field(default_factory=list)


class OssSettings(CloudProductSettings):
    """OSS collector settings."""

    bucket_names: list[str] = Field(default_factory=list)


class SlsSettings(CloudProductSettings):
    """SLS (Log Service) collector settings."""

    project: str = ""
    logstores: list[str] = Field(default_factory=list)


class RosSettings(CloudProductSettings):
    """ROS (Resource Orchestration) collector settings."""

    stack_names: list[str] = Field(default_factory=list)


class AcrSettings(CloudProductSettings):
    """ACR (Container Registry) collector settings."""

    instance_ids: list[str] = Field(default_factory=list)
    otel_only: bool = True


class AlbSettings(CloudProductSettings):
    """ALB (Application Load Balancer) collector settings."""

    load_balancer_ids: list[str] = Field(default_factory=list)


class FcSettings(CloudProductSettings):
    """FC (Function Compute) collector settings."""

    function_names: list[str] = Field(default_factory=list)


class EventBridgeSettings(CloudProductSettings):
    """EventBridge collector settings."""

    bus_names: list[str] = Field(default_factory=list)


class GrafanaSettings(BaseModel):
    """Grafana collector settings (non-Alibaba SDK, direct HTTP API)."""

    enabled: bool = False
    url: str = ""
    workspace_id: str = ""
    api_token_env: str = "GRAFANA_API_TOKEN"  # env var name holding the token
    folder_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TairSettings(CloudProductSettings):
    """Tair / Redis collector settings."""

    instance_ids: list[str] = Field(default_factory=list)


class CodeupSettings(BaseModel):
    """Codeup (Yunxiao DevOps) collector settings."""

    enabled: bool = False
    org_id: str = ""
    project_name: str = ""
    yunxiao_token_env: str = "YUNXIAO_TOKEN"  # env var name
    repo_ids: list[str] = Field(default_factory=list)
    pipeline_ids: list[str] = Field(default_factory=list)


class DeploymentSettings(BaseModel):
    """Deployment (Kubernetes / Alibaba Cloud) collector settings.

    Supports K8s (ACK) and 16 Alibaba Cloud products. Credentials are read
    from environment variables ``ALIBABA_CLOUD_ACCESS_KEY_ID`` /
    ``ALIBABA_CLOUD_ACCESS_KEY_SECRET`` by default, or can be set inline.
    """

    enabled: bool = False

    # --- K8s / ACK ---
    kubeconfig: Optional[str] = None
    kubeconfig_paths: list[str] = Field(default_factory=list)
    context: Optional[str] = None
    namespaces: list[str] = Field(default_factory=list)  # empty -> all
    cluster_name: str = "default"
    cluster_id: Optional[str] = None  # ACK cluster ID

    # --- Alibaba Cloud credentials ---
    aliyun_region: str = "cn-hongkong"
    aliyun_access_key_id: Optional[str] = None   # prefer env ALIBABA_CLOUD_ACCESS_KEY_ID
    aliyun_access_key_secret: Optional[str] = None  # prefer env ALIBABA_CLOUD_ACCESS_KEY_SECRET
    aliyun_account_id: Optional[str] = None

    # --- Cloud product toggles & params ---
    ecs: CloudProductSettings = Field(default_factory=CloudProductSettings)
    rds: RdsSettings = Field(default_factory=RdsSettings)
    oss: OssSettings = Field(default_factory=OssSettings)
    sls: SlsSettings = Field(default_factory=SlsSettings)
    cms: CloudProductSettings = Field(default_factory=CloudProductSettings)
    ros: RosSettings = Field(default_factory=RosSettings)
    acr: AcrSettings = Field(default_factory=AcrSettings)
    alb: AlbSettings = Field(default_factory=AlbSettings)
    arms: CloudProductSettings = Field(default_factory=CloudProductSettings)
    fc: FcSettings = Field(default_factory=FcSettings)
    eventbridge: EventBridgeSettings = Field(default_factory=EventBridgeSettings)
    grafana: GrafanaSettings = Field(default_factory=GrafanaSettings)
    gtm: CloudProductSettings = Field(default_factory=CloudProductSettings)
    tair: TairSettings = Field(default_factory=TairSettings)
    codeup: CodeupSettings = Field(default_factory=CodeupSettings)


class RuntimeSettings(BaseModel):
    """Runtime metrics / logs / traces collector settings (currently a STUB)."""

    enabled: bool = False
    prometheus_url: Optional[str] = None
    trace_backend: Optional[str] = None  # e.g. "jaeger", "otel"


class HistorySettings(BaseModel):
    """Version-control (Git) history collector: churn + temporal coupling.

    Reads commit history to attach per-file churn (hotspot risk) and to derive
    co-change edges between modules (Shotgun Surgery signal). A no-op on non-Git
    repos -- the collector records a warning and returns an empty result.
    """

    enabled: bool = True
    max_commits: int = 2000            # bound the history scan
    min_cochange: int = 3              # min shared commits before two files are coupled
    since: Optional[str] = None        # optional ISO date passed to git --since


class LLMSettings(BaseModel):
    """LLM semantic-enrichment settings (OpenAI-compatible endpoint via LangChain).

    There is no offline fallback for enrichment: either the model is configured
    (see :meth:`require_configured`) or enrichment must be explicitly disabled via
    :attr:`BuildSettings.enrich`.
    """

    # Whether enrichment should run. ``True`` by default (LLM-only). Set the
    # top-level ``enrich`` flag to ``False`` to skip the enrichment step entirely.
    provider: str = "openai"              # any OpenAI-compatible chat completions API
    model: str = ""                       # e.g. "gpt-4o-mini", "qwen-max", "deepseek-chat"
    base_url: Optional[str] = None        # e.g. "https://api.openai.com/v1" or an internal gateway
    api_key: Optional[str] = None         # prefer config file / env: KGB_LLM__API_KEY
    temperature: float = 0.0
    max_output_tokens: Optional[int] = None
    request_timeout: int = 60
    # Ask the endpoint for strict JSON responses (OpenAI "response_format").
    json_mode: bool = True
    # Batch size: how many entities to describe per model call.
    max_items_per_call: int = 40

    @model_validator(mode="before")
    @classmethod
    def _accept_api_base_alias(cls, data: Any) -> Any:
        # Tolerate ``api_base`` (used by some SDKs / older env names) as base_url.
        if isinstance(data, dict) and "api_base" in data and not data.get("base_url"):
            data = dict(data)
            data["base_url"] = data.pop("api_base")
        return data

    def require_configured(self) -> None:
        """Raise if the LLM is not usable. No silent degradation."""
        missing = []
        if not self.model:
            missing.append("model")
        if not self.api_key:
            missing.append("api_key")
        if missing:
            raise ValueError(
                "semantic enrichment requires an LLM but these are not configured: "
                f"{', '.join(missing)}. Set them in your config file under 'llm:' "
                "(or env KGB_LLM__MODEL / KGB_LLM__API_KEY), or disable enrichment with "
                "--skip-enrichment."
            )


class BuildSettings(BaseModel):
    """Top-level build configuration."""

    repo_path: Path
    output_path: Path = Path("out/knowledge_graph.json")
    docs_path: Optional[Path] = None        # defaults to repo_path
    namespace: str = ""                     # graph namespace (repo/tenant)
    # Semantic enrichment runs via LLM. When True the LLM must be configured.
    # Set False (CLI --skip-enrichment) to build the structural graph only.
    enrich: bool = True
    code: CodeSettings = Field(default_factory=CodeSettings)
    code_analysis: CodeAnalysisSettings = Field(default_factory=CodeAnalysisSettings)
    document: DocumentSettings = Field(default_factory=DocumentSettings)
    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    history: HistorySettings = Field(default_factory=HistorySettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Load a YAML config file into a plain dict (empty dict if path is falsy)."""
    if not path:
        return {}
    import yaml  # PyYAML is a core dependency

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a top-level mapping")
    return data


def _env_overrides() -> dict[str, Any]:
    """Collect ``KGB_*`` env vars into a nested override dict."""
    out: dict[str, Any] = {}
    for env_key, env_val in os.environ.items():
        if not env_key.startswith("KGB_") or env_val == "":
            continue
        section_path = env_key[len("KGB_"):].lower().split("__")
        node = out
        for part in section_path[:-1]:
            node = node.setdefault(part, {})
        leaf = section_path[-1]
        if leaf == "enabled":
            node[leaf] = env_val.lower() in {"1", "true", "yes"}
        elif leaf in {"temperature", "request_timeout", "max_items_per_call"}:
            try:
                node[leaf] = float(env_val) if leaf == "temperature" else int(env_val)
            except ValueError:
                node[leaf] = env_val
        else:
            node[leaf] = env_val
    # Convenience: fall back to OPENAI_* env vars if KGB_ equivalents are absent.
    llm = out.setdefault("llm", {})
    if isinstance(llm, dict):
        if not llm.get("api_key") and os.getenv("OPENAI_API_KEY"):
            llm["api_key"] = os.getenv("OPENAI_API_KEY")
        if not llm.get("base_url"):
            llm["base_url"] = os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL")
        if not llm.get("model") and os.getenv("OPENAI_MODEL"):
            llm["model"] = os.getenv("OPENAI_MODEL")
        # Drop None entries so they never override a config-file / default value.
        out["llm"] = {k: v for k, v in llm.items() if v is not None}
    # Drop now-empty sections.
    return {k: v for k, v in out.items() if not (isinstance(v, dict) and not v)}


def _deep_merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``top`` into ``base`` (top wins); returns a new dict."""
    result = dict(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _flatten_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    """Turn ``section__field`` keyword overrides into a nested dict."""
    nested: dict[str, Any] = {}
    for key, value in overrides.items():
        if "__" in key:
            section, field = key.split("__", 1)
            nested.setdefault(section, {})[field] = value
        else:
            nested[key] = value
    return nested


def settings_for(
    repo_path: str | Path,
    output_path: str | Path = "out/knowledge_graph.json",
    *,
    config_file: Optional[str | Path] = None,
    **overrides: Any,
) -> BuildSettings:
    """Resolve :class:`BuildSettings` with precedence: overrides > env > config file > defaults.

    Keyword ``overrides`` may target nested sections with a double underscore,
    e.g. ``settings_for("./repo", llm__model="gpt-4o", code__exclude_dirs=["x"])``.
    """
    from_file = load_config_file(config_file)
    from_env = _env_overrides()
    from_kwargs = _flatten_overrides(overrides)

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, from_file)
    merged = _deep_merge(merged, from_env)
    merged = _deep_merge(merged, from_kwargs)
    # Positional args always win.
    merged["repo_path"] = str(repo_path)
    merged["output_path"] = str(output_path)
    return BuildSettings(**merged)
