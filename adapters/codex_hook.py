"""Entrypoint for Codex's UserPromptSubmit command hook."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from marked.hooks import hook_main  # noqa: E402


if __name__ == "__main__":
    hook_main("codex")
