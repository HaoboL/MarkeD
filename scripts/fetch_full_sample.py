"""Fetch one pinned upstream Skill tree into quarantine; never execute it."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fetch_sample_corpus import COMMIT, REPO

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 4 * 1024 * 1024


def fetch_bytes(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "MarkeD-pinned-sample/0.1"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = response.read(limit + 1)
            break
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) or attempt == 2:
                raise
            time.sleep(attempt + 1)
    if len(result) > limit:
        raise ValueError("source response exceeds byte limit")
    return result


def blob_sha(raw: bytes) -> str:
    return hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()


def fetch(skill: str = "gh-fix-ci") -> dict[str, object]:
    metadata = json.loads((ROOT / ".marked/corpus/openai-skills/index.json").read_text(encoding="utf-8"))
    if metadata["commit"] != COMMIT:
        raise ValueError("metadata corpus has a different commit")
    original = next((item for item in metadata["items"] if item["name"] == skill), None)
    if original is None:
        raise ValueError("skill is not in the pinned metadata sample")
    prefix = f"skills/.curated/{skill}/"
    output = ROOT / ".marked" / "corpus" / "openai-full" / skill
    tree_url = f"https://api.github.com/repos/{REPO}/git/trees/{COMMIT}?recursive=1"
    tree = json.loads(fetch_bytes(tree_url, MAX_BYTES))
    if tree.get("truncated"):
        raise ValueError("Git tree response was truncated")
    entries = sorted(
        (item for item in tree["tree"] if item["type"] == "blob"
         and item["path"].startswith(prefix)),
        key=lambda item: item["path"],
    )
    paths = [item["path"][len(prefix):] for item in entries]
    if not entries or len(entries) > 65 or not {"SKILL.md", "LICENSE.txt"} <= set(paths):
        raise ValueError("main/license missing or file count exceeds importer limit")
    if output.exists():
        raise ValueError(f"destination already exists: {output}")
    staged: list[tuple[str, bytes, str, str]] = []
    total = 0
    for item, path in zip(entries, paths):
        posix = PurePosixPath(path)
        if (posix.is_absolute() or path != posix.as_posix()
                or any(part in {".", ".."} for part in path.split("/"))
                or item["mode"] not in {"100644", "100755"}):
            raise ValueError(f"unsafe Git tree entry: {path}")
        size = item["size"]
        if size < 0 or total + size > MAX_BYTES:
            raise ValueError("source tree exceeds importer byte limit")
        url = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/{prefix}{path}"
        raw = fetch_bytes(url, MAX_BYTES - total)
        if len(raw) != size or blob_sha(raw) != item["sha"]:
            raise ValueError(f"pinned Git blob mismatch: {path}")
        staged.append((path, raw, item["sha"], hashlib.sha256(raw).hexdigest()))
        total += len(raw)
    files = {path: raw for path, raw, _, _ in staged}
    if len(files["SKILL.md"]) > 32_768:
        raise ValueError("SKILL.md exceeds importer text limit")
    files["SKILL.md"].decode("utf-8")
    if b"Apache License" not in files["LICENSE.txt"] or b"Version 2.0" not in files["LICENSE.txt"]:
        raise ValueError("per-Skill Apache-2.0 license not confirmed")
    if original["skill_sha256"] != hashlib.sha256(files["SKILL.md"]).hexdigest():
        raise ValueError("SKILL.md differs from pinned metadata sample")
    manifest = {
        "id": skill, "version": "0.0.0", "kind": "skill",
        "title": skill, "description": original["description"],
        "author": REPO, "license": "Apache-2.0",
        "source": f"https://github.com/{REPO}/tree/{COMMIT}/{prefix[:-1]}",
        "content_file": "SKILL.md", "content_sha256": original["skill_sha256"],
        "support_files": [path for path in paths if path != "SKILL.md"],
    }
    inventory = {
        "repository": REPO, "commit": COMMIT, "skill": skill,
        "status": "quarantine-source-tree-snapshot",
        "source_tree_files": len(staged), "total_bytes": total,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warning": "All files under this Skill path were fetched; external dependencies, rights, and safety remain unreviewed. Do not install or execute.",
        "files": [
            {"path": path, "git_blob_sha1": gitsha, "sha256": sha256, "size": len(raw)}
            for path, raw, gitsha, sha256 in staged
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sample-", dir=output.parent) as staging:
        root = Path(staging)
        for path, raw, _, _ in staged:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        (root / "artifact.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (root / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        root.rename(output)
    return {"path": str(output), "files": len(staged), "bytes": total,
            "status": inventory["status"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", default="gh-fix-ci",
                        help="one of the already pinned eight metadata-sample Skill names")
    args = parser.parse_args()
    print(json.dumps(fetch(args.skill), ensure_ascii=False))
