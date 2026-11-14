from __future__ import annotations

import os
from typing import Any


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-4o-mini"
API_KEY = "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"
APP_TITLE = "Day 4 Vision Chat"


def resolve_api_key(api_key: str | None = None) -> str:
    """Return an API key from the function argument or environment."""
    api_key = (api_key or os.getenv("OPENROUTER_API_KEY") or API_KEY).strip()

    if not api_key:
        raise ValueError(
            "Set `OPENROUTER_API_KEY` in your environment or paste it in the API key box."
        )

    return api_key


def create_openrouter_client(api_key: str) -> Any:
    """Create an OpenAI SDK client configured for OpenRouter."""
    from openai import OpenAI

    return OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            default_headers={
                "HTTP-Referer": "http://localhost:7860",
                "X-Title": APP_TITLE,
            },
        )


def build_text_messages(
    user_prompt: str,
    history: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Convert prior chat history plus the new prompt into OpenRouter messages."""
    messages: list[dict[str, str]] = []

    for item in history or []:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content.strip()})
    messages.append(
        {
            "role": "user",
            "content": user_prompt.strip(),
        }
    )

    
    return messages


def ask_text_model(
    prompt: str,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    history: list[dict[str, Any]] | None = None,
) -> str:
    """Send one text-only chat request and return the assistant response."""
    client = create_openrouter_client(resolve_api_key(api_key))

    response = client.chat.completions.create(
        model=model,
        messages=build_text_messages(prompt, history),
        extra_body={"provider": {"data_collection": "deny"}},
    )

    return response.choices[0].message.content or ""


if __name__ == "__main__":
    print(ask_text_model("Explain Gradio in one sentence."))
