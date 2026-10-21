"""install_hooks.py - install the safety pre-commit hook into this clone (run once per PC).

    python install_hooks.py

Hooks are not copied by `git clone`, so every team member runs this once on every machine.
The hook blocks commits that contain audio, databases, model weights, .env, or anything that
looks like a Google API key or Hugging Face token. Git for Windows runs it with its own
built-in shell, so it works the same from PowerShell, VS Code or any other Git client.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

HOOK = r"""#!/bin/sh
# Installed by install_hooks.py - blocks data files, weights, .env and API keys from commits.
blocked=$(git diff --cached --name-only | grep -E '(^|/)\.env$|\.(wav|webm|m4a|opus|mp3|flac|sqlite|npz|pt|pth|ckpt|safetensors)$')
if [ -n "$blocked" ]; then
  echo "Commit blocked - these files must not be committed:"; echo "$blocked"; exit 1
fi
if git diff --cached -U0 | grep '^+' | grep -qE 'AIza[0-9A-Za-z_-]{35}|hf_[A-Za-z0-9]{30,}'; then
  echo "Commit blocked - an API key or HF token seems to be staged. Keep it in .env only."; exit 1
fi
exit 0
"""


def main():
    repo = Path(__file__).resolve().parent
    try:
        git_dir = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("Run this from inside the cloned repo (git must be installed).")
    hooks = (repo / git_dir / "hooks").resolve()
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "pre-commit"
    with open(
        target, "w", encoding="utf-8", newline="\n"
    ) as f:  # LF line endings: required by sh
        f.write(HOOK)
    os.chmod(
        target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    print(f"pre-commit hook installed at {target}")
    print("Test it:  a commit containing test.wav should print 'Commit blocked'.")


if __name__ == "__main__":
    main()
