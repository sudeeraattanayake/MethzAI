from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    DateTime,
    case
)
from sqlalchemy.engine import URL
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = URL.create(
    "sqlite",
    database=str(DATA_DIR / "chatbot_memory.db")
)

engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,
        "timeout": 30
    }
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False
)

Base = declarative_base()


def utc_now():
    # Preserve the existing database's naive UTC timestamp format.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def validate_thread_id(thread_id: str):
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("A valid thread_id is required.")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(String, unique=True, index=True)
    title = Column(String, default="New Chat")
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(String, index=True)
    role = Column(String)
    content = Column(Text)
    created_at = Column(DateTime, default=utc_now)


class LongTermMemory(Base):
    __tablename__ = "long_term_memory"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(String, index=True)
    memory = Column(Text)
    created_at = Column(DateTime, default=utc_now)


def init_db():
    Base.metadata.create_all(bind=engine)


def _upsert_conversation(
    db,
    thread_id: str,
    first_message: str | None = None
):
    now = utc_now()
    text = first_message.strip() if first_message else ""

    title = "New Chat"

    if text:
        title = text[:40]
        if len(text) > 40:
            title += "..."

    statement = insert(Conversation).values(
        thread_id=thread_id,
        title=title,
        created_at=now,
        updated_at=now
    )

    updates = {"updated_at": now}

    if text:
        updates["title"] = case(
            (Conversation.title == "New Chat", title),
            else_=Conversation.title
        )

    statement = statement.on_conflict_do_update(
        index_elements=["thread_id"],
        set_=updates
    )

    db.execute(statement)


def create_or_update_conversation(
    thread_id: str,
    first_message: str | None = None
):
    validate_thread_id(thread_id)

    if first_message is not None and not isinstance(first_message, str):
        raise ValueError("first_message must be a string or None.")

    with SessionLocal.begin() as db:
        _upsert_conversation(db, thread_id, first_message)


def list_conversations():
    with SessionLocal() as db:
        return (
            db.query(Conversation)
            .order_by(
                Conversation.updated_at.desc(),
                Conversation.id.desc()
            )
            .all()
        )


def save_chat_message(
    thread_id: str,
    role: str,
    content: str
):
    validate_thread_id(thread_id)

    if not isinstance(role, str) or not role.strip():
        raise ValueError("A message role is required.")

    if not isinstance(content, str):
        raise ValueError("Message content must be a string.")

    role = role.strip()

    with SessionLocal.begin() as db:
        _upsert_conversation(
            db,
            thread_id,
            content if role == "user" else None
        )

        db.add(
            ChatMessage(
                thread_id=thread_id,
                role=role,
                content=content,
                created_at=utc_now()
            )
        )


def get_chat_history(thread_id: str):
    validate_thread_id(thread_id)

    with SessionLocal() as db:
        return (
            db.query(ChatMessage)
            .filter(ChatMessage.thread_id == thread_id)
            .order_by(
                ChatMessage.created_at.asc(),
                ChatMessage.id.asc()
            )
            .all()
        )


def save_memory(thread_id: str, memory: str):
    validate_thread_id(thread_id)

    if not isinstance(memory, str) or not memory.strip():
        raise ValueError("Memory cannot be empty.")

    with SessionLocal.begin() as db:
        _upsert_conversation(db, thread_id)

        db.add(
            LongTermMemory(
                thread_id=thread_id,
                memory=memory.strip(),
                created_at=utc_now()
            )
        )

    return "Memory saved successfully."


def search_memory(thread_id: str, query: str):
    validate_thread_id(thread_id)

    if not isinstance(query, str):
        raise ValueError("Memory search query must be a string.")

    query = query.strip()

    with SessionLocal() as db:
        memories_query = (
            db.query(LongTermMemory)
            .filter(LongTermMemory.thread_id == thread_id)
        )

        if query:
            memories_query = memories_query.filter(
                LongTermMemory.memory.contains(
                    query,
                    autoescape=True
                )
            )

        memories = (
            memories_query
            .order_by(
                LongTermMemory.created_at.desc(),
                LongTermMemory.id.desc()
            )
            .limit(20)
            .all()
        )

        if not memories:
            return "No saved memory found."

        return "\n".join(
            f"- {item.memory}"
            for item in memories
        )
