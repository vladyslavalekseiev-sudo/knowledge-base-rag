"""Fault injection uses temporary files and an explicitly simulated Ollama server."""

import asyncio
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from rag.answer import Assistant, Ollama, validate_excerpts
from rag.app import create_app
from rag.documents import Passage, chunk_section, load_passages, slug
from rag.middleware import MAX_REQUEST_BYTES, HTTPProtections
from rag.search import Index, build_index, scope_terms, terms

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "How long do webhook retries continue?"
PASSAGE = Passage("test/retries/1", "Webhooks", "Delivery retries", "Webhook retries stop after 24 hours.")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("KB_OLLAMA_MODEL", raising=False)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(ROOT / "knowledge", tmp_path / "index.db")) as client:
        yield client


@pytest.mark.parametrize("body", [{}, {"question": None}, {"question": 12}, {"question": []},
                                  {"question": {}}, {"question": "xx"}, {"question": "  "},
                                  {"question": "x" * 361}, {"question": QUESTION, "extra": True}])
def test_invalid_input(client, body):
    assert client.post("/api/ask", json=body).status_code == 422


@pytest.mark.parametrize("question", ["abc", "x" * 360, "  " + QUESTION + "  ",
                                      "你好世界", "\x00\x00\x00", "\" OR *; DROP TABLE passages; --",
                                      "<script>alert(1)</script>"])
def test_safe_input_boundaries(client, question):
    response = client.post("/api/ask", json={"question": question})
    assert response.status_code == 200
    assert response.json()["status"] in {"answered", "insufficient_evidence"}
    assert client.post("/api/ask", json={"question": QUESTION}).json()["status"] == "answered"


def test_http_negative_paths(client):
    assert client.get("/api/ask").status_code == 405
    assert client.post("/api/ask", content="{", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/ask", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/ask", content=b"\xff", headers={"Content-Type": "application/octet-stream"}).status_code == 415
    assert client.post("/api/ask", content=b"", headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post("/api/ask", json={"question": QUESTION}, headers={"Content-Type": "application/vnd.relay+json"}).status_code == 200
    for path in ["/missing", "/assets/../rag/app.py", "/.env", "/api/sources/' OR 1=1 --"]:
        assert client.get(path).status_code == 404
    assert client.get("/openapi.json").json()["paths"]["/api/ask"]["post"]["responses"]["200"]
    assert client.get("/docs").status_code == 200


def test_request_body_is_bounded_before_json_validation(client):
    response = client.post("/api/ask", json={"question": "x" * 100_000})
    assert response.status_code == 413
    assert len(response.content) < 300


def test_streamed_request_cannot_bypass_size_limit(client):
    def body():
        yield b'{"question":"'
        for _ in range(12):
            yield b"x" * 1024
        yield b'"}'
    assert client.post("/api/ask", content=body(), headers={"Content-Type": "application/json"}).status_code == 413


def test_browser_security_headers(client):
    response = client.get("/")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "access-control-allow-origin" not in client.get("/api/documents", headers={"Origin": "https://evil.invalid"}).headers


@pytest.mark.parametrize("damage", ["missing", "corrupt", "schema"])
def test_database_failure_is_reported_as_unavailable(tmp_path, damage):
    database = tmp_path / "index.db"
    with TestClient(create_app(ROOT / "knowledge", database), raise_server_exceptions=False) as client:
        if damage == "missing":
            database.unlink()
        elif damage == "corrupt":
            database.write_bytes(b"not a sqlite database")
        else:
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("DROP TABLE passages")
        assert client.get("/api/health").status_code == 503
        result = client.post("/api/ask", json={"question": QUESTION})
        assert result.status_code == 503
        assert str(tmp_path) not in result.text
        assert client.get("/api/sources/test/retries/1").status_code == 503


@pytest.mark.parametrize("contents", ["", "no title", "# Title\ncontent outside section", "# Title\n## Empty",
                                      "# Title\n## !!!\nText", "# Title\n## A\nText\n## A!\nText", "# Title"])
def test_invalid_documents_are_rejected(tmp_path, contents):
    (tmp_path / "valid.md").write_text("# Valid\n## Section\nUseful text.", encoding="utf-8")
    (tmp_path / "invalid.md").write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        load_passages(tmp_path)


def test_duplicate_document_ids_and_non_english_identifiers(tmp_path):
    for name in ("one!.md", "one.md"):
        (tmp_path / name).write_text("# Title\n## Section\nText.", encoding="utf-8")
    with pytest.raises(ValueError, match="document identifier"):
        load_passages(tmp_path)
    assert slug("Hello, World!") == "hello-world"
    assert slug("你好") == ""


def test_missing_and_empty_corpus(tmp_path):
    for directory in (tmp_path, tmp_path / "missing"):
        with pytest.raises(ValueError, match="No knowledge passages"):
            load_passages(directory)


@pytest.mark.parametrize("count", [0, 1, 169, 170, 171, 340, 341, 5000])
def test_chunks_preserve_words(count):
    words = [f"token{number}" + ("." if number % 13 == 0 else "") for number in range(count)]
    chunks = chunk_section(" \n ".join(words))
    assert " ".join(chunks).split() == words
    assert all(0 < len(chunk.split()) <= 170 for chunk in chunks)


def test_persistence_atomic_failure_and_rebuild(tmp_path):
    path = tmp_path / "index.db"
    build_index(path, [PASSAGE])
    assert Index(path).source(PASSAGE.id) == PASSAGE
    original = path.read_bytes()
    with pytest.raises(AttributeError):
        build_index(path, [PASSAGE, None])  # Deliberate failure midway through the transaction.
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index.db"]
    replacement = Passage(PASSAGE.id, "New title", "Delivery retries", "Webhook retries stop after 48 hours.")
    build_index(path, [replacement])
    assert Index(path).source(PASSAGE.id) == replacement
    assert Index(path).source("' OR 1=1 --") is None


def test_restart_recovers_corrupt_derived_database(tmp_path):
    database = tmp_path / "index.db"
    database.write_bytes(b"broken")
    for _ in range(2):
        with TestClient(create_app(ROOT / "knowledge", database)) as client:
            assert client.post("/api/ask", json={"question": QUESTION}).json()["status"] == "answered"


def test_concurrent_readers_are_deterministic(tmp_path):
    path = tmp_path / "index.db"
    build_index(path, load_passages(ROOT / "knowledge"))
    assistant = Assistant(Index(path))
    with ThreadPoolExecutor(max_workers=16) as pool:
        answers = list(pool.map(assistant.ask, [QUESTION] * 160))
    assert all(answer == answers[0] and answer.citations for answer in answers)


@pytest.mark.parametrize("question", ["", "?;?", "Who can download invoices? How long do webhook retries continue? Where are EU backups stored? Who can reset MFA?",
                                      "annual refunds", "How long is audit history retained on Starter?",
                                      "Where are US workspace backups stored?"])
def test_retrieval_refuses_unsupported_input(tmp_path, question):
    path = tmp_path / "index.db"
    build_index(path, load_passages(ROOT / "knowledge"))
    assert Assistant(Index(path)).ask(question).citations == []


def test_scope_and_normalization():
    assert terms("US") == {"usa"}
    assert terms("retries retrying retried") == {"retry"}
    assert {"starter", "365", "refund"} <= scope_terms("Starter plan refund within 365 days", terms("Starter plan refund within 365 days"))


@pytest.mark.parametrize("origin", ["localhost:11434", "file:///tmp/model", "https://", "http://user:secret@host", "http://host?key=secret", "http://host#section"])
def test_invalid_model_configuration_fails_early(origin):
    with pytest.raises(ValueError, match="KB_OLLAMA_URL"):
        Ollama("unused", origin)


def test_empty_index_is_not_ready(tmp_path):
    path = tmp_path / "empty.db"
    build_index(path, [])
    with pytest.raises(sqlite3.DatabaseError, match="empty"):
        Index(path).check_ready()


def test_fragmented_body_and_disconnect():
    async def scenario(messages):
        received, sent = [], []

        async def downstream(scope, receive, send):
            received.append(await receive())

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        await HTTPProtections(downstream)({"type": "http", "method": "POST", "path": "/api/ask"}, receive, send)
        return received, sent

    received, sent = asyncio.run(scenario([
        {"type": "http.request", "body": b" " * 4000, "more_body": True},
        {"type": "http.request", "body": b" " * (MAX_REQUEST_BYTES - 4002) + b"{}", "more_body": False},
    ]))
    assert len(received[0]["body"]) == MAX_REQUEST_BYTES and not sent
    assert asyncio.run(scenario([{"type": "http.disconnect"}])) == ([], [])


@pytest.mark.parametrize("body", [b"[" * 1500 + b"]" * 1500, b"[" * 70 + b"]" * 70,
                                 b'{"question":"\\ud800\\ud800\\ud800"}', b'{"question":NaN}',
                                 b'{"question":Infinity}', b'{"question":1e400}', b'{"question":"\xff"}'],
                         ids=["decoder-depth", "nesting-limit", "surrogates", "nan", "infinity", "overflow", "utf8"])
def test_hostile_json_does_not_crash_or_echo_input(client, body):
    response = client.post("/api/ask", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert len(response.content) < 300
    assert client.post("/api/ask", json={"question": QUESTION}).status_code == 200


@pytest.mark.parametrize("raw", [None, {}, True, [None], [{}], [{"source": True, "first": 1, "last": 1}],
                                [{"source": 0, "first": 1, "last": 1}], [{"source": 1, "first": 2, "last": 1}],
                                [{"source": 1, "first": 1, "last": 99}],
                                [{"source": 1, "first": 1, "last": 1}] * 2])
def test_model_schema_rejects_malformed_ranges(raw):
    assert validate_excerpts(raw, [PASSAGE], [[PASSAGE.text]], QUESTION, {PASSAGE.id: set()}) is None


def test_model_schema_preserves_refusal_and_source_order():
    assert validate_excerpts([], [PASSAGE], [[PASSAGE.text]], QUESTION, {}) == []
    other = Passage("other/1", "Other", "Webhook retries", "Webhook retries continue.")
    ranges = [{"source": 2, "first": 1, "last": 1}, {"source": 1, "first": 1, "last": 1}]
    result = validate_excerpts(ranges, [PASSAGE, other], [[PASSAGE.text], [other.text]], QUESTION, {PASSAGE.id: set(), other.id: set()})
    assert [citation.source.id for citation in result] == [PASSAGE.id, other.id]
    ranges[0]["source"] = 1
    assert validate_excerpts(ranges, [PASSAGE, other], [[PASSAGE.text], [other.text]], QUESTION, {PASSAGE.id: set(), other.id: set()}) is None


@contextmanager
def faulty_model(payload=b"{}", status=200, truncate=False):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload) + (100 if truncate else 0)))
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=3)


@pytest.mark.parametrize(("payload", "status", "truncate"), [
    (b"not json", 200, False), (b"[]", 200, False), (b"{}", 500, False),
    (b'{}', 200, True), (b"x" * 70_000, 200, False),
    (b'{"message":{"content":"not json"}}', 200, False),
    (b'{"message":{"content":"{}"}}', 200, False),
    (b'{"message":{"content":"{\\"excerpts\\":[]}"}}', 200, False),
    (b"[" * 1500 + b"]" * 1500, 200, False),
], ids=["invalid-json", "wrong-type", "http-500", "truncated", "oversized", "invalid-content", "missing-excerpts", "refusal", "decoder-depth"])
def test_real_http_model_failure_contract(tmp_path, payload, status, truncate):
    path = tmp_path / "index.db"
    build_index(path, [PASSAGE])
    with faulty_model(payload, status, truncate) as origin:
        result = Assistant(Index(path), Ollama("simulated-fault-server", origin)).ask(QUESTION)
    if b"excerpts" in payload:
        assert result.citations == []
    else:
        assert result.mode == "extractive" and result.citations[0].quote == PASSAGE.text


def test_real_model_connection_refused_falls_back(tmp_path):
    path = tmp_path / "index.db"
    build_index(path, [PASSAGE])
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    result = Assistant(Index(path), Ollama("unavailable", f"http://127.0.0.1:{port}")).ask(QUESTION)
    assert result.mode == "extractive" and result.citations


def test_model_overload_uses_immediate_fallback():
    """A real blocking HTTP service verifies the per-instance admission limit."""
    ready, release, lock = Event(), Event(), Lock()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            with lock:
                calls.append(1)
                if len(calls) == 2:
                    ready.set()
            release.wait(timeout=5)
            body = b'{"message":{"content":"{\\"excerpts\\":[]}"}}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        model = Ollama("simulated-slow-server", f"http://127.0.0.1:{server.server_port}")
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(model.narrow, QUESTION, [PASSAGE], {PASSAGE.id: set()}) for _ in range(2)]
            try:
                assert ready.wait(timeout=3)
                clock = perf_counter()
                result = model.narrow(QUESTION, [PASSAGE], {PASSAGE.id: set()})
                elapsed = perf_counter() - clock
            finally:
                release.set()
                for task in pending:
                    task.result(timeout=3)
                saturated_calls = len(calls)
                retry = model.narrow(QUESTION, [PASSAGE], {PASSAGE.id: set()})
                server.shutdown()
                thread.join(timeout=3)
        assert result is None and elapsed < 0.5
        assert saturated_calls == 2
        assert retry == [] and len(calls) == 3
