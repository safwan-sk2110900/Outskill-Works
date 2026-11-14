

from contextlib import asynccontextmanager
import json
import logging
import os
from typing import AsyncGenerator, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("openai-compatible-backend")

openai_client: Optional[AsyncOpenAI] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global openai_client

    # Choose provider: Defaults to Google AI Studio's OpenAI compatibility layer
    # To route through OpenRouter:
    # Set BASE_URL="https://openrouter.ai/api/v1" and API_KEY to your OpenRouter key.
    base_url = os.getenv(
        "AI_BASE_URL"
        # "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    api_key = os.getenv("AI_API_KEY") 
    # or os.getenv("GEMINI_API_KEY")

    if not api_key:
        logger.warning(
            "No API key provided. Set AI_API_KEY or GEMINI_API_KEY in .env."
        )

    openai_client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
    )
    logger.info(f"Initialized AsyncOpenAI client pointing to: {base_url}")
    yield
    await openai_client.close()
    logger.info("Closed AsyncOpenAI client.")


app = FastAPI(
    title="OpenAI-Compatible Chat Backend",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configuration allowing Tauri webviews
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "tauri://localhost",
        "https://tauri.localhost",
        "http://localhost:1420",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Data Models ---
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1)


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    # For Google direct: "gemini-2.5-flash", "gemini-2.5-pro"
    # For OpenRouter: "google/gemini-2.5-flash", "openai/gpt-4o", etc.
    model: str = Field(default="gemini-2.5-flash")
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=2048, ge=1)


# --- Streaming Generator ---
async def sse_openai_generator(
    request: ChatRequest,
) -> AsyncGenerator[str, None]:
    if not openai_client:
        yield f"event: error\ndata: {json.dumps({'error': 'Client not initialized'})}\n\n"
        return

    formatted_messages = [
        {"role": m.role, "content": m.content} for m in request.messages
    ]

    try:
        stream = await openai_client.chat.completions.create(
            model=request.model,
            messages=formatted_messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=True,
        )

        async for chunk in stream:
            if chunk.choices and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta.content
                if delta:
                    payload = json.dumps({"delta": delta})
                    yield f"data: {payload}\n\n"

        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"OpenAI completion error: {e}")
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest):
    if not request.messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Messages list cannot be empty.",
        )
    return StreamingResponse(
        sse_openai_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend:app", host="127.0.0.1", port=8000, reload=True)