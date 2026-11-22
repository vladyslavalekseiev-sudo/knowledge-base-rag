import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from rag.app import create_app
from rag.documents import MAX_CHUNK_WORDS, chunk_section

ROOT = Path(__file__).resolve().parents[1]
RETRY_ID = "webhooks/delivery-retries/1"


@pytest.fixture(autouse=True)
def offline_default(monkeypatch):
    monkeypatch.delenv("KB_OLLAMA_MODEL", raising=False)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(ROOT / "knowledge", tmp_path / "kb.sqlite3")) as browser:
        yield browser


def test_ask_cites_a_passage_that_can_be_opened_independently(client):
    assert client.get("/api/health").json() == {"status": "ready", "documents": 5, "passages": 19}
    assert client.get("/").status_code == 200
    assert "function showSource" in client.get("/assets/app.js").text
    result = client.post("/api/ask", json={"question": "How long do webhook retries continue?"}).json()
    assert result["status"] == "answered"
    assert "24 hours" in result["answer"]
    [citation] = result["citations"]
    assert citation["id"] == RETRY_ID
    assert client.get(citation["url"]).status_code == 200
    assert citation["quote"] in client.get("/api/sources/" + citation["id"]).json()["text"]


def test_requires_evidence_for_each_part_and_scope(client):
    questions = [
        "Who can download invoices? Can we pay with cryptocurrency?",
        "How long is audit history retained on the starter plan?",
        "Where are australian workspace backups stored?",
        "How long do I have to dispute a failed card payment?",
    ]
    for question in questions:
        result = client.post("/api/ask", json={"question": question}).json()
        assert result["status"] == "insufficient_evidence"
        assert result["citations"] == []
    supported = client.post("/api/ask", json={
        "question": "Who can download invoices, and when does a guest invitation expire?"
    }).json()
    assert {citation["id"] for citation in supported["citations"]} == {
        "billing/download-invoices/1", "access/guest-invitations/1"
    }


def test_empty_or_oversized_questions_and_unknown_sources_are_rejected(client):
    for question in ("  ", "x" * 361):
        assert client.post("/api/ask", json={"question": question}).status_code == 422
    assert client.get("/api/sources/missing/document/1").status_code == 404


def test_chunking_preserves_words_and_bounds_unpunctuated_sections():
    words = [f"word{i}" for i in range(2 * MAX_CHUNK_WORDS + 13)]
    chunks = chunk_section(" ".join(words))
    assert " ".join(chunks).split() == words
    assert all(len(chunk.split()) <= MAX_CHUNK_WORDS for chunk in chunks)


@pytest.fixture
def ollama(monkeypatch):
    received = []
    response = {"excerpts": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, data))
            payload = json.dumps({"message": {"content": json.dumps(response)}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("KB_OLLAMA_MODEL", "test-model")
        monkeypatch.setenv("KB_OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
        try:
            yield response, received
        finally:
            server.shutdown()
            thread.join(timeout=3)


def test_ollama_http_contract_and_grounding(ollama, tmp_path):
    response, received = ollama
    with TestClient(create_app(ROOT / "knowledge", tmp_path / "kb.sqlite3")) as browser:
        response["excerpts"] = [{"source": 1, "first": 2, "last": 2}]
        result = browser.post("/api/ask", json={"question": "How long do webhook retries continue?"}).json()
        assert result["mode"] == "ollama"
        assert result["answer"] == "Retries stop 24 hours after the initial delivery. [1]"
        path, payload = received[-1]
        assert path == "/api/chat" and payload["model"] == "test-model"
        context = json.loads(payload["messages"][1]["content"])["passages"][0]
        assert context["source"] == 1
        assert context["sentences"][1] == {"number": 2, "text": "Retries stop 24 hours after the initial delivery."}

        response["excerpts"] = [{"source": 1, "first": 1, "last": 1}]
        scoped = browser.post("/api/ask", json={"question": "How long is audit history retained on Team?"}).json()
        assert scoped["mode"] == "extractive"
        assert "Team workspaces retain audit history for 90 days." in scoped["answer"]

        response["excerpts"] = [{"source": 1, "first": 3, "last": 3}]
        clipped = browser.post("/api/ask", json={"question": "Can guests change workspace settings?"}).json()
        assert clipped["mode"] == "ollama"
        assert "cannot change workspace settings" in clipped["answer"]

        response["excerpts"] = [{"source": 1, "first": 9, "last": 9}]
        unsupported = browser.post("/api/ask", json={"question": "How long do webhook retries continue?"}).json()
        assert unsupported["mode"] == "extractive"
        assert "30 days" not in unsupported["answer"]

        response["excerpts"] = []
        refusal = browser.post("/api/ask", json={"question": "How long do webhook retries continue?"}).json()
        assert refusal["status"] == "insufficient_evidence"
