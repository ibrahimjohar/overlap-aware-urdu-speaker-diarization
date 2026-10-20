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

Progress is shown live: "[3/10] VIDEO_ID ..." then a single updating line with percent, size,
speed and time left. Stopping with Ctrl+C is safe: the half-finished file is kept as .part and the
next run resumes it instead of starting again (useful on slow connections).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

from common import RAW_DIR, TARGET_SR, WAV_DIR, db, log_event, now

YTDLP_OPTS = [
    "-f", "bestaudio/best",          # audio-only stream, no re-encoding
    "--no-playlist",
    "--write-info-json",             # keeps title, channel, upload date, licence text, description
    "--no-overwrites",
    "--retries", "10", "--fragment-retries", "10",
    "--sleep-requests", "1",         # pause between metadata requests
    "--sleep-interval", "5", "--max-sleep-interval", "20",   # random pause before each download
    "--print", "after_move:SAVED::%(filepath)s", "--no-simulate",
    "--progress", "--newline",       # keep the progress bar (--print hides it) as one line per update
    "--continue",                    # resume a .part file left by an interrupted run
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


def run_ytdlp(cmd):
    """Run yt-dlp, showing its progress on one updating console line.

    Returns (returncode, saved_path or None, full output text)."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env)
    saved, lines, shown = None, [], False
    for raw in proc.stdout:
        line = raw.rstrip()
        lines.append(line)
        if line.startswith("SAVED::"):
            saved = line[len("SAVED::"):]
        elif line.startswith("[download]") and ("%" in line or "Sleeping" in line):
            print("\r    " + line[len("[download]"):].strip()[:90].ljust(90), end="", flush=True)
            shown = True
        elif "ERROR" in line or "WARNING" in line:
            if shown:
                print()
                shown = False
            print("    " + line[:160], flush=True)
    proc.wait()
    if shown:
        print()
    return proc.returncode, saved, "\n".join(lines)


def select_queue(con, args):
    """Videos to download this run: passed metadata, approved channel, not yet downloaded.

    Creative Commons first, then longest first within the limits. --per-channel spreads a small run
    across channels; --max-minutes keeps very long streams out (useful on small GPUs)."""
    sql = """SELECT v.video_id, v.channel_id FROM videos v JOIN channels c USING(channel_id)
             WHERE v.meta_status='pass' AND v.video_id NOT IN (SELECT video_id FROM downloads
                                                              WHERE status IN ('ok','audio_deleted'))
               AND v.video_id NOT IN (SELECT video_id FROM triage WHERE decision='reject')"""
    params = []
    if args.tier == "A":
        sql += " AND v.tier_prelim LIKE 'A%'"
    if not args.include_unvetted:
        sql += " AND c.vetting='approved'"
    if args.max_minutes:
        sql += " AND v.duration_s <= ?"
        params.append(args.max_minutes * 60)
    sql += " ORDER BY v.tier_prelim, v.duration_s DESC"
    picked, per_ch = [], {}
    for r in con.execute(sql, params).fetchall():
        if args.per_channel and per_ch.get(r["channel_id"], 0) >= args.per_channel:
            continue
        per_ch[r["channel_id"]] = per_ch.get(r["channel_id"], 0) + 1
        picked.append(r)
        if len(picked) >= args.limit:
            break
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--tier", default="A", choices=["A", "any"])
    ap.add_argument("--per-channel", type=int, default=0,
                    help="at most N videos per channel in this run (0 = no cap); use 3 for the pilot")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="skip videos longer than this (0 = no limit); e.g. 90 for the pilot")
    ap.add_argument("--include-unvetted", action="store_true",
                    help="also download from channels not yet approved (not recommended)")
    ap.add_argument("--dry-run", action="store_true", help="only list what would be downloaded")
    args = ap.parse_args()
    con = db()
    if args.dry_run:
        for r in select_queue(con, args):
            v = con.execute("SELECT duration_s, tier_prelim, title FROM videos WHERE video_id=?",
                            (r["video_id"],)).fetchone()
            print(f"{r['video_id']}  {v['duration_s'] / 60:5.1f} min  {v['tier_prelim']:8s} {(v['title'] or '')[:60]}")
        return
    ytdlp_version = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True).stdout.strip()
    queue = select_queue(con, args)
    if not queue:
        print("nothing to download")
        return
    total_min = sum((con.execute("SELECT duration_s FROM videos WHERE video_id=?",
                                 (r["video_id"],)).fetchone()[0] or 0) for r in queue) / 60
    print(f"{len(queue)} videos queued, {total_min:.0f} min of audio in total. Ctrl+C stops safely.")
    done, failed, t_start = 0, 0, time.time()
    for i, r in enumerate(queue, 1):
        vid, ch = r["video_id"], r["channel_id"]
        mins = (con.execute("SELECT duration_s FROM videos WHERE video_id=?", (vid,)).fetchone()[0] or 0) / 60
        print(f"[{i}/{len(queue)}] {vid}  ({mins:.1f} min) downloading...", flush=True)
        (RAW_DIR / ch).mkdir(exist_ok=True)
        cmd = ["yt-dlp", *YTDLP_OPTS, "-o", str(RAW_DIR / ch / "%(id)s.%(ext)s"),
               f"https://www.youtube.com/watch?v={vid}"]
        t0 = time.time()
        rc, orig, out = run_ytdlp(cmd)
        if any(sig in out for sig in BLOCK_SIGNS):
            con.execute("INSERT OR REPLACE INTO downloads(video_id, downloaded_at, status, error, ytdlp_version)"
                        " VALUES (?,?,?,?,?)", (vid, now(), "blocked", out[-500:], ytdlp_version))
            con.commit()
            raise SystemExit("YouTube is asking for verification. Stop for today, lower the rate, "
                             "and do NOT attempt to bypass it (see Section 7.5).")
        if rc != 0 or not orig or not os.path.exists(orig):
            con.execute("INSERT OR REPLACE INTO downloads(video_id, downloaded_at, status, error, ytdlp_version)"
                        " VALUES (?,?,?,?,?)", (vid, now(), "failed", out[-500:], ytdlp_version))
            log_event(con, "video", vid, "download_failed", out[-300:])
            con.commit()
            failed += 1
            print(f"    FAILED (exit code {rc}); details saved in the database. Moving on.")
            continue
        print(f"    downloaded in {(time.time() - t0) / 60:.1f} min; converting to 16 kHz WAV...", flush=True)
        wav = WAV_DIR / f"{vid}.wav"
        try:
            codec, sr, chans, br, dur = probe(orig)
            to_wav(orig, wav)
        except subprocess.CalledProcessError as exc:
            con.execute("INSERT OR REPLACE INTO downloads(video_id, downloaded_at, orig_path, status, error,"
                        " ytdlp_version) VALUES (?,?,?,?,?,?)",
                        (vid, now(), orig, "convert_failed", str(exc)[-500:], ytdlp_version))
            con.commit()
            failed += 1
            print("    CONVERSION FAILED; details saved in the database. Moving on.")
            continue
        con.execute("""INSERT OR REPLACE INTO downloads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (vid, now(), orig, codec, sr, chans, br, dur, str(wav),
                     sha256(orig), sha256(wav), ytdlp_version, "ok", None))
        log_event(con, "video", vid, "downloaded", {"codec": codec, "sr": sr, "duration": dur})
        con.commit()
        done += 1
        print(f"    ok: {codec} {sr} Hz {chans} ch, {dur / 60:.1f} min  "
              f"(elapsed {(time.time() - t_start) / 60:.0f} min, {done} done, {failed} failed)", flush=True)
    print(f"finished: {done} downloaded, {failed} failed")


if __name__ == "__main__":
    main()
