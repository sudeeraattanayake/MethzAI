import docx2txt
from pypdf import PdfReader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from pathlib import Path
from typing import List
from dotenv import load_dotenv
import os
import certifi

load_dotenv()

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
CHROMA_DIR = BASE_DIR / "chroma_db"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

embeddings = OpenAIEmbeddings(
    model="text-embedding-3-small"
)

vectorstre = Chroma(
    collection_name="agentic_chatbot_docs",
    embedding_function=embeddings,
    persist_directory=str(CHROMA_DIR)
)


def read_file_text(file_path: str) -> str:
    path = Path(file_path)

    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path.name}")

    suffix = path.suffix.lower()

    if suffix == ".pdf":
        reader = PdfReader(str(path))
        pages = [
            page.extract_text() or ""
            for page in reader.pages
        ]
        return "\n".join(pages)

    if suffix == ".docx":
        return docx2txt.process(str(path)) or ""

    if suffix in {".txt", ".md", ".py", ".csv"}:
        return path.read_text(
            encoding="utf-8",
            errors="ignore"
        )

    raise ValueError(
        "Unsupported file type. Upload PDF, DOCX, TXT, MD, PY, or CSV."
    )


def add_document_to_rag(file_path: str, thread_id: str):
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("A valid thread_id is required.")

    text = read_file_text(file_path)

    if not text.strip():
        raise ValueError(
            "No text could be extracted. Scanned PDFs may require OCR."
        )

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=900,
        chunk_overlap=150
    )

    chunks = splitter.split_text(text)
    filename = Path(file_path).name

    docs: List[Document] = [
        Document(
            page_content=chunk,
            metadata={
                "thread_id": thread_id,
                "source": filename
            }
        )
        for chunk in chunks
    ]

    vectorstre.add_documents(docs)

    return {
        "filename": filename,
        "chunks": len(docs)
    }


def retrieve_from_rag(
    query: str,
    thread_id: str,
    k: int = 4
) -> str:
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("A valid thread_id is required.")

    query = query.strip()

    if not query:
        return "Please provide a document search query."

    if type(k) is not int or k < 1:
        raise ValueError("k must be a positive integer.")

    docs = vectorstre.similarity_search(
        query,
        k=k,
        filter={"thread_id": thread_id}
    )

    if not docs:
        return "No relevant uploaded document content found."

    results = []

    for i, doc in enumerate(docs, start=1):
        source = doc.metadata.get("source", "uploaded document")
        results.append(
            f"[Source {i}: {source}]\n{doc.page_content}"
        )

    return "\n\n".join(results)
