"""s05_download.py - Step 5: download the original audio stream and make a 16 kHz working copy.

    python s05_download.py --limit 50                 # CC candidates from approved channels first
    python s05_download.py --limit 50 --tier any

For each video this script:
  1. downloads the best audio-only stream with yt-dlp, unchanged (usually Opus in .webm or AAC in .m4a)
  2. records codec / sample rate / channels / bitrate / duration with ffprobe
  3. writes a 16 kHz, mono, 16-bit PCM WAV working copy with ffmpeg
  4. stores SHA-256 checksums of both files
It stops the whole run if YouTube starts asking for sign-in / bot verification.
Never try to get around such a block; slow down and talk to your supervisor instead.
"""
import argparse
import hashlib
import json
import subprocess

from common import RAW_DIR, TARGET_SR, WAV_DIR, db, log_event, now

YTDLP_OPTS = [
    "-f", "bestaudio/best",          # audio-only stream, no re-encoding
    "--no-playlist",
    "--write-info-json",             # keeps title, channel, upload date, licence text, description
    "--no-overwrites",
    "--retries", "10", "--fragment-retries", "10",
    "--sleep-requests", "1",         # pause between metadata requests
    "--sleep-interval", "5", "--max-sleep-interval", "20",   # random pause before each download
    "--print", "after_move:filepath", "--no-simulate",
]
BLOCK_SIGNS = ("Sign in to confirm", "not a bot", "HTTP Error 429")


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                          "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate:format=duration,bit_rate",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    j = json.loads(out.stdout)
    st = j["streams"][0]
    br = st.get("bit_rate") or j["format"].get("bit_rate")
    return (st.get("codec_name"), int(st.get("sample_rate", 0)), int(st.get("channels", 0)),
            int(br) if br and br != "N/A" else None, float(j["format"]["duration"]))


def to_wav(src, dst):
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                    "-i", str(src), "-map", "0:a:0", "-vn",
                    "-ac", "1", "-ar", str(TARGET_SR), "-c:a", "pcm_s16le", str(dst)], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--tier", default="A", choices=["A", "any"])
    ap.add_argument("--include-unvetted", action="store_true",
                    help="also download from channels not yet approved (not recommended)")
    args = ap.parse_args()
    con = db()
    ytdlp_version = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True).stdout.strip()
    sql = """SELECT v.video_id, v.channel_id FROM videos v JOIN channels c USING(channel_id)
             WHERE v.meta_status='pass' AND v.video_id NOT IN (SELECT video_id FROM downloads
                                                              WHERE status='ok')"""
    if args.tier == "A":
        sql += " AND v.tier_prelim LIKE 'A%'"
    if not args.include_unvetted:
        sql += " AND c.vetting='approved'"
    sql += " ORDER BY v.tier_prelim, v.duration_s DESC LIMIT ?"
    for r in con.execute(sql, (args.limit,)).fetchall():
        vid, ch = r["video_id"], r["channel_id"]
        (RAW_DIR / ch).mkdir(exist_ok=True)
        cmd = ["yt-dlp", *YTDLP_OPTS, "-o", str(RAW_DIR / ch / "%(id)s.%(ext)s"),
               f"https://www.youtube.com/watch?v={vid}"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if any(sig in res.stderr for sig in BLOCK_SIGNS):
            con.execute("INSERT OR REPLACE INTO downloads(video_id, downloaded_at, status, error, ytdlp_version)"
                        " VALUES (?,?,?,?,?)", (vid, now(), "blocked", res.stderr[-500:], ytdlp_version))
            con.commit()
            raise SystemExit("YouTube is asking for verification. Stop for today, lower the rate, "
                             "and do NOT attempt to bypass it (see Section 7.5).")
        paths = [p for p in res.stdout.strip().splitlines() if p]
        if res.returncode != 0 or not paths:
            con.execute("INSERT OR REPLACE INTO downloads(video_id, downloaded_at, status, error, ytdlp_version)"
                        " VALUES (?,?,?,?,?)", (vid, now(), "failed", res.stderr[-500:], ytdlp_version))
            log_event(con, "video", vid, "download_failed", res.stderr[-300:])
            con.commit()
            continue
        orig = paths[-1]
        wav = WAV_DIR / f"{vid}.wav"
        try:
            codec, sr, chans, br, dur = probe(orig)
            to_wav(orig, wav)
        except subprocess.CalledProcessError as exc:
            con.execute("INSERT OR REPLACE INTO downloads(video_id, downloaded_at, orig_path, status, error,"
                        " ytdlp_version) VALUES (?,?,?,?,?,?)",
                        (vid, now(), orig, "convert_failed", str(exc)[-500:], ytdlp_version))
            con.commit()
            continue
        con.execute("""INSERT OR REPLACE INTO downloads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (vid, now(), orig, codec, sr, chans, br, dur, str(wav),
                     sha256(orig), sha256(wav), ytdlp_version, "ok", None))
        log_event(con, "video", vid, "downloaded", {"codec": codec, "sr": sr, "duration": dur})
        con.commit()
        print(f"ok {vid} {codec} {sr}Hz {chans}ch {dur / 60:.1f} min")


if __name__ == "__main__":
    main()
