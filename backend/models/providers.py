"""Model providers: a unified stream abstraction shared by every agent engine.

Three backends are supported and swapped at runtime (env vars ``MODEL_*`` or the
``/api/settings`` endpoint):

* ``openai``  - OpenAI platform (server API key, or the user's OAuth access
                token via the Responses API with ``chatgpt: true``).
* ``ollama``  - Local Ollama server (``http://localhost:11434/v1``), GPU
                enabled via model params (``num_gpu=-1``). Uses the OpenAI
                client against Ollama's OpenAI-compatible endpoint so tool
                calling + streaming just work.
* ``torch``   - HuggingFace ``transformers`` running locally on GPU/CUDA or
                CPU. Text generation + a light JSON "call a tool" mode (no
                native function calling); optional heavy install.

The unified chunk stream emitted by ``chat_stream`` is:

    {"type": "text", "content": "<delta>"}
    {"type": "tool_calls", "calls": [{"id", "name", "arguments"}]}
    {"type": "usage", "input": int, "output": int}
    {"type": "error", "message": "..."}

Every provider accepts chat-completions-shaped messages (system/user/assistant
with ``tool_calls`` and ``tool`` result messages), and OpenAI tool definitions
``[{"type": "function", "function": {"name", "description", "parameters"}}]``.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Iterator

import openai

from config import config


class ProviderError(Exception):
    pass


@dataclass
class ProviderConfig:
    """Effective runtime config for a model backend."""

    kind: str                      # openai | ollama | torch
    model: str
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.7
    use_responses: bool = False    # openai chatgpt (OAuth) mode
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Compatible base (OpenAI platform + Ollama /v1 + vLLM + LM Studio ...)
# --------------------------------------------------------------------------- #
class CompatProvider:
    """chat.completions-style streaming with tool calling."""

    kind = "compat"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.client = openai.OpenAI(
            api_key=cfg.api_key or "not-needed",
            base_url=cfg.base_url or None,
            timeout=120,
            max_retries=2,
        )

    def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> Iterator[dict]:
        kwargs = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.cfg.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = False
        if self.cfg.extra:
            kwargs["extra_body"] = self.cfg.extra

        try:
            stream = self.client.chat.completions.create(**kwargs)
        except Exception as exc:  # surface model-not-found etc. clearly
            raise ProviderError(f"{self.cfg.kind} :: {exc}") from exc

        call_buf: dict[int, dict] = {}
        call_order: list[int] = []
        for chunk in stream:
            if getattr(chunk, "usage", None):
                yield {
                    "type": "usage",
                    "input": chunk.usage.prompt_tokens or 0,
                    "output": chunk.usage.completion_tokens or 0,
                }
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                yield {"type": "text", "content": delta.content}
            if delta and getattr(delta, "tool_calls", None):
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in call_buf:
                        call_buf[idx] = {
                            "id": tc.id or "",
                            "name": "",
                            "arguments": "",
                        }
                        call_order.append(idx)
                    if tc.function:
                        if tc.function.name:
                            call_buf[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            call_buf[idx]["arguments"] += tc.function.arguments
        if call_buf:
            calls = []
            for idx in sorted(call_buf):
                c = call_buf[idx]
                # arguments fragments are raw json, join and (if needed) tidy
                args = c["arguments"]
                try:
                    json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = _tidy_json(args)
                calls.append({"id": c["id"], "name": c["name"], "arguments": args or "{}"})
            yield {"type": "tool_calls", "calls": calls}

    def close(self):
        self.client.close()

    def client_options(self) -> dict:
        """base_url / api_key for third-party OpenAI-compatible clients
        (Agents SDK, LangChain) to build on the same backend."""
        return {
            "base_url": self.cfg.base_url or None,
            "api_key": self.cfg.api_key,
        }


class OpenAICompatProvider(CompatProvider):
    kind = "openai"

    def __init__(self, cfg: ProviderConfig):
        cfg = ProviderConfig(
            kind="openai", model=cfg.model, base_url=cfg.base_url,
            api_key=cfg.api_key, temperature=cfg.temperature, extra=cfg.extra,
        )
        super().__init__(cfg)


class OllamaProvider(CompatProvider):
    kind = "ollama"

    def __init__(self, cfg: ProviderConfig):
        base = cfg.base_url or config.OLLAMA_BASE_URL or "http://localhost:11434/v1"
        # GPU: -1 = all layers on GPU, set via MODEL_EXTRA{"num_gpu":-1,...}
        cfg = ProviderConfig(
            kind="ollama", model=cfg.model, base_url=base,
            api_key="ollama", temperature=cfg.temperature, extra=cfg.extra,
        )
        super().__init__(cfg)


class KimiProvider(CompatProvider):
    """Moonshot AI (Kimi) - OpenAI-compatible chat completions."""

    kind = "kimi"

    def __init__(self, cfg: ProviderConfig):
        base = cfg.base_url or config.KIMI_BASE_URL or "https://api.moonshot.ai/v1"
        cfg = ProviderConfig(
            kind="kimi", model=cfg.model, base_url=base,
            api_key=config.KIMI_API_KEY, temperature=cfg.temperature, extra=cfg.extra,
        )
        super().__init__(cfg)


# --------------------------------------------------------------------------- #
# OpenAI Responses API (ChatGPT-subscription / fine-grained OAuth tokens)
# --------------------------------------------------------------------------- #
class ResponsesProvider:
    """Responses API streaming. Used for ChatGPT OAuth access tokens; also
    works fine with plain project API keys."""

    kind = "openai-responses"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.client = openai.OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url or None,
            timeout=120,
            max_retries=2,
        )

    def chat_stream(self, messages, tools=None) -> Iterator[dict]:
        body = {"chatgpt": True} if self.cfg.use_responses else {}
        body.update(self.cfg.extra)
        tool_schemas = [t["function"] for t in tools] if tools else None
        try:
            with self.client.responses.stream(
                model=self.cfg.model,
                input=messages,  # chat-completions-shaped input is accepted
                tools=tool_schemas,
                stream=True,
                temperature=self.cfg.temperature,
                extra_body=body,
            ) as stream:
                for event in stream:
                    if event.type == "response.output_text.delta":
                        yield {"type": "text", "content": event.delta}
                response = stream.get_final_response()
        except openai.OpenAIError as exc:
            raise ProviderError(str(exc)) from exc

        calls = []
        for item in response.output:
            if item.type == "function_call":
                calls.append(
                    {
                        "id": item.call_id,
                        "name": item.name,
                        "arguments": item.arguments or "{}",
                    }
                )
        if calls:
            yield {"type": "tool_calls", "calls": calls}
        usage = getattr(response, "usage", None)
        if usage:
            yield {
                "type": "usage",
                "input": getattr(usage, "input_tokens", 0) or 0,
                "output": getattr(usage, "output_tokens", 0) or 0,
            }

    def close(self):
        self.client.close()

    def client_options(self) -> dict:
        return {
            "base_url": self.cfg.base_url or None,
            "api_key": self.cfg.api_key,
        }


# --------------------------------------------------------------------------- #
# Local GPU via HuggingFace transformers (optional install)
# --------------------------------------------------------------------------- #
class TorchProvider:
    """Runs a local instruct model with transformers on the configured device
    (``cuda`` recommended, ``auto`` picks whatever is available). Includes a
    minimal JSON tool-call mode since most local instruct models have no native
    function-calling API during generate()."""

    kind = "torch"
    _models: dict = {}
    _lock = threading.Lock()

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.model, self.tokenizer, self.streamer = None, None, None
        self._device = cfg.extra.get("device_map", "auto")

    def _ensure_loaded(self):
        with self._lock:
            if self.model is not None:
                return
            try:
                import torch  # noqa: F401
                from transformers import (  # type: ignore
                    AutoModelForCausalLM,
                    AutoTokenizer,
                    TextIteratorStreamer,
                )
            except ImportError as exc:
                raise ProviderError(
                    "torch provider requires `pip install torch transformers "
                    "accelerate` inside the venv"
                ) from exc

            cfg = self.cfg.extra
            model_id = self.cfg.model
            kwargs = dict(
                device_map=cfg.get("device_map", "auto"),
                torch_dtype=cfg.get("dtype", "auto") or "auto",
            )
            self.tokenizer = AutoTokenizer.from_pretrained(model_id)
            self.model = AutoModelForCausalLM.from_pretrained(model_id, **kwargs)
            if not self.tokenizer.pad_token:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            self.streamer = TextIteratorStreamer(self.tokenizer, skip_prompt=True)
            self._max_new = int(cfg.get("max_new_tokens", 512))

    def _render(self, messages: list[dict]) -> str:
        try:
            return self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        except Exception:
            lines = []
            for m in messages:
                role = m["role"].replace("tool", "assistant")
                lines.append(f"{role}: {m.get('content') or ''}")
            return "\n".join(lines) + "\nassistant:"

    _TOOL_JSON = re.compile(r"\{[\s\S]*?\}", re.M)

    def chat_stream(self, messages, tools=None) -> Iterator[dict]:
        self._ensure_loaded()
        import threading as _t

        prompt = self._render(messages)
        if tools:
            prompt += (
                "\n\nIf you need to call a tool, reply with ONLY a JSON object of "
                f'this exact shape: {{"name": "<one of: {[t["function"]["name"] for t in tools]}>", '
                '"arguments": {<tool arguments as a JSON object>}}. Otherwise reply normally.'
            )

        done = {"flag": False}
        thread = _t.Thread(
            target=self._generate_worker, args=(prompt, done), daemon=True
        )
        thread.start()

        text_parts: list[str] = []
        for token in self.streamer:
            if done["flag"] and not token:
                break
            yield {"type": "text", "content": token}
            text_parts.append(token)
        thread.join(timeout=60)

        if tools:
            full = "".join(text_parts)
            for m in self._TOOL_JSON.finditer(full):
                try:
                    parsed = json.loads(m.group(0))
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and parsed.get("name"):
                    yield {
                        "type": "tool_calls",
                        "calls": [
                            {
                                "id": "local-call-1",
                                "name": parsed["name"],
                                "arguments": json.dumps(parsed.get("arguments", {})),
                            }
                        ],
                    }
                    break

    def _generate_worker(self, prompt: str, done: dict):
        try:
            from transformers import GenerationConfig

            gen_cfg = self.cfg.extra
            self.model.generate(
                **self.tokenizer(prompt, return_tensors="pt", padding=True).to(
                    self.model.device
                ),
                streamer=self.streamer,
                max_new_tokens=gen_cfg.get("max_new_tokens", 512),
                do_sample=bool(gen_cfg.get("do_sample", True)),
                temperature=self.cfg.temperature,
                top_p=float(gen_cfg.get("top_p", 0.95)),
                pad_token_id=self.tokenizer.eos_token_id,
                generation_config=GenerationConfig(),
            )
        except Exception as exc:  # pragma: no cover - surfaced via streamer
            self.streamer.stream.put_nowait("")
            raise
        finally:
            done["flag"] = True

    def close(self):
        self.model = None
        self.tokenizer = None

    def client_options(self) -> dict | None:
        return None  # no OpenAI-compatible client - RawLoop engine only


# --------------------------------------------------------------------------- #
def _tidy_json(fragment: str) -> str:
    """Patch naive JSON fragments (missing closing quotes/brackets) if possible."""
    for suffix in ('"}', '"}', "}", "]"):
        candidate = fragment.rstrip() + suffix
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            continue
    return "{}"


class ProviderManager:
    """Builds the right provider for a runtime provider selection."""

    def __init__(
        self,
        *,
        bearer: str | None,
        use_oauth_access: bool,
        runtime: dict | None = None,
    ):
        self.bearer = bearer
        self.use_oauth_access = use_oauth_access
        self.runtime = runtime or {}

    def config_for(self, provider_name: str, model: str | None = None) -> ProviderConfig:
        rt = self.runtime
        model = model or rt.get("model") or (config.CHATGPT_MODEL if self.use_oauth_access else config.DEFAULT_MODEL)
        temp = float(rt.get("temperature", 0.7))
        extra = dict(rt.get("model_extra") or {})
        if provider_name == "openai":
            use_resp = bool(self.use_oauth_access or rt.get("use_responses", False))
            return ProviderConfig(
                kind="openai",
                model=model,
                base_url=rt.get("base_url", "") or None,
                api_key=self.bearer or "",
                temperature=temp,
                use_responses=use_resp,
                extra=extra,
            )
        if provider_name == "ollama":
            return ProviderConfig(
                kind="ollama",
                model=model or rt.get("ollama_model") or "qwen2.5:7b",
                base_url=rt.get("ollama_base_url", "") or config.OLLAMA_BASE_URL,
                temperature=temp,
                extra=extra,
            )
        if provider_name == "kimi":
            return ProviderConfig(
                kind="kimi",
                model=model or rt.get("kimi_model") or config.KIMI_MODEL,
                base_url=rt.get("kimi_base_url", "") or config.KIMI_BASE_URL,
                api_key=config.KIMI_API_KEY,
                temperature=temp,
                extra=extra,
            )
        if provider_name == "torch":
            return ProviderConfig(
                kind="torch",
                model=model or rt.get("torch_model") or "Qwen/Qwen2.5-7B-Instruct",
                temperature=temp,
                extra={**config.TORCH_EXTRA, **extra},
            )
        raise ProviderError(f"Unknown provider: {provider_name}")

    def build(self, provider_name: str | None = None, model: str | None = None):
        name = (provider_name or self.runtime.get("provider") or config.PROVIDER_DEFAULT).lower()
        cfg = self.config_for(name, model)
        if name == "openai":
            # Bearer already covers the server OPENAI_API_KEY when configured
            # (bearer_for_api falls back to it), so an empty key here means
            # neither a server key nor a user OAuth token exists. Fail fast
            # with guidance instead of letting the SDK hit api.openai.com with
            # its placeholder key and surface a raw 401.
            if not self.bearer and not config.OPENAI_API_KEY:
                raise ProviderError(
                    "OpenAI provider selected but no API key is configured. "
                    "Set OPENAI_API_KEY (or sign in with OpenAI OAuth), or "
                    "switch to provider 'ollama' or 'torch' in the settings."
                )
            if cfg.use_responses:
                return ResponsesProvider(cfg)
            return OpenAICompatProvider(cfg)
        if name == "ollama":
            return OllamaProvider(cfg)
        if name == "kimi":
            if not config.KIMI_API_KEY:
                raise ProviderError(
                    "Kimi provider selected but KIMI_API_KEY is not set. "
                    "Add KIMI_API_KEY to backend/.env (Moonshot AI account)."
                )
            return KimiProvider(cfg)
        if name == "torch":
            try:
                import torch  # noqa: F401
                import transformers  # noqa: F401
            except ImportError:
                raise ProviderError(
                    "torch provider selected but torch/transformers are not "
                    "installed (or install them with: pip install torch transformers)."
                ) from None
            return TorchProvider(cfg)
        raise ProviderError(f"Unknown provider: {name}")


def list_ollama_models() -> list[str]:
    """Best-effort list of models served by the local Ollama server."""
    try:
        import requests

        base = config.OLLAMA_BASE_URL
        url = base.replace("/v1", "").rstrip("/") + "/api/tags"
        resp = requests.get(url, timeout=3)
        resp.raise_for_status()
        return sorted(m["name"] for m in resp.json().get("models", []))
    except Exception:
        return []


def list_kimi_models() -> list[str]:
    """Kimi model ids - env-customizable via KIMI_MODELS."""
    return list(config.KIMI_MODELS)