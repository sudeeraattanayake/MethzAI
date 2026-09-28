from tools import IMAGE_DIR
from rag import UPLOAD_DIR, add_document_to_rag, vectorstre
from database import (
    ChatMessage,
    Conversation,
    LongTermMemory,
    SessionLocal,
    create_or_update_conversation,
    get_chat_history,
    init_db,
    list_conversations,
    save_chat_message,
)
from agent import ALLOWED_MODELS, DEFAULT_MODEL, get_agent
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from uuid import uuid4
import json
import logging
import os
import re
import shutil
import unicodedata

import certifi
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()


logger = logging.getLogger("methzai")

DATA_DIR = BASE_DIR / "data"
IMAGE_INDEX = DATA_DIR / "web_image_index.json"
STORED_PREFIX = "__METHZAI_WEB_V1__:"

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".py", ".csv"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

ACTIVE_THREADS = set()
ACTIVE_LOCK = Lock()
FILES_LOCK = Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    init_db()
    yield


app = FastAPI(title="MethzAI", lifespan=lifespan)

templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates")
)


class ChatRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=30000)
    model: str = DEFAULT_MODEL
    attachments: list[str] = Field(default_factory=list, max_length=20)


def validate_thread(thread_id):
    if not isinstance(thread_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,100}", thread_id
    ):
        raise HTTPException(400, "Invalid conversation ID.")

    return thread_id


def begin_operation(thread_id):
    with ACTIVE_LOCK:
        if thread_id in ACTIVE_THREADS:
            raise HTTPException(
                409,
                "This conversation is still processing a request. Please wait.",
            )

        ACTIVE_THREADS.add(thread_id)


def finish_operation(thread_id):
    with ACTIVE_LOCK:
        ACTIVE_THREADS.discard(thread_id)


def text_content(content):
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict)
            and item.get("type") in ("text", "output_text")
        )

    return ""


def json_content(content):
    if isinstance(content, dict):
        return content

    try:
        data = json.loads(text_content(content))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def stored_message(content, **metadata):
    return STORED_PREFIX + json.dumps(
        {"content": content, **metadata},
        ensure_ascii=False,
    )


def decode_message(message):
    result = {
        "role": message.role,
        "content": message.content or "",
    }

    if result["content"].startswith(STORED_PREFIX):
        try:
            data = json.loads(result["content"][len(STORED_PREFIX):])

            result.update({
                key: value
                for key, value in data.items()
                if key in (
                    "content",
                    "images",
                    "attachments",
                    "model",
                    "memory_updated",
                )
            })
        except (ValueError, AttributeError):
            pass

    return result


def read_image_index():
    if not IMAGE_INDEX.exists():
        return {}

    return json.loads(IMAGE_INDEX.read_text(encoding="utf-8"))


def write_image_index(index):
    temporary = IMAGE_INDEX.with_suffix(".tmp")
    temporary.write_text(json.dumps(index), encoding="utf-8")
    temporary.replace(IMAGE_INDEX)


def register_image(thread_id, result):
    path = Path(result.get("image_path", "")).resolve()

    if (
        path.parent != IMAGE_DIR.resolve()
        or not path.is_file()
        or not re.fullmatch(r"[a-f0-9]{32}\.png", path.name)
    ):
        raise ValueError("The image tool returned an invalid path.")

    with FILES_LOCK:
        index = read_image_index()
        index[path.name] = thread_id
        write_image_index(index)

    return {
        "url": "/api/images/" + path.name,
        "filename": path.name,
        "alt": "Image generated by MethzAI",
    }


def tool_activity(name):
    labels = {
        "generate_image": ("image", "Creating your image…"),
        "remember_this": ("memory", "Updating memory…"),
        "recall_memory": ("memory", "Recalling saved memories…"),
        "search_uploaded_documents": (
            "documents",
            "Searching your documents…",
        ),
        "calculator": ("calculating", "Calculating…"),
    }

    if name in labels:
        return labels[name]

    if "tavily" in name or "search" in name:
        return "search", "Searching the web…"

    return "tool", "Using a tool…"


def public_error(error):
    code = getattr(error, "status_code", None)

    if code in (401, 403):
        return (
            "The API denied access. Check your API key, "
            "billing, and model permissions."
        )

    if code == 429:
        return (
            "An API rate or usage limit was reached. "
            "Check your quota and billing."
        )

    if code == 404:
        return (
            "The selected API model was not found "
            "or is unavailable to this account."
        )

    if isinstance(error, ValueError) and str(error).startswith(
        "The current conversation turn"
    ):
        return str(error)

    return (
        "The request could not finish. "
        "Check the Python terminal for the exact error."
    )


@app.middleware("http")
async def same_origin_requests(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")

        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse(
                {"error": "Cross-origin request rejected."},
                status_code=403,
            )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"

    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"

    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException):
    return JSONResponse(
        {"error": str(error.detail)},
        status_code=error.status_code,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    return JSONResponse(
        {
            "error": (
                "Invalid request. Check the message, "
                "thread ID, and file fields."
            )
        },
        status_code=422,
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    logger.error(
        "Request failed: %s",
        request.url.path,
        exc_info=(type(error), error, error.__traceback__),
    )

    return JSONResponse(
        {"error": public_error(error)},
        status_code=500,
    )


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={},
    )


@app.get("/logo")
def logo():
    path = BASE_DIR / "logo.png"

    if not path.is_file():
        raise HTTPException(404, "Logo not found.")

    return FileResponse(path, media_type="image/png")


@app.get("/api/models")
def models():
    return {
        "models": [DEFAULT_MODEL] + sorted(
            ALLOWED_MODELS - {DEFAULT_MODEL}
        ),
        "default_model": DEFAULT_MODEL,
    }


@app.get("/api/conversations")
def conversations():
    return {
        "conversations": [
            {
                "thread_id": conversation.thread_id,
                "title": conversation.title,
                "updated_at": (
                    conversation.updated_at.isoformat() + "Z"
                    if conversation.updated_at
                    else ""
                ),
            }
            for conversation in list_conversations()
        ]
    }


@app.get("/api/conversations/{thread_id}/messages")
def messages(thread_id: str):
    validate_thread(thread_id)

    return {
        "messages": [
            decode_message(message)
            for message in get_chat_history(thread_id)
        ]
    }


@app.get("/api/documents")
def documents(thread_id: str):
    validate_thread(thread_id)
    directory = UPLOAD_DIR / thread_id

    return {
        "documents": [
            {
                "filename": path.name,
                "size": path.stat().st_size,
            }
            for path in sorted(directory.glob("*"))
            if path.is_file() and not path.name.startswith(".")
        ]
    }


@app.get("/api/memories")
def memories(thread_id: str):
    validate_thread(thread_id)

    with SessionLocal() as db:
        items = (
            db.query(LongTermMemory)
            .filter(LongTermMemory.thread_id == thread_id)
            .order_by(
                LongTermMemory.created_at.desc(),
                LongTermMemory.id.desc(),
            )
            .all()
        )

        return {
            "memories": [
                {"id": item.id, "memory": item.memory}
                for item in items
            ]
        }


@app.post("/api/upload")
def upload(
    thread_id: str = Form(...),
    files: list[UploadFile] = File(...),
):
    validate_thread(thread_id)

    if not 1 <= len(files) <= 5:
        raise HTTPException(400, "Upload between 1 and 5 files.")

    begin_operation(thread_id)
    saved, errors = [], []

    try:
        create_or_update_conversation(thread_id)

        directory = UPLOAD_DIR / thread_id
        directory.mkdir(parents=True, exist_ok=True)

        for file in files:
            original = Path(
                (file.filename or "").replace("\\", "/")
            ).name

            suffix = Path(original).suffix.lower()

            if suffix not in ALLOWED_EXTENSIONS:
                errors.append(
                    "Unsupported file: " + (original or "unnamed")
                )
                continue

            stem = unicodedata.normalize(
                "NFKC",
                Path(original).stem,
            )
            stem = (
                re.sub(r"[^\w .-]", "_", stem).strip(" .")[:80]
                or "document"
            )

            name = uuid4().hex[:8] + "_" + stem + suffix
            path = directory / name

            payload = file.file.read(MAX_UPLOAD_BYTES + 1)

            if len(payload) > MAX_UPLOAD_BYTES:
                errors.append(original + ": exceeds 10 MB.")
                continue

            try:
                path.write_bytes(payload)

                result = add_document_to_rag(
                    str(path),
                    thread_id,
                )

                saved.append({
                    "filename": name,
                    "chunks": result["chunks"],
                })

            except Exception:
                logger.exception("Document indexing failed")

                try:
                    vectorstre.delete(
                        where={
                            "$and": [
                                {"thread_id": {"$eq": thread_id}},
                                {"source": {"$eq": name}},
                            ]
                        }
                    )
                except Exception:
                    logger.exception(
                        "Could not clean up partial document indexing"
                    )

                path.unlink(missing_ok=True)
                errors.append(
                    original + ": could not be indexed. Check the terminal."
                )

        return {"files": saved, "errors": errors}

    finally:
        for file in files:
            file.file.close()

        finish_operation(thread_id)


@app.get("/api/images/{filename}")
def image(filename: str, download: bool = False):
    if not re.fullmatch(r"[a-f0-9]{32}\.png", filename):
        raise HTTPException(404, "Image not found.")

    with FILES_LOCK:
        known = filename in read_image_index()

    path = IMAGE_DIR / filename

    if not known or not path.is_file():
        raise HTTPException(404, "Image not found.")

    return FileResponse(
        path,
        media_type="image/png",
        filename=filename,
        content_disposition_type="attachment" if download else "inline",
    )


@app.post("/api/chat")
def chat(body: ChatRequest):
    thread_id = validate_thread(body.thread_id)
    message = body.message.strip()

    if not message:
        raise HTTPException(400, "Enter a message.")

    if body.model not in ALLOWED_MODELS:
        raise HTTPException(400, "Choose a supported model.")

    attachments = [
        name
        for name in body.attachments
        if name == Path(name).name
        and (UPLOAD_DIR / thread_id / name).is_file()
    ]

    begin_operation(thread_id)

    events = Queue(maxsize=256)
    disconnected = Event()

    def emit(kind, **data):
        while not disconnected.is_set():
            try:
                events.put(
                    {"type": kind, **data},
                    timeout=0.5,
                )
                return
            except Full:
                continue

    def run_agent():
        text_parts, images = [], []
        memory_updated = False
        error_message = None
        warning = None

        try:
            emit("status", stage="thinking", label="Thinking…")

            create_or_update_conversation(thread_id, message)

            save_chat_message(
                thread_id,
                "user",
                stored_message(message, attachments=attachments),
            )

            graph = get_agent(body.model)

            writing = False
            emitted_text_in_turn = False

            config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 30,
            }

            for part in graph.stream(
                {"messages": [HumanMessage(content=message)]},
                config=config,
                stream_mode=["messages", "updates"],
            ):
                if (
                    isinstance(part, dict)
                    and "type" in part
                    and "data" in part
                ):
                    mode, payload = part["type"], part["data"]
                else:
                    mode, payload = part

                if mode == "messages":
                    chunk, metadata = payload

                    if (
                        metadata.get("langgraph_node") != "chatbot"
                        or not isinstance(chunk, (AIMessage, AIMessageChunk))
                    ):
                        continue

                    text = text_content(chunk.content)

                    if text:
                        if not writing:
                            emit(
                                "status",
                                stage="writing",
                                label="Writing…",
                            )
                            writing = True

                        emitted_text_in_turn = True
                        text_parts.append(text)
                        emit("token", text=text)

                elif mode == "updates":
                    for node, update in payload.items():
                        if not isinstance(update, dict):
                            continue

                        for msg in update.get("messages", []):
                            if node == "chatbot":
                                text = text_content(msg.content)

                                if text and not emitted_text_in_turn:
                                    text_parts.append(text)
                                    emit("token", text=text)

                                emitted_text_in_turn = False

                                for call in (
                                    getattr(msg, "tool_calls", []) or []
                                ):
                                    writing = False
                                    stage, label = tool_activity(
                                        call.get("name", "")
                                    )

                                    emit(
                                        "status",
                                        stage=stage,
                                        label=label,
                                    )

                            elif node == "tools":
                                name = getattr(msg, "name", "") or ""
                                result = json_content(msg.content)
                                raw = text_content(msg.content)

                                failed = (
                                    getattr(msg, "status", None) == "error"
                                    or result.get("status") == "error"
                                    or raw.lower().startswith(
                                        ("error", "calculation error")
                                    )
                                )

                                if failed:
                                    warning = result.get("message") or (
                                        "A tool could not finish. "
                                        "Check the terminal."
                                    )
                                    emit("tool_error", message=warning)

                                elif (
                                    name == "remember_this"
                                    and "saved successfully" in raw.lower()
                                ):
                                    memory_updated = True
                                    emit(
                                        "memory",
                                        message="Memory updated",
                                    )

                                elif name == "generate_image":
                                    if result.get("status") != "success":
                                        warning = (
                                            "Image generation returned "
                                            "no successful result."
                                        )
                                        emit(
                                            "tool_error",
                                            message=warning,
                                        )
                                    else:
                                        asset = register_image(
                                            thread_id,
                                            result,
                                        )
                                        images.append(asset)
                                        emit("image", **asset)

        except Exception as error:
            logger.exception("Agent response failed")
            error_message = public_error(error)

        finally:
            try:
                content = "".join(text_parts)

                if error_message:
                    content += (
                        ("\n\n" if content else "")
                        + "[Request failed: "
                        + error_message
                        + "]"
                    )

                save_chat_message(
                    thread_id,
                    "assistant",
                    stored_message(
                        content,
                        images=images,
                        model=body.model,
                        memory_updated=memory_updated,
                    ),
                )

            except Exception:
                logger.exception("Could not save assistant response")
                error_message = error_message or (
                    "The response could not be saved. "
                    "Check the terminal."
                )

            finish_operation(thread_id)

            if error_message:
                emit("error", message=error_message)
            else:
                emit("done", warning=warning)

            emit("_finished")

    def stream_events():
        try:
            while True:
                try:
                    event = events.get(timeout=15)
                except Empty:
                    yield json.dumps({"type": "heartbeat"}) + "\n"
                    continue

                if event["type"] == "_finished":
                    break

                yield json.dumps(event, ensure_ascii=False) + "\n"

        finally:
            # An external request may still finish and save its response.
            disconnected.set()

    try:
        Thread(
            target=run_agent,
            daemon=True,
            name="methzai-chat",
        ).start()
    except Exception:
        finish_operation(thread_id)
        raise

    return StreamingResponse(
        stream_events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/api/conversations/{thread_id}")
def delete_conversation(thread_id: str):
    validate_thread(thread_id)
    begin_operation(thread_id)

    try:
        graph = get_agent(DEFAULT_MODEL)
        graph.checkpointer.delete_thread(thread_id)

        vectorstre.delete(where={"thread_id": thread_id})

        with SessionLocal.begin() as db:
            for model in (
                ChatMessage,
                LongTermMemory,
                Conversation,
            ):
                (
                    db.query(model)
                    .filter(model.thread_id == thread_id)
                    .delete(synchronize_session=False)
                )

        shutil.rmtree(
            UPLOAD_DIR / thread_id,
            ignore_errors=True,
        )

        with FILES_LOCK:
            index = read_image_index()

            for filename, owner in list(index.items()):
                if owner == thread_id:
                    if re.fullmatch(r"[a-f0-9]{32}\.png", filename):
                        (IMAGE_DIR / filename).unlink(missing_ok=True)

                    del index[filename]

            write_image_index(index)

        return {"ok": True}

    finally:
        finish_operation(thread_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Keep one worker for the in-process locks and image index.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8080,
    )
