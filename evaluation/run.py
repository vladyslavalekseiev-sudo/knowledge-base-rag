import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from rag.answer import Assistant, Ollama
from rag.documents import load_passages
from rag.search import Index, build_index

ROOT = Path(__file__).resolve().parents[1]


def evaluate(engine: Assistant, cases: list[dict]) -> dict:
    samples = []
    categories: dict[str, Counter] = {}
    source_hits = source_predictions = source_expected = refusals = correctly_refused = 0
    elapsed = []
    for case in cases:
        clock = perf_counter()
        result = engine.ask(case["question"])
        elapsed.append((perf_counter() - clock) * 1000)
        expected = set(case["sources"])
        actual = {citation.source.id for citation in result.citations}
        quotes = "\n".join(citation.quote for citation in result.citations).casefold()
        content_correct = all(part.casefold() in quotes for part in case["includes"])
        passed = expected == actual and content_correct
        categories.setdefault(case["group"], Counter()).update(total=1, passed=int(passed))
        source_hits += len(expected & actual)
        source_predictions += len(actual)
        source_expected += len(expected)
        refusals += int(not expected)
        correctly_refused += int(not expected and not actual)
        samples.append({
            "id": case["id"], "group": case["group"], "question": case["question"],
            "expected": sorted(expected), "actual": sorted(actual), "passed": passed,
            "missing_answer_text": [part for part in case["includes"] if part.casefold() not in quotes],
            "mode": result.mode if result.citations else None,
        })
    return {
        "cases": len(samples),
        "passed": sum(sample["passed"] for sample in samples),
        "correct_refusals": correctly_refused,
        "refusal_cases": refusals,
        "source_precision": round(source_hits / max(1, source_predictions), 3),
        "source_recall": round(source_hits / max(1, source_expected), 3),
        "p50_ms": round(median(elapsed), 2) if elapsed else None,
        "groups": categories,
        "results": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the knowledge base benchmark.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cases", type=Path, default=ROOT / "evaluation" / "questions.json")
    parser.add_argument("--model", help="Ollama model to evaluate. Omit for the offline baseline.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    with TemporaryDirectory() as temporary:
        db = Path(temporary) / "kb.sqlite3"
        build_index(db, load_passages(ROOT / "knowledge"))
        engine = Assistant(Index(db), Ollama(args.model, args.ollama_url) if args.model else None)
        report = {"configured_model": args.model, **evaluate(engine, cases)}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    summary = f"{report['passed']}/{report['cases']} cases passed"
    print(f"{summary}, {report['correct_refusals']}/{report['refusal_cases']} correct refusals")
    for sample in report["results"]:
        if not sample["passed"]:
            print(sample["id"], sample["expected"], sample["actual"], sample["missing_answer_text"])
    return int(report["passed"] != report["cases"])


if __name__ == "__main__":
    raise SystemExit(main())
