"""Inspect metadata-only retrieval over the quarantined sample corpus.

This probe does not execute skills, call a model, or write routing/quality
scores to the MarkeD catalog. Labels are engineering smoke checks only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from marked.core import _topical_score  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index", type=Path, default=ROOT / ".marked" / "corpus" / "openai-skills" / "index.json"
    )
    parser.add_argument(
        "--queries", type=Path, default=ROOT / "examples" / "sample_corpus_queries.json"
    )
    args = parser.parse_args()
    corpus = json.loads(args.index.read_text(encoding="utf-8"))
    queries = json.loads(args.queries.read_text(encoding="utf-8"))
    results = []
    for case in queries:
        ranked = []
        for item in corpus["items"]:
            row = {
                "title": item["name"],
                "description": item["description"],
                "tags_json": "[]",
                "stages_json": "[]",
            }
            score = _topical_score(case["task"], row, "general")
            if score > 0:
                ranked.append({"name": item["name"], "score": round(score, 3)})
        ranked.sort(key=lambda entry: (-entry["score"], entry["name"]))
        top1 = ranked[0]["name"] if ranked else None
        results.append({
            "case": case["id"],
            "expected": case["expected"],
            "top1": top1,
            "top3": ranked[:3],
            "matches_smoke_label": top1 == case["expected"],
        })
    print(json.dumps({
        "corpus_commit": corpus["commit"],
        "probe": "metadata-only lexical routing; no execution or quality evaluation",
        "total": len(results),
        "smoke_label_matches": sum(result["matches_smoke_label"] for result in results),
        "cases": results,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
