"""Shared bilingual prompt registry for every local model provider."""

from .contracts import (
    SUPPORTED_LOCALES,
    TRADING_STRATEGIES,
    Locale,
    PromptBundle,
    PromptInputs,
    PromptTask,
    TradingStrategy,
)
from .registry import (
    news_questions,
    resolve_prompt,
    task_prompt_version,
    task_prompt_versions,
    validate_registry,
)

__all__ = [
    "SUPPORTED_LOCALES",
    "TRADING_STRATEGIES",
    "Locale",
    "PromptBundle",
    "PromptInputs",
    "PromptTask",
    "TradingStrategy",
    "news_questions",
    "resolve_prompt",
    "task_prompt_version",
    "task_prompt_versions",
    "validate_registry",
]
