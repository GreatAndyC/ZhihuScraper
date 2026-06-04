from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class LLMSettings:
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = ""
    model: str = ""


def load_llm_settings() -> LLMSettings:
    provider = (os.getenv("RAG_LLM_PROVIDER", "deepseek") or "deepseek").strip().lower()
    if provider == "deepseek":
        return LLMSettings(
            provider=provider,
            api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip(),
        )
    if provider == "openai":
        return LLMSettings(
            provider=provider,
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            base_url=os.getenv("OPENAI_BASE_URL", "").strip(),
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip(),
        )
    raise ValueError(f"不支持的 LLM 提供方: {provider}")


class LLMClient:
    def __init__(self, settings: LLMSettings | None = None):
        self.settings = settings or load_llm_settings()

    def ensure_configured(self) -> None:
        if not self.settings.api_key:
            raise RuntimeError(f"{self.settings.provider} API Key 未配置")
        if not self.settings.model:
            raise RuntimeError(f"{self.settings.provider} 模型名未配置")

    def chat(self, *, system_prompt: str, user_prompt: str) -> str:
        self.ensure_configured()
        raise NotImplementedError("一期仅落客户端骨架；后续在此接入 DeepSeek/OpenAI 实际调用逻辑")
