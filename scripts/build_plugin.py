"""Build a minimal MarkeD plugin root from an explicit source allowlist.

The output directory must not exist. This prevents local catalogs, samples,
logs, credentials, and repository internals from entering a plugin cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "plugins" / "marked"
FILES = (
    ".codex-plugin/plugin.json",
    ".claude-plugin/plugin.json",
    "hooks/hooks.json",
    "adapters/universal_hook.py",
    "adapters/codex_hook.py",
    "adapters/claude_hook.py",
    "marked/__init__.py",
    "marked/__main__.py",
    "marked/cli.py",
    "marked/core.py",
    "marked/dialogue.py",
    "marked/hooks.py",
    "README.md",
    "LICENSE",
)


def build(output: Path = DEFAULT_OUTPUT, *, replace: bool = False) -> dict[str, object]:
    output = output.expanduser()
    if output.is_symlink():
        raise ValueError("plugin output must not be a symlink")
    output = output.resolve()
    old_exists = output.exists()
    if old_exists:
        if not replace:
            raise ValueError(f"plugin output already exists: {output}; pass --replace")
        marker = output / "BUILD_MANIFEST.json"
        if not output.is_dir() or not marker.is_file():
            raise ValueError("refusing to replace an output without the MarkeD build marker")
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("builder") != "MarkeD explicit allowlist":
            raise ValueError("refusing to replace an unknown plugin output")
    output.parent.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with tempfile.TemporaryDirectory(prefix=".marked-build-", dir=output.parent) as temporary:
        staging = Path(temporary)
        for relative in FILES:
            source = ROOT / relative
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"unsafe or missing source: {relative}")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = source.read_bytes()
            target.write_bytes(raw)
            hashes[relative] = hashlib.sha256(raw).hexdigest()
        (staging / "BUILD_MANIFEST.json").write_text(
            json.dumps({"builder": "MarkeD explicit allowlist", "files": hashes}, indent=2) + "\n",
            encoding="utf-8",
        )
        if old_exists:
            backup = output.parent / f".marked-previous-{uuid.uuid4().hex}"
            output.rename(backup)
            try:
                staging.rename(output)
            except BaseException:
                backup.rename(output)
                raise
            shutil.rmtree(backup)
        else:
            staging.rename(output)
    return {"path": str(output), "files": len(hashes),
            "copied_catalog": False, "replaced": old_exists}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true",
                        help="atomically replace only an existing MarkeD-generated package")
    args = parser.parse_args()
    print(json.dumps(build(args.output, replace=args.replace), ensure_ascii=False))


if __name__ == "__main__":
    main()
