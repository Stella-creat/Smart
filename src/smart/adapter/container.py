"""适配器 — 业务层组装依赖，提供统一 AIProvider 单例。"""
from __future__ import annotations

from smart.adapter.provider.ai_provider import AIProvider
from smart.adapter.provider.deep_tutor_provider import DeepTutorProvider

_provider_instance: AIProvider | None = None


def get_ai_provider() -> AIProvider:
    """全局 AIProvider — 默认 DeepTutor 实现。

    更换实现（如 DeepSeek 直连）只需在入口处调用 set_ai_provider(new_impl)，
    业务层所有 import get_ai_provider() 的代码自动切到新实现。
    """
    global _provider_instance
    if _provider_instance is None:
        _provider_instance = DeepTutorProvider()
    return _provider_instance


def set_ai_provider(provider: AIProvider) -> None:
    global _provider_instance
    _provider_instance = provider
