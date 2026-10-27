"""Runtime configuration (D4).

Every environment-specific value - credentials, endpoints, model identifiers,
limits - is read from environment variables at runtime. Nothing here has a
default URL, key or model name, so the repository holds none. See
`.env.example` for the documented list.
"""
from __future__ import annotations

import os


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing."""


def _get(name: str, default=None, required: bool = False, cast=str):
    raw = os.getenv(name)
    if raw is None or raw == "":
        if required:
            raise ConfigError(f"Missing required environment variable {name}. See .env.example.")
        return default
    if cast is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return cast(raw)


class Settings:
    """Typed view over the environment. Properties re-read os.environ, so tests
    can monkeypatch variables without reloading modules."""

    # --- shared infrastructure -------------------------------------------
    @property
    def service_name(self) -> str:
        return _get("SERVICE_NAME", "ecs")

    @property
    def redis_url(self) -> str:
        return _get("REDIS_URL", required=True)

    @property
    def chroma_host(self) -> str:
        return _get("CHROMA_HOST", required=True)

    @property
    def chroma_port(self) -> int:
        return _get("CHROMA_PORT", required=True, cast=int)

    @property
    def chroma_collection(self) -> str:
        return _get("CHROMA_COLLECTION", "eia_corpus")

    @property
    def embedding_backend(self) -> str:
        # "minilm" = all-MiniLM-L6-v2 via ONNX (local, free); "hash" = tests only
        return _get("EMBEDDING_BACKEND", "minilm")

    # --- service-to-service endpoints --------------------------------------
    @property
    def llm_gateway_url(self) -> str:
        return _get("LLM_GATEWAY_URL", required=True)

    @property
    def verifier_url(self) -> str:
        return _get("VERIFIER_URL", required=True)

    @property
    def otel_endpoint(self) -> str | None:
        return _get("OTEL_EXPORTER_OTLP_ENDPOINT")

    # --- LLM provider (gateway only) --------------------------------------
    @property
    def llm_provider_base_url(self) -> str:
        return _get("LLM_PROVIDER_BASE_URL", required=True)

    @property
    def llm_provider_api_key(self) -> str | None:
        return _get("LLM_PROVIDER_API_KEY")

    @property
    def llm_model(self) -> str:
        return _get("LLM_MODEL", required=True)

    @property
    def llm_fallback_base_url(self) -> str | None:
        return _get("LLM_FALLBACK_BASE_URL")

    @property
    def llm_fallback_api_key(self) -> str | None:
        return _get("LLM_FALLBACK_API_KEY")

    @property
    def llm_fallback_model(self) -> str | None:
        return _get("LLM_FALLBACK_MODEL")

    @property
    def price_input_per_1m(self) -> float:
        return _get("LLM_PRICE_INPUT_PER_1M_USD", 0.0, cast=float)

    @property
    def price_output_per_1m(self) -> float:
        return _get("LLM_PRICE_OUTPUT_PER_1M_USD", 0.0, cast=float)

    # --- agent limits (A1) and reliability (A5) ---------------------------
    @property
    def prompt_version(self) -> str:
        return _get("PROMPT_VERSION", "v2")

    @property
    def max_steps(self) -> int:
        return _get("AGENT_MAX_STEPS", 8, cast=int)

    @property
    def max_tool_calls(self) -> int:
        return _get("AGENT_MAX_TOOL_CALLS", 10, cast=int)

    @property
    def max_run_seconds(self) -> float:
        return _get("AGENT_MAX_RUN_SECONDS", 90.0, cast=float)

    @property
    def max_verify_retries(self) -> int:
        return _get("AGENT_MAX_VERIFY_RETRIES", 1, cast=int)

    @property
    def llm_timeout_s(self) -> float:
        return _get("LLM_TIMEOUT_SECONDS", 30.0, cast=float)

    @property
    def llm_max_attempts(self) -> int:
        return _get("LLM_MAX_ATTEMPTS", 3, cast=int)

    @property
    def tool_timeout_s(self) -> float:
        return _get("TOOL_TIMEOUT_SECONDS", 10.0, cast=float)

    @property
    def verifier_timeout_s(self) -> float:
        return _get("VERIFIER_TIMEOUT_SECONDS", 45.0, cast=float)

    @property
    def breaker_fail_threshold(self) -> int:
        return _get("BREAKER_FAIL_THRESHOLD", 5, cast=int)

    @property
    def breaker_reset_s(self) -> float:
        return _get("BREAKER_RESET_SECONDS", 30.0, cast=float)

    # --- platform controls (D7, L6) ---------------------------------------
    @property
    def rate_limit_per_minute(self) -> int:
        return _get("RATE_LIMIT_PER_MINUTE", 30, cast=int)

    @property
    def run_cache_ttl_s(self) -> int:
        return _get("RUN_CACHE_TTL_SECONDS", 3600, cast=int)

    @property
    def run_cache_enabled(self) -> bool:
        return _get("RUN_CACHE_ENABLED", True, cast=bool)

    @property
    def live_score_sample_rate(self) -> float:
        return _get("LIVE_SCORE_SAMPLE_RATE", 0.2, cast=float)

    @property
    def run_ttl_s(self) -> int:
        return _get("RUN_TTL_SECONDS", 7 * 24 * 3600, cast=int)

    # --- demo / stub (D6) --------------------------------------------------
    @property
    def demo_faults(self) -> bool:
        return _get("DEMO_FAULTS", False, cast=bool)

    @property
    def stub_delay_ms(self) -> float:
        return _get("STUB_DELAY_MS", 1500.0, cast=float)

    @property
    def metrics_port(self) -> int:
        return _get("METRICS_PORT", 9100, cast=int)


settings = Settings()
