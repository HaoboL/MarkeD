"""Host-specific UserPromptSubmit entrypoint for packaged Codex or Claude Code hooks."""

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from marked.hooks import hook_main  # noqa: E402


if __name__ == "__main__":
    host = "codex" if os.environ.get("PLUGIN_ROOT") else "claude-code"
    hook_main(host)
