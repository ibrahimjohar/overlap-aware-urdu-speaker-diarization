"""setup_fyp.py - create the FYP repo layout and the data folder outside the repo.

Run from the ROOT of your cloned repo (works on Windows, Linux and Mac):

    python setup_fyp.py --data-root "D:/FYP-data"          # Windows example
    python setup_fyp.py --data-root ~/FYP-data             # Linux / Mac example

Safe to run more than once: it never overwrites or deletes anything.
"""
import argparse
import shutil
from pathlib import Path

REPO_DIRS = {
    "docs": "Proposal, sourcing protocol, meeting notes, supervisor sign-offs.",
    "docs/meetings": "Meeting notes with supervisors (one file per meeting, YYYY-MM-DD.md).",
    "sourcing": "YouTube sourcing pipeline: common.py, s01-s12, requirements.txt.",
    "sourcing/config": "Keyword lists and other pipeline configuration.",
    "sourcing/pilot_notes": "Pilot measurements and observations (text only, no audio).",
    "annotation": "Annotation guidelines and format converters (later stage).",
    "modeling": "WavLM / Conformer / Mamba model code and training configs (later stage).",
    "experiments": "Small result tables, plots and run configs. Never checkpoints or audio.",
    "report": "LaTeX source for the FYP report and any paper.",
}

DATA_DIRS = ["pilot/corpus", "main/corpus", "backups", "checkpoints"]

GITIGNORE_LINES = [
    "# --- data: never commit audio, databases or corpus folders",
    "corpus/", "handoff/", "*.wav", "*.webm", "*.m4a", "*.opus", "*.mp3", "*.flac",
    "*.sqlite", "*.sqlite-journal", "*.npz",
    "# --- model weights",
    "*.pt", "*.pth", "*.ckpt", "*.safetensors", "*.bin",
    "# --- PDFs only allowed in docs/ (keep other people's papers out: use references.bib)",
    "*.pdf", "!docs/*.pdf", "!docs/**/*.pdf",
    "# --- secrets",
    ".env", "*.key",
    "# --- python / editors",
    ".venv/", "venv/", "__pycache__/", "*.pyc", ".ipynb_checkpoints/", ".DS_Store",
    "# --- internal notes stay local (public repo): folders exist, contents are never pushed",
    "sourcing/pilot_notes/*", "!sourcing/pilot_notes/README.md",
    "docs/meetings/*", "!docs/meetings/README.md",
]

README = """# Urdu Speaker Diarization & Speaker-Attributed Transcription (FYP)

FAST-NUCES Karachi. Team: Areeba Hasnain (lead), Syed Ukkashah Ahmed, Ibrahim Johar Farooqi.
Supervisors: Dr. Muhammad Yaseen Khan, Dr. Muhammad Rafi (co-supervisor).

## Layout
| Folder | Contents |
|---|---|
| `docs/` | proposal, sourcing protocol, meeting notes, sign-offs |
| `sourcing/` | YouTube sourcing pipeline (s01-s12) and its config |
| `annotation/` | annotation guidelines and converters |
| `modeling/` | model code and training configs |
| `experiments/` | small result tables, plots, run configs |
| `report/` | LaTeX source of the report / paper |

## Data lives OUTSIDE this repo
Audio, the provenance database and checkpoints are never committed.
Each member sets the data location in a local `.env` (copy `.env.example`):

    URDU_CORPUS_ROOT=<data-root>/pilot/corpus   # prototype
    URDU_CORPUS_ROOT=<data-root>/main/corpus    # full run

Load it before running scripts (Git Bash / Linux / Mac):

    set -a; source .env; set +a

## Rules
- Never commit audio, databases, model weights or `.env`.
- Run sourcing scripts from `sourcing/`.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True, help="folder OUTSIDE the repo for audio and databases")
    args = ap.parse_args()
    repo = Path.cwd()
    data_root = Path(args.data_root).expanduser().resolve()

    if not (repo / ".git").exists():
        raise SystemExit("Run this from the root of your cloned repo (the folder containing .git).")
    if data_root == repo or repo in data_root.parents:
        raise SystemExit("--data-root must be OUTSIDE the repo folder.")

    # 1. repo folders, each with a short README so Git tracks it
    for d, purpose in REPO_DIRS.items():
        p = repo / d
        p.mkdir(parents=True, exist_ok=True)
        if not any(p.iterdir()):
            (p / "README.md").write_text(f"# {d}\n\n{purpose}\n", encoding="utf-8")
        print("repo  ", p.relative_to(repo))

    # 2. move files from the earlier layout (scripts/, config/) if present
    old_scripts, old_config = repo / "scripts", repo / "config"
    if old_scripts.exists():
        for f in old_scripts.iterdir():
            dest = repo / "sourcing" / f.name
            if f.name == "keywords_seed.txt":
                dest = repo / "sourcing" / "config" / f.name
            if not dest.exists():
                shutil.move(str(f), str(dest))
        if not any(old_scripts.iterdir()):
            old_scripts.rmdir()
        print("moved  scripts/ -> sourcing/")
    if old_config.exists():
        for f in old_config.iterdir():
            dest = repo / "sourcing" / "config" / f.name
            if not dest.exists():
                shutil.move(str(f), str(dest))
        if not any(old_config.iterdir()):
            old_config.rmdir()
        print("moved  config/ -> sourcing/config/")

    # 3. .gitignore: add any missing lines, never remove existing ones
    gi = repo / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    missing = [l for l in GITIGNORE_LINES if l not in existing]
    if missing:
        with open(gi, "a", encoding="utf-8") as f:
            if existing and existing[-1].strip():
                f.write("\n")
            f.write("\n".join(missing) + "\n")
    print(f"file   .gitignore ({len(missing)} lines added)")

    # 4. README, .env.example and a local .env
    if not (repo / "README.md").exists() or (repo / "README.md").stat().st_size < 50:
        (repo / "README.md").write_text(README, encoding="utf-8")
        print("file   README.md")
    pilot = (data_root / "pilot" / "corpus").as_posix()
    env_example = (f"URDU_CORPUS_ROOT={pilot}\n"
                   "YT_API_KEY=paste-your-youtube-data-api-key\n"
                   "HF_TOKEN=paste-your-huggingface-token\n"
                   "DIAR_MODEL=pyannote/speaker-diarization-3.1\n"
                   "LID_MODEL=small\n")
    (repo / ".env.example").write_text(env_example.replace(pilot, "<data-root>/pilot/corpus"), encoding="utf-8")
    if not (repo / ".env").exists():
        (repo / ".env").write_text(env_example, encoding="utf-8")
        print("file   .env (local only, fill in your keys)")

    # 5. data folders outside the repo
    for d in DATA_DIRS:
        (data_root / d).mkdir(parents=True, exist_ok=True)
        print("data  ", data_root / d)
    (data_root / "README.txt").write_text(
        "FYP data folder. NOT part of the Git repo.\n"
        "pilot/corpus - prototype run (URDU_CORPUS_ROOT during the pilot)\n"
        "main/corpus - full run\n"
        "backups - weekly copies of provenance.sqlite and licence_evidence/\n"
        "checkpoints - model weights\n"
        "Keep this folder access-controlled; never upload it to a public link.\n", encoding="utf-8")

    print("\nDone. Next: git status  ->  git add .  ->  git commit  ->  git push")


if __name__ == "__main__":
    main()