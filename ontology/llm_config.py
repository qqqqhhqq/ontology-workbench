#!/usr/bin/env python3
"""真实 LLM 模型与 API Key 配置管理器（安全掩码、多来源合并与本地持久化）。

配置读取优先级（高 -> 低）：
  1. 单次调用显式传入的 runtime_override
  2. 系统环境变量与工作区根目录 .env 文件（LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY ...）
  3. 本地私有配置文件 config/llm.local.yaml（已加入 .gitignore）
  4. 模板默认值 config/llm.example.yaml
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG_PATH = ROOT_DIR / "config" / "llm.example.yaml"
LOCAL_CONFIG_PATH = ROOT_DIR / "config" / "llm.local.yaml"
DOTENV_PATH = ROOT_DIR / ".env"

SUPPORTED_PROVIDERS = {"openai_compatible", "anthropic", "gemini"}

DEFAULT_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "openai": {
        "label": "OpenAI",
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "deepseek": {
        "label": "DeepSeek",
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "dashscope_qwen": {
        "label": "通义千问 (百炼)",
        "provider": "openai_compatible",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-20250514",
    },
    "gemini": {
        "label": "Google Gemini",
        "provider": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-2.5-flash",
    },
    "ollama_local": {
        "label": "本地 Ollama",
        "provider": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
    },
}


@dataclass
class LLMConfig:
    provider: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    temperature: float = 0.1
    max_turns: int = 8
    timeout_seconds: int = 60
    config_source: str = "default"

    def is_local_no_key_endpoint(self) -> bool:
        low = self.base_url.lower()
        return any(h in low for h in ("127.0.0.1", "localhost", "0.0.0.0"))

    def is_ready(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip() and (self.api_key.strip() or self.is_local_no_key_endpoint()))

    def masked_api_key(self) -> str:
        k = self.api_key.strip()
        if not k:
            return ""
        if len(k) <= 8:
            return "*" * len(k)
        return f"{k[:4]}****{k[-4:]}"

    def to_public_dict(self) -> dict[str, Any]:
        """返回给前端/日志的安全视图，永不泄露明文 api_key。"""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "has_api_key": bool(self.api_key.strip()),
            "api_key_masked": self.masked_api_key(),
            "is_ready": self.is_ready(),
            "is_local_endpoint": self.is_local_no_key_endpoint(),
            "temperature": self.temperature,
            "max_turns": self.max_turns,
            "timeout_seconds": self.timeout_seconds,
            "config_source": self.config_source,
            "local_config_path": str(LOCAL_CONFIG_PATH.relative_to(ROOT_DIR)),
            "provider_presets": load_provider_presets(),
        }


def _read_dotenv(path: Path = DOTENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            result[k] = v
    return result


def load_provider_presets(example_path: Path = EXAMPLE_CONFIG_PATH) -> dict[str, dict[str, str]]:
    if example_path.exists():
        data = yaml.safe_load(example_path.read_text(encoding="utf-8")) or {}
        presets = data.get("provider_presets")
        if isinstance(presets, dict) and presets:
            return presets
    return dict(DEFAULT_PROVIDER_PRESETS)


def load_llm_config(
    override: dict[str, Any] | None = None,
    local_config_path: Path = LOCAL_CONFIG_PATH,
    dotenv_path: Path = DOTENV_PATH,
) -> LLMConfig:
    """按 模板 -> config/llm.local.yaml -> .env / 环境变量 -> 单次覆盖 的优先级合并配置。"""
    cfg = LLMConfig()
    sources: list[str] = []

    # 1. config/llm.local.yaml
    if local_config_path.exists():
        raw = yaml.safe_load(local_config_path.read_text(encoding="utf-8")) or {}
        sec = raw.get("llm") or {}
        if sec:
            sources.append(local_config_path.name)
            _apply_dict(cfg, sec)

    # 2. .env + os.environ
    dotenv_vars = _read_dotenv(dotenv_path)
    env_map = {
        "provider": os.environ.get("LLM_PROVIDER") or dotenv_vars.get("LLM_PROVIDER"),
        "base_url": os.environ.get("LLM_BASE_URL") or dotenv_vars.get("LLM_BASE_URL"),
        "model": os.environ.get("LLM_MODEL") or dotenv_vars.get("LLM_MODEL"),
        "api_key": os.environ.get("LLM_API_KEY") or dotenv_vars.get("LLM_API_KEY"),
        "temperature": os.environ.get("LLM_TEMPERATURE") or dotenv_vars.get("LLM_TEMPERATURE"),
        "max_turns": os.environ.get("LLM_MAX_TURNS") or dotenv_vars.get("LLM_MAX_TURNS"),
        "timeout_seconds": os.environ.get("LLM_TIMEOUT_SECONDS") or dotenv_vars.get("LLM_TIMEOUT_SECONDS"),
    }
    env_non_empty = {k: v for k, v in env_map.items() if v not in (None, "")}
    if env_non_empty:
        sources.append("env")
        _apply_dict(cfg, env_non_empty)

    # 3. 单次请求覆盖（若 api_key 为空字符串，保留已保存的密钥，防止前端留空误覆盖）
    if override:
        ov = dict(override)
        if not str(ov.get("api_key") or "").strip():
            ov.pop("api_key", None)
        non_empty = {k: v for k, v in ov.items() if v not in (None, "")}
        if non_empty:
            sources.append("request_override")
            _apply_dict(cfg, non_empty)

    if cfg.provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"不支持的模型协议 provider={cfg.provider!r}，可选：{sorted(SUPPORTED_PROVIDERS)}")

    cfg.config_source = " + ".join(sources) if sources else "default"
    return cfg


def save_llm_config(
    updates: dict[str, Any],
    local_config_path: Path = LOCAL_CONFIG_PATH,
) -> LLMConfig:
    """将用户配置的真实模型地址与 API Key 持久化到 config/llm.local.yaml（已 gitignore）。"""
    existing: dict[str, Any] = {}
    if local_config_path.exists():
        raw = yaml.safe_load(local_config_path.read_text(encoding="utf-8")) or {}
        existing = dict(raw.get("llm") or {})

    merged = {
        "provider": str(updates.get("provider") or existing.get("provider") or "openai_compatible").strip(),
        "base_url": str(updates.get("base_url") or existing.get("base_url") or "https://api.openai.com/v1").strip(),
        "model": str(updates.get("model") or existing.get("model") or "gpt-4o-mini").strip(),
        "api_key": existing.get("api_key", ""),
        "temperature": float(updates.get("temperature", existing.get("temperature", 0.1))),
        "max_turns": int(updates.get("max_turns", existing.get("max_turns", 8))),
        "timeout_seconds": int(updates.get("timeout_seconds", existing.get("timeout_seconds", 60))),
    }

    if merged["provider"] not in SUPPORTED_PROVIDERS:
        raise ValueError(f"不支持的模型协议：{merged['provider']}")

    # 支持显式清空或更新 api_key；若前端未传 api_key 或传空且 clear_api_key!=True，保留已有密钥
    if updates.get("clear_api_key"):
        merged["api_key"] = ""
    elif "api_key" in updates and str(updates["api_key"]).strip():
        merged["api_key"] = str(updates["api_key"]).strip()

    local_config_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 本地真实模型接入配置（已被 .gitignore 忽略，不会被提交到 Git）\n"
        "# 可直接编辑本文件，或在 Web 控制台顶部更新保存。\n\n"
    )
    local_config_path.write_text(
        header + yaml.safe_dump({"llm": merged}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return load_llm_config(local_config_path=local_config_path)


def _apply_dict(cfg: LLMConfig, data: dict[str, Any]) -> None:
    if data.get("provider"):
        cfg.provider = str(data["provider"]).strip()
    if data.get("base_url"):
        cfg.base_url = str(data["base_url"]).strip()
    if data.get("model"):
        cfg.model = str(data["model"]).strip()
    if data.get("api_key") is not None and str(data["api_key"]).strip():
        cfg.api_key = str(data["api_key"]).strip()
    if data.get("temperature") not in (None, ""):
        cfg.temperature = float(data["temperature"])
    if data.get("max_turns") not in (None, ""):
        cfg.max_turns = int(data["max_turns"])
    if data.get("timeout_seconds") not in (None, ""):
        cfg.timeout_seconds = int(data["timeout_seconds"])
