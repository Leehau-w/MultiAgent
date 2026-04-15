from __future__ import annotations

from .base import ProviderAdapter, ProviderMessage

_registry: dict[str, type[ProviderAdapter]] = {}


def register(name: str, cls: type[ProviderAdapter]) -> None:
    _registry[name] = cls


def get_adapter(provider: str) -> ProviderAdapter:
    """Return a fresh adapter instance for the given provider name."""
    cls = _registry.get(provider)
    if cls is None:
        available = ", ".join(sorted(_registry)) or "(none)"
        raise ValueError(
            f"Unknown provider '{provider}'. Available: {available}"
        )
    return cls()


def _auto_register() -> None:
    """Import adapters so they self-register. Import errors are non-fatal."""
    from . import claude_adapter as _ca  # noqa: F401
    from . import ollama_adapter as _oa  # noqa: F401
    from . import openai_adapter as _oi  # noqa: F401


_auto_register()

__all__ = [
    "ProviderAdapter",
    "ProviderMessage",
    "get_adapter",
    "register",
]
