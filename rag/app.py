import os
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StringConstraints

from .answer import INSUFFICIENT, Assistant, Ollama
from .documents import Passage, load_passages
from .search import Index, build_index

ROOT = Path(__file__).resolve().parents[1]


class Ask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=360)]


class SourceRef(BaseModel):
    id: str
    document: str
    section: str
    url: str


class CitedExcerpt(SourceRef):
    quote: str


class SourceExcerpt(SourceRef):
    text: str


class AskResult(BaseModel):
    status: Literal["answered", "insufficient_evidence"]
    answer: str
    citations: list[CitedExcerpt]
    mode: Literal["extractive", "ollama"] | None


def source_ref(passage: Passage) -> SourceRef:
    return SourceRef(
        id=passage.id,
        document=passage.document,
        section=passage.section,
        url="/?source=" + quote(passage.id, safe=""),
    )


def create_app(knowledge: Path | None = None, database: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        directory = knowledge or Path(os.getenv("KB_DOCUMENTS", ROOT / "knowledge"))
        db = database or Path(os.getenv("KB_DATABASE", ROOT / ".data" / "knowledge.sqlite3"))
        passages = load_passages(directory)
        build_index(db, passages)
        model_name = os.getenv("KB_OLLAMA_MODEL", "").strip()
        model = Ollama(model_name, os.getenv("KB_OLLAMA_URL", "http://127.0.0.1:11434")) if model_name else None
        app.state.assistant = Assistant(Index(db), model)
        documents = Counter((p.id.split("/", 1)[0], p.document) for p in passages)
        app.state.documents = [
            {"id": identifier, "title": title, "passages": count}
            for (identifier, title), count in documents.items()
        ]
        app.state.stats = {"documents": len(documents), "passages": len(passages)}
        yield

    app = FastAPI(title="Relay Knowledge API", lifespan=lifespan)
    app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(ROOT / "web" / "index.html")

    @app.get("/api/health")
    def health(request: Request):
        return {"status": "ready", **request.app.state.stats}

    @app.get("/api/documents")
    def documents(request: Request):
        return request.app.state.documents

    @app.post("/api/ask")
    def ask(data: Ask, request: Request) -> AskResult:
        result = request.app.state.assistant.ask(data.question)
        if not result.citations:
            return AskResult(status="insufficient_evidence", answer=INSUFFICIENT, citations=[], mode=None)
        citations = [
            CitedExcerpt(**source_ref(citation.source).model_dump(), quote=citation.quote)
            for citation in result.citations
        ]
        answer = "\n\n".join(f"{citation.quote} [{number}]" for number, citation in enumerate(citations, 1))
        return AskResult(status="answered", answer=answer, citations=citations, mode=result.mode)

    @app.get("/api/sources/{passage_id:path}")
    def source(passage_id: str, request: Request) -> SourceExcerpt:
        passage = request.app.state.assistant.index.source(passage_id)
        if passage is None:
            raise HTTPException(404, "Source passage not found.")
        return SourceExcerpt(**source_ref(passage).model_dump(), text=passage.text)

    return app


app = create_app()
