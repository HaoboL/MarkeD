"""Fetch a pinned, quarantined Skill sample for offline routing checks.

This script never installs, executes, imports, admits, or activates a Skill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = "openai/skills"
COMMIT = "49f948faa9258a0c61caceaf225e179651397431"
SKILLS = (
    "cli-creator",
    "gh-address-comments",
    "gh-fix-ci",
    "jupyter-notebook",
    "security-best-practices",
    "security-ownership-map",
    "security-threat-model",
    "screenshot",
)
MAX_FILE_BYTES = 128 * 1024
ROOT = Path(__file__).resolve().parents[1]


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "MarkeD-sample-corpus/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(MAX_FILE_BYTES + 1)
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"missing or oversized source file: {url}")
    raw.decode("utf-8")
    return raw


def _metadata(markdown: str) -> dict[str, str]:
    if not markdown.startswith("---\n"):
        raise ValueError("SKILL.md has no YAML frontmatter")
    end = markdown.find("\n---\n", 4)
    if end < 0:
        raise ValueError("SKILL.md has unclosed YAML frontmatter")
    frontmatter = markdown[4:end]
    name = re.search(r"(?m)^name:\s*['\"]?([^\n'\"]+)", frontmatter)
    description = re.search(r"(?m)^description:\s*(.*)$", frontmatter)
    if not name or not description:
        raise ValueError("SKILL.md frontmatter needs name and description")
    summary = description.group(1).strip().strip("'\"")
    if summary in {"|", ">", "|-", ">-", ""}:
        tail = frontmatter[description.end():]
        continuation = []
        for line in tail.splitlines()[1:]:
            if line and not line[0].isspace():
                break
            if line.strip():
                continuation.append(line.strip())
        summary = " ".join(continuation)
    return {"name": name.group(1).strip(), "description": summary}


def fetch_corpus(output: Path) -> dict[str, object]:
    base = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/skills/.curated"
    staged = []
    for slug in SKILLS:
        source = f"{base}/{slug}"
        skill = _fetch(f"{source}/SKILL.md")
        license_text = _fetch(f"{source}/LICENSE.txt")
        if b"Apache License" not in license_text or b"Version 2.0" not in license_text:
            raise ValueError(f"{slug}: license is not confirmed Apache-2.0")
        metadata = _metadata(skill.decode("utf-8"))
        if metadata["name"] != slug:
            raise ValueError(f"{slug}: frontmatter name mismatch")
        staged.append((slug, skill, license_text, metadata))

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    items = []
    for slug, skill, license_text, metadata in staged:
        folder = output / slug
        folder.mkdir(exist_ok=True)
        for filename, data in (("SKILL.md", skill), ("LICENSE.txt", license_text)):
            target = folder / filename
            if target.exists() and target.read_bytes() != data:
                raise ValueError(f"refusing to overwrite changed sample: {target}")
            target.write_bytes(data)
        items.append({
            "name": metadata["name"],
            "description": metadata["description"],
            "license": "Apache-2.0",
            "source": f"https://github.com/{REPO}/tree/{COMMIT}/skills/.curated/{slug}",
            "skill_sha256": hashlib.sha256(skill).hexdigest(),
            "license_sha256": hashlib.sha256(license_text).hexdigest(),
            "status": "quarantine-metadata-only",
            "package_complete": False,
            "reason": "Supporting scripts, references, assets, and permissions were not fetched or reviewed.",
        })
    index = {
        "repository": REPO,
        "commit": COMMIT,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": "offline routing sample; no task execution or quality claim",
        "items": items,
    }
    (output / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".marked" / "corpus" / "openai-skills"
    )
    args = parser.parse_args()
    try:
        index = fetch_corpus(args.output)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        parser.exit(2, f"sample corpus fetch failed: {exc}\n")
    print(json.dumps({
        "path": str(args.output.resolve()),
        "commit": COMMIT,
        "skills": len(index["items"]),
        "status": "quarantine-metadata-only",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
