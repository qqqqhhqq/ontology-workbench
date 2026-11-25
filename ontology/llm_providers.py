#!/usr/bin/env python3
"""多协议真实大模型 HTTP 客户端（零第三方依赖，基于 Python stdlib urllib）。

支持三大主流协议，覆盖市面上绝大多数云端与本地大模型：
  1. openai_compatible : OpenAI / DeepSeek / 阿里云百炼(Qwen) / 智谱 / Moonshot / vLLM / Ollama
  2. anthropic         : Anthropic Claude Messages API (/v1/messages)
  3. gemini            : Google Gemini REST API (/models/{model}:generateContent)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ontology.llm_config import LLMConfig


class LLMCallError(Exception):
    """调用真实大模型接口失败（包含 HTTP 状态码与服务端原始报错）。"""


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    provider: str
    model: str
    text: str = ""
    reasoning: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0
    raw_assistant_entry: dict[str, Any] = field(default_factory=dict)


def _http_post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int,
    max_429_retries: int = 2,
) -> tuple[dict[str, Any], int]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(max_429_retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                elapsed = max(1, int((time.perf_counter() - t0) * 1000))
                return json.loads(raw), elapsed
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_429_retries:
                time.sleep(3 * (attempt + 1))
                continue
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:600]
            except Exception:
                pass
            raise LLMCallError(f"HTTP {exc.code} {exc.reason} ({url}) -> {err_body}") from exc
        except urllib.error.URLError as exc:
            raise LLMCallError(f"无法连接模型端点 {url}：{exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMCallError(f"请求模型超时（>{timeout}s）：{url}") from exc
        except json.JSONDecodeError as exc:
            raise LLMCallError(f"模型返回非合法 JSON：{exc}") from exc
    raise LLMCallError(f"请求 {url} 在重试后仍失败")


# ---------------------------------------------------------------------------
# 1. OpenAI Compatible (/chat/completions)
# ---------------------------------------------------------------------------


def _call_openai_compatible(
    cfg: LLMConfig,
    system_prompt: str,
    history: list[dict[str, Any]],
    mcp_tools: list[dict[str, Any]],
) -> LLMResponse:
    url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    messages = [{"role": "system", "content": system_prompt}, *history]
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "stream": False,
    }
    if mcp_tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["inputSchema"],
                },
            }
            for t in mcp_tools
        ]
        payload["tool_choice"] = "auto"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.api_key.strip() or 'ollama'}",
    }
    data, elapsed = _http_post_json(url, headers, payload, cfg.timeout_seconds)

    choices = data.get("choices") or []
    if not choices:
        raise LLMCallError(f"OpenAI 兼容接口未返回 choices：{json.dumps(data, ensure_ascii=False)[:400]}")

    msg = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()

    tool_calls: list[LLMToolCall] = []
    for idx, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        tool_calls.append(
            LLMToolCall(
                id=str(tc.get("id") or f"call_{idx + 1}"),
                name=str(fn.get("name") or ""),
                arguments=args,
            )
        )

    usage_raw = data.get("usage") or {}
    p_tok = int(usage_raw.get("prompt_tokens") or 0)
    c_tok = int(usage_raw.get("completion_tokens") or 0)
    t_tok = int(usage_raw.get("total_tokens") or (p_tok + c_tok))

    # 标准化 assistant 消息供下一轮回传（严格遵守 OpenAI 消息格式）
    assistant_entry: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        assistant_entry["tool_calls"] = msg["tool_calls"]

    return LLMResponse(
        provider=cfg.provider,
        model=str(data.get("model") or cfg.model),
        text=text,
        reasoning=reasoning,
        tool_calls=tool_calls,
        usage=LLMUsage(p_tok, c_tok, t_tok),
        latency_ms=elapsed,
        raw_assistant_entry=assistant_entry,
    )


# ---------------------------------------------------------------------------
# 2. Anthropic Claude Messages API (/v1/messages)
# ---------------------------------------------------------------------------


def _call_anthropic(
    cfg: LLMConfig,
    system_prompt: str,
    history: list[dict[str, Any]],
    mcp_tools: list[dict[str, Any]],
) -> LLMResponse:
    base = cfg.base_url.rstrip("/")
    url = base if base.endswith("/messages") else f"{base}/v1/messages"
    payload: dict[str, Any] = {
        "model": cfg.model,
        "max_tokens": 2048,
        "temperature": cfg.temperature,
        "system": system_prompt,
        "messages": history,
    }
    if mcp_tools:
        payload["tools"] = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["inputSchema"],
            }
            for t in mcp_tools
        ]

    headers = {
        "Content-Type": "application/json",
        "x-api-key": cfg.api_key.strip(),
        "anthropic-version": "2023-06-01",
    }
    data, elapsed = _http_post_json(url, headers, payload, cfg.timeout_seconds)

    blocks = data.get("content") or []
    texts: list[str] = []
    reasonings: list[str] = []
    tool_calls: list[LLMToolCall] = []

    for blk in blocks:
        btype = blk.get("type")
        if btype == "text":
            texts.append(str(blk.get("text") or ""))
        elif btype == "thinking":
            reasonings.append(str(blk.get("thinking") or ""))
        elif btype == "tool_use":
            tool_calls.append(
                LLMToolCall(
                    id=str(blk.get("id") or f"toolu_{len(tool_calls) + 1}"),
                    name=str(blk.get("name") or ""),
                    arguments=dict(blk.get("input") or {}),
                )
            )

    usage_raw = data.get("usage") or {}
    p_tok = int(usage_raw.get("input_tokens") or 0)
    c_tok = int(usage_raw.get("output_tokens") or 0)

    return LLMResponse(
        provider=cfg.provider,
        model=str(data.get("model") or cfg.model),
        text="\n".join(texts).strip(),
        reasoning="\n".join(reasonings).strip(),
        tool_calls=tool_calls,
        usage=LLMUsage(p_tok, c_tok, p_tok + c_tok),
        latency_ms=elapsed,
        raw_assistant_entry={"role": "assistant", "content": blocks},
    )


# ---------------------------------------------------------------------------
# 3. Google Gemini REST API (/models/{model}:generateContent)
# ---------------------------------------------------------------------------


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """将标准 JSON Schema 转为 Gemini REST API 兼容的 OpenAPI 子集。"""
    t = str(schema.get("type", "object")).upper()
    out: dict[str, Any] = {"type": t}
    if "description" in schema:
        out["description"] = schema["description"]
    if "enum" in schema:
        out["enum"] = list(schema["enum"])
    if t == "OBJECT":
        props = schema.get("properties") or {}
        if props:
            out["properties"] = {k: _to_gemini_schema(v) for k, v in props.items()}
        if schema.get("required"):
            out["required"] = list(schema["required"])
    elif t == "ARRAY" and "items" in schema:
        out["items"] = _to_gemini_schema(schema["items"])
    return out


def _call_gemini(
    cfg: LLMConfig,
    system_prompt: str,
    history: list[dict[str, Any]],
    mcp_tools: list[dict[str, Any]],
) -> LLMResponse:
    base = cfg.base_url.rstrip("/")
    key_q = urllib.parse.quote(cfg.api_key.strip(), safe="")
    url = f"{base}/models/{urllib.parse.quote(cfg.model, safe='-._')}:generateContent?key={key_q}"

    payload: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": history,
        "generationConfig": {"temperature": cfg.temperature},
    }
    if mcp_tools:
        payload["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": _to_gemini_schema(t["inputSchema"]),
                    }
                    for t in mcp_tools
                ]
            }
        ]

    headers = {"Content-Type": "application/json"}
    data, elapsed = _http_post_json(url, headers, payload, cfg.timeout_seconds)

    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMCallError(f"Gemini 接口未返回 candidates：{json.dumps(data, ensure_ascii=False)[:400]}")

    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    texts: list[str] = []
    tool_calls: list[LLMToolCall] = []

    for idx, part in enumerate(parts):
        if "text" in part:
            texts.append(str(part["text"]))
        if "functionCall" in part:
            fc = part["functionCall"]
            tool_calls.append(
                LLMToolCall(
                    id=f"gemini_call_{idx + 1}",
                    name=str(fc.get("name") or ""),
                    arguments=dict(fc.get("args") or {}),
                )
            )

    u = data.get("usageMetadata") or {}
    p_tok = int(u.get("promptTokenCount") or 0)
    c_tok = int(u.get("candidatesTokenCount") or 0)
    t_tok = int(u.get("totalTokenCount") or (p_tok + c_tok))

    return LLMResponse(
        provider=cfg.provider,
        model=cfg.model,
        text="\n".join(texts).strip(),
        reasoning="",
        tool_calls=tool_calls,
        usage=LLMUsage(p_tok, c_tok, t_tok),
        latency_ms=elapsed,
        raw_assistant_entry={"role": "model", "parts": parts},
    )


# ---------------------------------------------------------------------------
# 统一调度、消息构造与连通性探活
# ---------------------------------------------------------------------------


def build_initial_history(provider: str, user_task: str) -> list[dict[str, Any]]:
    if provider == "gemini":
        return [{"role": "user", "parts": [{"text": user_task}]}]
    return [{"role": "user", "content": user_task}]


def append_tool_results(
    provider: str,
    history: list[dict[str, Any]],
    results: list[tuple[LLMToolCall, dict[str, Any]]],
) -> None:
    """按各协议严格规范将本轮全部工具执行结果追加到会话历史。"""
    if not results:
        return
    if provider == "openai_compatible":
        for tc, payload in results:
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            )
    elif provider == "anthropic":
        history.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": json.dumps(payload, ensure_ascii=False),
                        "is_error": not bool(payload.get("ok", True)),
                    }
                    for tc, payload in results
                ],
            }
        )
    elif provider == "gemini":
        history.append(
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": tc.name,
                            "response": payload,
                        }
                    }
                    for tc, payload in results
                ],
            }
        )


def call_llm(
    cfg: LLMConfig,
    system_prompt: str,
    history: list[dict[str, Any]],
    mcp_tools: list[dict[str, Any]],
) -> LLMResponse:
    if cfg.provider == "openai_compatible":
        return _call_openai_compatible(cfg, system_prompt, history, mcp_tools)
    if cfg.provider == "anthropic":
        return _call_anthropic(cfg, system_prompt, history, mcp_tools)
    if cfg.provider == "gemini":
        return _call_gemini(cfg, system_prompt, history, mcp_tools)
    raise LLMCallError(f"不支持的模型协议：{cfg.provider}")


def ping_llm(cfg: LLMConfig) -> dict[str, Any]:
    """向真实模型端点发一条极简探活请求，用于验证 Base URL、Model 与 API Key 连通性。"""
    if not cfg.is_ready():
        return {
            "ok": False,
            "provider": cfg.provider,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "message": "API Key 尚未配置（预留状态）。请先填写 API Key 或配置本地 Ollama 地址后重试。",
        }
    try:
        resp = call_llm(
            cfg=cfg,
            system_prompt="You are a connectivity test endpoint. Reply briefly with PONG.",
            history=build_initial_history(cfg.provider, "Reply with PONG."),
            mcp_tools=[],
        )
        return {
            "ok": True,
            "provider": resp.provider,
            "model": resp.model,
            "base_url": cfg.base_url,
            "latency_ms": resp.latency_ms,
            "reply": resp.text[:120],
            "usage": {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            },
        }
    except LLMCallError as exc:
        return {
            "ok": False,
            "provider": cfg.provider,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "message": str(exc),
        }
