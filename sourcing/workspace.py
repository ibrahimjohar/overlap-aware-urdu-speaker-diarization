"""workspace.py - set up and switch the data workspaces (pilot / main), and back them up.

    python workspace.py init              # create the full folder structure next to the repo
    python workspace.py use main          # point .env at FYP-data/main/corpus (or: use pilot)
    python workspace.py status            # which corpus is active, free disk space, funnel counts
    python workspace.py backup            # dated copy of the database + licence evidence
    python workspace.py relocate          # after moving FYP-data to another folder/PC: fix stored paths

Folder structure (outside the repo, never pushed to GitHub):

    FYP-data/
      pilot/corpus/            pilot run (10 videos) - keep for reference
      main/corpus/             the real, large-scale corpus (scripts create all subfolders)
      main/triage_sheets/      exported/filled triage CSVs (contain reviewer notes)
      main/permissions/        Tier B permission e-mails/letters as PDF
      main/notes/              weekly reports, decisions, meeting notes about the data
      backups/                 dated copies made by `workspace.py backup`
      checkpoints/             model weights (later stages)
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENV = REPO / ".env"
SUBDIRS = ["pilot/corpus", "main/corpus", "main/triage_sheets", "main/permissions", "main/notes",
           "backups", "checkpoints"]


def read_env():
    if not ENV.exists():
        sys.exit(f"{ENV} not found - copy .env.example to .env first.")
    return ENV.read_text(encoding="utf-8").splitlines()


def current_root():
    for line in read_env():
        if line.strip().startswith("URDU_CORPUS_ROOT="):
            return Path(line.split("=", 1)[1].strip().strip('"'))
    return None


def data_root(arg):
    if arg:
        return Path(arg).expanduser().resolve()
    root = current_root()
    if root is not None and root.name == "corpus" and root.parent.name in ("pilot", "main"):
        return root.parents[1]
    return (REPO.parent / "FYP-data").resolve()


def cmd_init(args):
    base = data_root(args.data_root)
    if base == REPO or REPO in base.parents:
        sys.exit("The data folder must be OUTSIDE the repo.")
    for d in SUBDIRS:
        (base / d).mkdir(parents=True, exist_ok=True)
        print("ok  ", base / d)
    readme = base / "README.txt"
    if not readme.exists():
        readme.write_text(__doc__.split("Folder structure")[1], encoding="utf-8")
    print(f"\nWorkspace ready under {base}. Switch to it with: python workspace.py use main")


def cmd_use(args):
    base = data_root(args.data_root)
    target = (base / args.which / "corpus")
    target.mkdir(parents=True, exist_ok=True)
    value = target.as_posix()
    lines, done = read_env(), False
    for i, line in enumerate(lines):
        if line.strip().startswith("URDU_CORPUS_ROOT="):
            lines[i], done = f"URDU_CORPUS_ROOT={value}", True
    if not done:
        lines.insert(0, f"URDU_CORPUS_ROOT={value}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"active corpus -> {value}")


def cmd_status(args):
    root = current_root()
    print(f"active corpus : {root}")
    if root is None or not root.exists():
        return
    usage = shutil.disk_usage(root)
    print(f"free disk     : {usage.free / 1e9:.1f} GB")
    dbp = root / "provenance.sqlite"
    if not dbp.exists():
        print("database      : not created yet")
        return
    con = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    q = lambda s: con.execute(s).fetchone()[0]
    for label, sql in [
            ("videos discovered", "SELECT COUNT(*) FROM videos"),
            ("metadata pass", "SELECT COUNT(*) FROM videos WHERE meta_status='pass'"),
            ("channels approved", "SELECT COUNT(*) FROM channels WHERE vetting='approved'"),
            ("channels pending", "SELECT COUNT(*) FROM channels WHERE vetting='pending'"),
            ("downloaded (ok)", "SELECT COUNT(*) FROM downloads WHERE status='ok'"),
            ("hours downloaded", "SELECT ROUND(COALESCE(SUM(duration_s),0)/3600.0,1) FROM downloads WHERE status='ok'"),
            ("full-screened", "SELECT COUNT(*) FROM screening WHERE stage='full'"),
            ("accepted in triage", "SELECT COUNT(DISTINCT video_id) FROM triage WHERE decision='accept'")]:
        try:
            print(f"{label:18s}: {q(sql)}")
        except sqlite3.OperationalError:
            pass


def cmd_backup(args):
    root = current_root()
    dbp = root / "provenance.sqlite"
    if not dbp.exists():
        sys.exit("No database to back up yet.")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    dest = data_root(args.data_root) / "backups" / f"{root.parent.name}_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(dbp), sqlite3.connect(dest / "provenance.sqlite")
    src.backup(dst)                      # consistent copy even if another script is writing
    dst.close()
    src.close()
    ev = root / "licence_evidence"
    if ev.exists() and any(ev.iterdir()):
        shutil.copytree(ev, dest / "licence_evidence", dirs_exist_ok=True)
    print(f"backup written to {dest}")


MARKERS = ("audio_raw", "audio_wav16k", "machine_rttm", "licence_evidence", "speaker_embeddings")
PATH_COLUMNS = [("downloads", "video_id", "orig_path"), ("downloads", "video_id", "wav_path"),
                ("screening", "rowid", "rttm_path"), ("licence_evidence", "rowid", "screenshot_path")]


def rebase(old, root):
    """Rebuild a stored file path under the current corpus root, using its corpus subfolder."""
    parts = old.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part in MARKERS:
            return str(root.joinpath(*parts[i:]))
    return None


def cmd_relocate(args):
    """Rewrite absolute paths in the database so they point into the active corpus folder."""
    root = current_root()
    dbp = root / "provenance.sqlite"
    if not dbp.exists():
        sys.exit(f"No database at {dbp}. Point .env at the moved corpus first (workspace.py use ...).")
    con = sqlite3.connect(dbp)
    changed, missing = 0, 0
    for table, key, col in PATH_COLUMNS:
        try:
            rows = con.execute(f"SELECT {key}, {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            continue
        for k, old in rows:
            new = rebase(old, root)
            if new is None or new == old:
                continue
            if not Path(new).exists():
                missing += 1
            if not args.dry_run:
                con.execute(f"UPDATE {table} SET {col}=? WHERE {key}=?", (new, k))
            changed += 1
    if not args.dry_run:
        con.execute("INSERT INTO events VALUES (datetime('now'), 'workspace', 'corpus', ?, 'relocated', ?)",
                    (str(root), f"{changed} paths rewritten"))
        con.commit()
    print(f"{'would rewrite' if args.dry_run else 'rewrote'} {changed} paths to {root}")
    if missing:
        print(f"WARNING: {missing} rewritten paths point to files that do not exist here - "
              "check the whole FYP-data folder was copied.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="", help="FYP-data folder (default: derived from .env)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    u = sub.add_parser("use")
    u.add_argument("which", choices=["pilot", "main"])
    sub.add_parser("status")
    sub.add_parser("backup")
    r = sub.add_parser("relocate")
    r.add_argument("--dry-run", action="store_true", help="only report what would change")
    args = ap.parse_args()
    {"init": cmd_init, "use": cmd_use, "status": cmd_status, "backup": cmd_backup,
     "relocate": cmd_relocate}[args.cmd](args)


if __name__ == "__main__":
    main()
