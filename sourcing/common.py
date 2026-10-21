"""common.py - shared configuration, paths and provenance-database helpers.

Every other script imports this file. Nothing here talks to YouTube.
Set these environment variables before running anything:
    URDU_CORPUS_ROOT   folder where all data lives (default: ./corpus)
    YT_API_KEY         YouTube Data API v3 key (one per team member / Google Cloud project)
    HF_TOKEN           Hugging Face token (needed for gated pyannote models)
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")  # reads the repo-root .env on any OS/terminal

# ---------------------------------------------------------------- paths
ROOT = Path(os.environ.get("URDU_CORPUS_ROOT", "./corpus")).resolve()
DB_PATH = ROOT / "provenance.sqlite"
RAW_DIR = ROOT / "audio_raw"            # untouched originals exactly as downloaded (read-only)
WAV_DIR = ROOT / "audio_wav16k"         # full-length 16 kHz mono 16-bit PCM working copies
RTTM_DIR = ROOT / "machine_rttm"        # machine diarization hypotheses for full videos
PROBE_DIR = ROOT / "tmp_probes"         # temporary probe clips for stage-1 screening
EVIDENCE_DIR = ROOT / "licence_evidence"
EMB_DIR = ROOT / "speaker_embeddings"
HANDOFF_DIR = ROOT / "handoff"          # packaged excerpts for the annotation stage
LOG_DIR = ROOT / "logs"
REPORT_DIR = ROOT / "reports"           # CSV reports (seed-channel profiles, weekly numbers)

YT_API_KEY = os.environ.get("YT_API_KEY")
HF_TOKEN = os.environ.get("HF_TOKEN")

# ---------------------------------------------------------------- tunable thresholds
MIN_DURATION_S = 8 * 60           # shorter videos rarely contain usable 8-15 min conversations
MAX_DURATION_S = 4 * 60 * 60      # very long streams are mostly dead air; review manually if needed
TARGET_SR = 16000                 # sample rate of all working audio

EXCLUDE_TERMS = [                 # genre words that almost always mean music, scripted or monologue content
    "naat", "qawwali", "song", "lyrics", "drama episode", "full movie", "audiobook",
    "khutba", "bayan", "tilawat", "mushaira",
    "نعت", "قوالی", "گانا", "ڈرامہ", "خطبہ", "تلاوت", "مشاعرہ",
]

# Letters used in Urdu but not in Persian/Arabic writing. An Arabic-script title with none of these
# is probably Persian or Arabic (e.g. "گفتگو" also means "conversation" in Persian).
URDU_ONLY_LETTERS = set("ٹڈڑںےۓہھ")


def arabic_script_count(text):
    return sum(1 for ch in text if "\u0600" <= ch <= "\u06FF")


def looks_not_urdu(text, min_letters=8):
    """True when text is clearly Arabic-script but uses no Urdu-only letter."""
    return arabic_script_count(text) >= min_letters and not (URDU_ONLY_LETTERS & set(text))


for d in (ROOT, RAW_DIR, WAV_DIR, RTTM_DIR, PROBE_DIR, EVIDENCE_DIR, EMB_DIR, HANDOFF_DIR, LOG_DIR, REPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords(
    keyword TEXT, track TEXT, window_start TEXT, window_end TEXT,
    pages INTEGER, n_results INTEGER, done_at TEXT,
    PRIMARY KEY (keyword, track, window_start));
CREATE TABLE IF NOT EXISTS channels(
    channel_id TEXT PRIMARY KEY, title TEXT, uploads_playlist TEXT,
    discovered_via TEXT, discovered_at TEXT, expanded_at TEXT,
    vetting TEXT DEFAULT 'pending', vetting_note TEXT);
CREATE TABLE IF NOT EXISTS videos(
    video_id TEXT PRIMARY KEY, channel_id TEXT, discovered_via TEXT, discovered_at TEXT,
    title TEXT, description TEXT, published_at TEXT, duration_s REAL,
    license TEXT, licensed_content INTEGER, made_for_kids INTEGER,
    default_audio_lang TEXT, live TEXT, privacy TEXT,
    meta_checked_at TEXT, meta_status TEXT, meta_reason TEXT, tier_prelim TEXT);
CREATE TABLE IF NOT EXISTS licence_evidence(
    video_id TEXT, checked_at TEXT, api_license TEXT, wayback_url TEXT,
    screenshot_path TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS downloads(
    video_id TEXT PRIMARY KEY, downloaded_at TEXT, orig_path TEXT, orig_codec TEXT,
    orig_sr INTEGER, orig_channels INTEGER, orig_bitrate INTEGER, duration_s REAL,
    wav_path TEXT, sha256_orig TEXT, sha256_wav TEXT, ytdlp_version TEXT,
    status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS screening(
    video_id TEXT, stage TEXT, screened_at TEXT,
    speech_ratio REAL, music_ratio REAL, noise_ratio REAL,
    urdu_hindi_frac REAL, english_frac REAL, other_lang_frac REAL, lid_windows INTEGER,
    n_speakers INTEGER, overlap_ratio REAL, max_concurrent INTEGER, changes_per_min REAL,
    rttm_path TEXT, diar_model TEXT, score REAL,
    PRIMARY KEY (video_id, stage));
CREATE TABLE IF NOT EXISTS triage(
    video_id TEXT, reviewer TEXT, reviewed_at TEXT, decision TEXT, tier TEXT,
    original_creator TEXT, conversational TEXT, urdu_dominant TEXT, music_bed TEXT,
    inserted_clips TEXT, minors TEXT, private_info TEXT, sensitive TEXT,
    format_type TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS excerpts(
    excerpt_id TEXT PRIMARY KEY, video_id TEXT, channel_id TEXT, start_s REAL, end_s REAL,
    n_speakers_hyp INTEGER, overlap_ratio_hyp REAL, score REAL,
    tier TEXT, split TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS events(
    ts TEXT, actor TEXT, entity TEXT, entity_id TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS channel_profile(
    channel_id TEXT, profiled_at TEXT, category TEXT, seed TEXT, known_licence TEXT,
    n_checked INTEGER, cc_share REAL, claimed_share REAL, long_share REAL, median_min REAL,
    urdu_title_share REAL, subscribers INTEGER, video_count INTEGER, suggestion TEXT,
    PRIMARY KEY (channel_id, profiled_at));
CREATE TABLE IF NOT EXISTS takedowns(
    request_id TEXT PRIMARY KEY, received_at TEXT, requester TEXT, video_id TEXT,
    action_taken TEXT, completed_at TEXT);
"""


def now() -> str:
    """UTC timestamp in ISO-8601, used for every *_at column."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def log_event(con, entity, entity_id, action, detail="", actor=None):
    """Append-only audit log. Never delete rows from this table."""
    actor = actor or os.environ.get("USER", "unknown")
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    con.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                (now(), actor, entity, entity_id, action, detail))


def youtube_client():
    """Authenticated YouTube Data API v3 client (API key only, public data only)."""
    if not YT_API_KEY:
        raise SystemExit("Set YT_API_KEY first (see Section 5.3 of the protocol).")
    from googleapiclient.discovery import build
    return build("youtube", "v3", developerKey=YT_API_KEY, cache_discovery=False)


def is_quota_error(err) -> bool:
    return "quotaExceeded" in str(err) or "dailyLimitExceeded" in str(err)


def read_rttm(path):
    """Return a list of (start, end, speaker) tuples from an RTTM file."""
    segs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.split()
            if len(p) >= 8 and p[0] == "SPEAKER":
                start, dur = float(p[3]), float(p[4])
                segs.append((start, start + dur, p[7]))
    return sorted(segs)


def write_rttm(path, uri, segs):
    with open(path, "w", encoding="utf-8") as f:
        for s, e, spk in sorted(segs):
            f.write(f"SPEAKER {uri} 1 {s:.3f} {e - s:.3f} <NA> <NA> {spk} <NA> <NA>\n")


def speech_stats(segs):
    """Speech time, overlapped time and max simultaneous speakers from diarization segments."""
    events = []
    for s, e, _ in segs:
        events += [(s, 1), (e, -1)]
    events.sort()                    # at equal times, ends (-1) sort before starts (+1)
    active, prev, speech, overlap, max_c = 0, None, 0.0, 0.0, 0
    for t, d in events:
        if prev is not None and active > 0:
            speech += t - prev
            if active > 1:
                overlap += t - prev
        active += d
        max_c = max(max_c, active)
        prev = t
    return speech, overlap, max_c


def speaker_changes(segs):
    ordered = sorted(segs)
    return sum(1 for a, b in zip(ordered, ordered[1:]) if a[2] != b[2])


def speech_union(segs):
    """Merge all speaker segments into non-overlapping speech intervals."""
    merged = []
    for s, e, _ in sorted(segs):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged
