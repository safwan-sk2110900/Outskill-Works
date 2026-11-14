
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import base64
import io
import json
import logging
import mimetypes
import os
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("quiz-backend")

BASE_DIR = Path(__file__).parent
QUIZ_DIR = BASE_DIR / "quizzes"        # generated quiz JSON
RESPONSE_DIR = BASE_DIR / "responses"  # user answers
QUIZ_DIR.mkdir(exist_ok=True)
RESPONSE_DIR.mkdir(exist_ok=True)

MODEL = os.getenv("AI_MODEL", "gemini-3.8-flash")
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 60_000
ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")

QUIZ_SYSTEM_PROMPT = """You create multiple-choice quizzes from source material.
Return ONLY a JSON object, with no markdown and no commentary, in exactly this shape:
{
  "title": "short quiz title",
  "questions": [
    {"question": "...", "options": ["text", "text", "text", "text"], "correct_index": 0}
  ]
}
Rules:
- Exactly 10 questions.
- Exactly 4 options per question, and only one is correct.
- correct_index is the 0-based position (0 to 3) of the correct option.
- Do not prefix options with letters like "A)".
- Base every question only on the provided material.
- Vary which position holds the correct answer.
"""



openai_client: Optional[AsyncOpenAI] = None

# --- Data Models ---
class QuizQuestion(BaseModel):
    question: str = Field(..., min_length=1)
    options: List[str] = Field(..., min_length=4, max_length=4)
    correct_index: int = Field(..., ge=0, le=3)


class Quiz(BaseModel):
    id: str = ""
    title: str = Field(..., min_length=1)
    source_filename: str = ""
    created_at: str = ""
    questions: List[QuizQuestion] = Field(..., min_length=10, max_length=10)


class AnswersRequest(BaseModel):
    answers: Dict[int, int]  # question index -> chosen option index




def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_id(quiz_id: str) -> str:
    if not ID_PATTERN.fullmatch(quiz_id):
        raise HTTPException(status_code=404, detail="Quiz not found.")
    return quiz_id


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")  # write then rename, so a crash can't leave half a file
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_quiz(quiz_id: str) -> dict:
    path = QUIZ_DIR / f"{check_id(quiz_id)}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Quiz not found.")
    return read_json(path)


def load_response(quiz_id: str) -> Optional[dict]:
    path = RESPONSE_DIR / f"{quiz_id}.json"
    return read_json(path) if path.exists() else None


def get_status(resp: Optional[dict]) -> str:
    if not resp:
        return "not_started"
    if resp.get("completed"):
        return "complete"
    return "incomplete" if resp.get("answers") else "not_started"


def summarize(quiz: dict, resp: Optional[dict]) -> dict:
    status_ = get_status(resp)
    return {
        "id": quiz["id"],
        "title": quiz["title"],
        "source_filename": quiz["source_filename"],
        "created_at": quiz["created_at"],
        "status": status_,
        "answered": len(resp["answers"]) if resp else 0,
        "total": len(quiz["questions"]),
        "score": resp.get("score") if status_ == "complete" else None,
    }


def check_answers(quiz: dict, answers: Dict[int, int], require_all: bool) -> None:
    total = len(quiz["questions"])
    for q_idx, choice in answers.items():
        if not (0 <= q_idx < total) or not (0 <= choice <= 3):
            raise HTTPException(status_code=422, detail="Invalid answer data.")
    if require_all and len(answers) != total:
        raise HTTPException(status_code=400, detail=f"Answer all {total} questions before submitting.")


def save_response(quiz_id: str, answers: Dict[int, int], completed=False, score=None) -> dict:
    data = {
        "quiz_id": quiz_id,
        "answers": {str(k): v for k, v in answers.items()},  # JSON keys must be strings
        "completed": completed,
        "score": score,
        "updated_at": now_iso(),
    }
    write_json(RESPONSE_DIR / f"{quiz_id}.json", data)
    return data

def extract_content(filename: str, data: bytes) -> tuple[str, Optional[str]]:
    """Return (text, image_data_url). Exactly one is useful."""
    ext = Path(filename).suffix.lower()

    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages), None

    if ext == ".docx":
        from docx import Document
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs), None

    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        return "", f"data:{mime};base64,{base64.b64encode(data).decode()}"

    try:  # .txt, .md, .csv, .json, source code, and so on
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=415,
            detail="Unsupported file type. Use text, code, PDF, Word (.docx) or image files.",
        )

def parse_json_loose(raw: str) -> dict:
    raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw)  # strip ```json fences if present
    return json.loads(raw)


async def generate_quiz(text: str, image_url: Optional[str], filename: str) -> Quiz:
    content = [{
        "type": "text",
        "text": f"Create the quiz from this material (file: {filename}).\n\n{text[:MAX_TEXT_CHARS]}",
    }]
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}})

    last_error = ""
    for attempt in range(2):  # one retry if the AI returns bad JSON
        response = await openai_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": QUIZ_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            temperature=0.4,
            max_tokens=8192,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
        try:
            return Quiz(**parse_json_loose(raw))
        except ValueError as e:  # covers bad JSON and failed validation
            last_error = str(e)
            logger.warning("Quiz attempt %d invalid: %s", attempt + 1, last_error[:200])

    raise HTTPException(status_code=502, detail="The AI didn't return a valid quiz. Try again.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global openai_client

    # Choose provider: Defaults to Google AI Studio's OpenAI compatibility layer
    # To route through OpenRouter:
    # Set BASE_URL="https://openrouter.ai/api/v1" and API_KEY to your OpenRouter key.
    base_url = os.getenv(
        "AI_BASE_URL",
        #"https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    api_key = os.getenv("AI_API_KEY") 
    #or os.getenv("GEMINI_API_KEY")

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
    title="OpenAI-Compatible Backend",
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
        "http://localhost:5173"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)






@app.post("/api/quizzes", status_code=201)
async def create_quiz(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is too large (15 MB max).")

    filename = file.filename or "upload"
    try:
        text, image_url = extract_content(filename, data)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Could not read %s", filename)
        raise HTTPException(status_code=422, detail="Couldn't read that file.")

    if not text.strip() and not image_url:
        raise HTTPException(status_code=422, detail="No readable content found in that file.")

    try:
        quiz = await generate_quiz(text, image_url, filename)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("AI request failed: %s", e)
        raise HTTPException(status_code=502, detail=f"AI request failed: {e}")

    quiz.id = uuid.uuid4().hex
    quiz.source_filename = filename
    quiz.created_at = now_iso()
    write_json(QUIZ_DIR / f"{quiz.id}.json", quiz.model_dump())
    return summarize(quiz.model_dump(), None)


@app.get("/api/quizzes")
async def list_quizzes():
    items = []
    for path in QUIZ_DIR.glob("*.json"):
        try:
            quiz = read_json(path)
            items.append(summarize(quiz, load_response(quiz["id"])))
        except Exception:
            logger.warning("Skipping unreadable quiz file: %s", path.name)
    items.sort(key=lambda q: q["created_at"], reverse=True)
    return items


@app.get("/api/quizzes/{quiz_id}")
async def get_quiz(quiz_id: str):
    quiz = load_quiz(quiz_id)
    resp = load_response(quiz_id)
    info = summarize(quiz, resp)
    answers = {int(k): v for k, v in (resp or {}).get("answers", {}).items()}

    questions = []
    for i, q in enumerate(quiz["questions"]):
        item = {"question": q["question"], "options": q["options"]}
        if info["status"] == "complete":  # the only place correct answers are revealed
            item["correct_index"] = q["correct_index"]
            item["is_correct"] = answers.get(i) == q["correct_index"]
        questions.append(item)

    return {**info, "questions": questions, "answers": answers}


@app.put("/api/quizzes/{quiz_id}/progress")
async def save_progress(quiz_id: str, body: AnswersRequest):
    quiz = load_quiz(quiz_id)
    if get_status(load_response(quiz_id)) == "complete":
        raise HTTPException(status_code=409, detail="This quiz was already submitted.")
    check_answers(quiz, body.answers, require_all=False)
    resp = save_response(quiz_id, body.answers)
    return summarize(quiz, resp)


@app.post("/api/quizzes/{quiz_id}/submit")
async def submit_quiz(quiz_id: str, body: AnswersRequest):
    quiz = load_quiz(quiz_id)
    if get_status(load_response(quiz_id)) == "complete":
        raise HTTPException(status_code=409, detail="This quiz was already submitted.")
    check_answers(quiz, body.answers, require_all=True)
    score = sum(
        1 for i, q in enumerate(quiz["questions"]) if body.answers.get(i) == q["correct_index"]
    )
    resp = save_response(quiz_id, body.answers, completed=True, score=score)
    return summarize(quiz, resp)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend:app", host="127.0.0.1", port=8000, reload=True)