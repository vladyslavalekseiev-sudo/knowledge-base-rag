import re
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

from .documents import Passage

WORDS = re.compile(r"[a-z0-9]+")
QUALIFIERS = re.compile(r"\b([a-z0-9-]+)\s+(?:plans?|regions?|workspaces?)\b", re.I)
PROPER_TERMS = re.compile(r"\b(?:[A-Z][a-z]+|[A-Z]{2,})\b")
# A nearby procedure does not establish that a distinct policy action is supported.
POLICY_ACTIONS = {"appeal", "cancel", "chargeback", "deactivation", "dispute", "refund", "rotate"}
STOP_WORDS = frozenset("""
    a about all an and any are as at be been by can could did do does for from had has have
    how i if in into is it its me most my of on or our please s should so some than that
    the their them then there these they this to under us was we were what when where which
    who will with would you your should tell say according after before during once only
    must need help get long often much many soon someone another remains relayops
""".split())
CONCEPTS = {
    "sso": "saml", "login": "signin", "signon": "signin",
    "2fa": "authenticator", "mfa": "authenticator",
    "twofactor": "authenticator", "provision": "provisioning",
    "deactivate": "deactivation", "deactivated": "deactivation",
    "retry": "retry", "retries": "retry", "retried": "retry", "retrying": "retry",
    "retained": "retention", "retains": "retention", "retain": "retention",
    "retaining": "retention", "retention": "retention",
    "downloaded": "download", "downloads": "download",
    "changing": "change", "changed": "change", "changes": "change",
    "expires": "expire", "expired": "expire", "expiration": "expire", "expiry": "expire",
    "rotated": "rotate", "rotation": "rotate", "rotating": "rotate",
    "deleted": "deletion", "delete": "deletion", "deleting": "deletion",
    "storage": "store", "stored": "store", "stores": "store",
    "oncall": "oncall", "posts": "post", "posted": "post",
    "publishes": "publish", "published": "publish", "publishing": "publish",
    "disables": "deactivation", "disable": "deactivation",
    "finished": "resolution", "resolved": "resolution",
}
TERM_VARIANTS: dict[str, set[str]] = {}
for word, concept in CONCEPTS.items():
    TERM_VARIANTS.setdefault(concept, set()).add(word)


def terms(text: str) -> set[str]:
    text = re.sub(r"\bUS\b", "usa", text).lower().replace("sign-in", "signin").replace("sign-on", "signon")
    text = text.replace("two-factor", "twofactor").replace("on-call", "oncall")
    result = set()
    for word in WORDS.findall(text):
        if word in STOP_WORDS:
            continue
        if word in CONCEPTS:
            word = CONCEPTS[word]
        elif word.endswith("s") and len(word) > 3 and not word.endswith(("ss", "us", "ies")):
            word = word[:-1]
        result.add(word)
    return result


def scope_terms(question: str, query: set[str]) -> set[str]:
    # A passage about a different plan or region is not evidence for the requested scope.
    modifiers = " ".join(QUALIFIERS.findall(question))
    names = " ".join(PROPER_TERMS.findall(question))
    return (terms(modifiers + " " + names) & query) | (POLICY_ACTIONS & query) | {
        word for word in query if word.isdecimal()
    }


def build_index(path: Path, passages: list[Passage]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".sqlite3", delete=False) as file:
        temp = Path(file.name)
    try:
        with closing(sqlite3.connect(temp)) as db, db:
            db.execute("""CREATE VIRTUAL TABLE passages USING fts5(
                id UNINDEXED, document, section, text, concepts, tokenize = 'porter unicode61'
            )""")
            db.executemany(
                "INSERT INTO passages VALUES (?, ?, ?, ?, ?)",
                ((p.id, p.document, p.section, p.text, " ".join(sorted(terms(p.section + " " + p.text))))
                 for p in passages),
            )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class Index:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)

    def source(self, passage_id: str) -> Passage | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT id, document, section, text FROM passages WHERE id = ?", (passage_id,)
            ).fetchone()
        return Passage(*row) if row else None

    def find(self, question: str) -> Passage | None:
        query = terms(question)
        if len(query) < 2:
            return None
        expanded = query | {variant for term in query for variant in TERM_VARIANTS.get(term, ())}
        match = " OR ".join(f'"{word}"' for word in sorted(expanded))
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT id, document, section, text, concepts FROM passages
                   WHERE passages MATCH ?
                   ORDER BY bm25(passages, 0, 0.6, 2.5, 1, 0.4), id
                   LIMIT 12""",
                (match,),
            ).fetchall()
        required = scope_terms(question, query)
        best: Passage | None = None
        best_score = 0.0
        for passage_id, document, section, text, concepts in rows:
            available = set(concepts.split())
            overlap = query & available
            coverage = len(overlap) / len(query)
            if len(overlap) < 2 or coverage < 0.6 or not required <= available:
                continue
            score = coverage + 0.12 * len(query & terms(section)) / len(query)
            if score > best_score:
                best_score = score
                best = Passage(passage_id, document, section, text)
        return best
