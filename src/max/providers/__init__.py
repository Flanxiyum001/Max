"""Provider adapters for MAX."""

from .base import (  # noqa: F401
    ChatProvider,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimit,
    ProviderResult,
    ProviderServerError,
    ProviderTimeout,
    ToolCall,
)
from .mock import MockProvider  # noqa: F401
from .openai_compat import OpenAICompatProvider  # noqa: F401


def build_provider(config) -> ChatProvider:
    """Construct the provider named in configuration.

    Raises ProviderConfigError (never a crash) when configuration is missing.
    """
    if config.provider in {"mock", "test"}:
        return MockProvider()
    if config.provider in {"openai", "openai-compat", "freellm"}:
        if not config.base_url:
            raise ProviderConfigError(
                "MAX_BASE_URL is not configured; set it to your provider's "
                "OpenAI-compatible base URL (see .env.example)"
            )
        if not config.model:
            raise ProviderConfigError(
                "MAX_MODEL is not configured; set it to a model identifier "
                "your provider reports as available"
            )
        return OpenAICompatProvider(
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
            timeout=config.timeout_s,
            retries=config.retries,
        )
    raise ProviderConfigError(f"unknown MAX_PROVIDER '{config.provider}'")
