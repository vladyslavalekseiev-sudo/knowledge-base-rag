import re
from dataclasses import dataclass
from pathlib import Path

MAX_CHUNK_WORDS = 170
SENTENCES = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Passage:
    id: str
    document: str
    section: str
    text: str


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def chunk_section(text: str) -> list[str]:
    """Pack sentences, with a word limit even for documents without punctuation."""
    chunks: list[str] = []
    current: list[str] = []
    for sentence in SENTENCES.split(text):
        words = sentence.split()
        if current and len(current) + len(words) > MAX_CHUNK_WORDS:
            chunks.append(" ".join(current))
            current = []
        while len(words) > MAX_CHUNK_WORDS:
            chunks.append(" ".join(words[:MAX_CHUNK_WORDS]))
            words = words[MAX_CHUNK_WORDS:]
        current.extend(words)
    if current:
        chunks.append(" ".join(current))
    return chunks


def load_passages(directory: Path) -> list[Passage]:
    passages: list[Passage] = []
    documents: set[str] = set()
    for file in sorted(directory.glob("*.md")):
        lines = file.read_text(encoding="utf-8-sig").strip().splitlines()
        if not lines or not lines[0].startswith("# ") or not lines[0][2:].strip():
            raise ValueError(f"{file.name} must start with a document title.")
        document = lines[0][2:].strip()
        document_id = slug(file.stem)
        if not document_id or document_id in documents:
            raise ValueError(f"Invalid or duplicate document identifier: {file.name}.")
        documents.add(document_id)
        sections: dict[str, tuple[str, list[str]]] = {}
        current: list[str] | None = None
        for line in lines[1:]:
            if line.startswith("## "):
                section = line[3:].strip()
                section_id = slug(section)
                if not section_id or section_id in sections:
                    raise ValueError(f"Invalid or duplicate section in {file.name}: {section!r}.")
                current = []
                sections[section_id] = (section, current)
            elif current is not None:
                current.append(line)
            elif line.strip():
                raise ValueError(f"Place content in a section in {file.name}.")
        for section_id, (section, paragraphs) in sections.items():
            chunks = chunk_section(" ".join(" ".join(paragraphs).split()))
            if not chunks:
                raise ValueError(f"Empty section in {file.name}: {section!r}.")
            for position, chunk in enumerate(chunks, 1):
                passage_id = f"{document_id}/{section_id}/{position}"
                passages.append(Passage(passage_id, document, section, chunk))
    if not passages:
        raise ValueError(f"No knowledge passages found in {directory}.")
    return passages
