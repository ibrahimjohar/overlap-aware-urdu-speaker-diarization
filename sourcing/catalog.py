"""catalog.py - one row per video with everything known about it, for filtering, cleaning and audits.

    python catalog.py                     # all videos that were downloaded (default)
    python catalog.py --all               # every discovered video, downloaded or not
    python catalog.py --out my_view.csv   # custom output path

Writes <corpus>/reports/catalog_<date>.csv (UTF-8 with BOM, so Excel shows Urdu correctly).
Read-only: it never changes the database. Rebuild it whenever you need a fresh view.

Columns (grouped):
  identity     video_id, video_url, channel_id, channel_url, channel_title, title
  source       published_at, upload_date_ytdlp, uploader, duration_min, view_count, categories, tags,
               chapters, ytdlp_language, api_audio_language, made_for_kids
  licence      api_licence, partner_claimed, tier_prelim, licence_evidence_url, licence_checked_at,
               seed_known_licence
  provenance   discovered_via, discovered_at, channel_vetting, channel_vetting_note
  download     download_status, downloaded_at, orig_codec, orig_sample_rate, orig_channels,
               orig_bitrate_kbps, ytdlp_version, sha256_original, sha256_wav, wav_path, info_json
  screening    probe_score, full_score, speech_pct, music_pct, noise_pct, urdu_hindi_pct, english_pct,
               lid_windows, n_speakers, overlap_pct, max_concurrent, changes_per_min, diar_model, rttm_path
  decisions    triage_decision, triage_tier, triage_reviewer, triage_at, triage_notes, format_type,
               n_excerpts, excerpt_hours, splits, takedown, last_event, last_event_at
"""

import argparse
import csv
import json
from datetime import date
from pathlib import Path

from common import RAW_DIR, REPORT_DIR, db

SQL = """
SELECT v.video_id, v.channel_id, c.title AS channel_title, v.title, v.published_at, v.duration_s,
       v.default_audio_lang, v.made_for_kids, v.license, v.licensed_content, v.tier_prelim,
       v.discovered_via, v.discovered_at, v.meta_status, v.meta_reason,
       c.vetting, c.vetting_note,
       (SELECT known_licence FROM channel_profile cp WHERE cp.channel_id = v.channel_id
          ORDER BY profiled_at DESC LIMIT 1) AS seed_known_licence,
       le.wayback_url, le.checked_at AS licence_checked_at,
       d.status AS download_status, d.downloaded_at, d.orig_codec, d.orig_sr, d.orig_channels,
       d.orig_bitrate, d.ytdlp_version, d.sha256_orig, d.sha256_wav, d.wav_path, d.orig_path,
       sp.score AS probe_score,
       sf.score AS full_score, sf.speech_ratio, sf.music_ratio, sf.noise_ratio, sf.urdu_hindi_frac,
       sf.english_frac, sf.lid_windows, sf.n_speakers, sf.overlap_ratio, sf.max_concurrent,
       sf.changes_per_min, sf.diar_model, sf.rttm_path,
       t.decision, t.tier, t.reviewer, t.reviewed_at, t.notes, t.format_type,
       (SELECT COUNT(*) FROM excerpts e WHERE e.video_id = v.video_id AND e.tier != 'X') AS n_excerpts,
       (SELECT ROUND(SUM(end_s - start_s) / 3600.0, 3) FROM excerpts e
          WHERE e.video_id = v.video_id AND e.tier != 'X') AS excerpt_hours,
       (SELECT GROUP_CONCAT(DISTINCT split) FROM excerpts e WHERE e.video_id = v.video_id) AS splits,
       (SELECT request_id FROM takedowns tk WHERE tk.video_id = v.video_id LIMIT 1) AS takedown,
       (SELECT action FROM events ev WHERE ev.entity_id = v.video_id ORDER BY ts DESC LIMIT 1) AS last_event,
       (SELECT ts FROM events ev WHERE ev.entity_id = v.video_id ORDER BY ts DESC LIMIT 1) AS last_event_at
FROM videos v
LEFT JOIN channels c ON c.channel_id = v.channel_id
LEFT JOIN downloads d ON d.video_id = v.video_id
LEFT JOIN screening sp ON sp.video_id = v.video_id AND sp.stage = 'probe'
LEFT JOIN screening sf ON sf.video_id = v.video_id AND sf.stage = 'full'
LEFT JOIN (SELECT * FROM triage t1 WHERE reviewed_at = (SELECT MAX(reviewed_at) FROM triage t2
           WHERE t2.video_id = t1.video_id)) t ON t.video_id = v.video_id
LEFT JOIN (SELECT * FROM licence_evidence l1 WHERE checked_at = (SELECT MAX(checked_at) FROM licence_evidence l2
           WHERE l2.video_id = l1.video_id)) le ON le.video_id = v.video_id
{where}
ORDER BY c.title, v.published_at
"""


def info_json_for(row):
    """The yt-dlp .info.json saved next to the original audio (if the video was downloaded)."""
    if row["orig_path"]:
        p = Path(row["orig_path"]).with_suffix(".info.json")
        if p.exists():
            return p
    if row["channel_id"]:
        p = RAW_DIR / row["channel_id"] / f"{row['video_id']}.info.json"
        if p.exists():
            return p
    return None


def pct(x):
    return "" if x is None else round(100 * x, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--all", action="store_true", help="include videos that were never downloaded"
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    where = "" if args.all else "WHERE d.video_id IS NOT NULL"
    rows = db().execute(SQL.format(where=where)).fetchall()
    out = (
        Path(args.out)
        if args.out
        else REPORT_DIR / f"catalog_{date.today().isoformat()}.csv"
    )
    records = []
    for r in rows:
        info, info_path = {}, info_json_for(r)
        if info_path:
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = {}
        upload = info.get("upload_date") or ""
        records.append(
            {
                "video_id": r["video_id"],
                "video_url": f"https://www.youtube.com/watch?v={r['video_id']}",
                "channel_id": r["channel_id"],
                "channel_url": f"https://www.youtube.com/channel/{r['channel_id']}"
                if r["channel_id"]
                else "",
                "channel_title": r["channel_title"] or info.get("channel", ""),
                "title": r["title"] or info.get("title", ""),
                "published_at": r["published_at"],
                "upload_date_ytdlp": f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
                if len(upload) == 8
                else "",
                "uploader": info.get("uploader", ""),
                "duration_min": round(r["duration_s"] / 60, 1)
                if r["duration_s"]
                else "",
                "view_count": info.get("view_count", ""),
                "categories": "; ".join(info.get("categories") or []),
                "tags": "; ".join((info.get("tags") or [])[:15]),
                "chapters": len(info.get("chapters") or []),
                "ytdlp_language": info.get("language") or "",
                "api_audio_language": r["default_audio_lang"],
                "made_for_kids": r["made_for_kids"],
                "api_licence": r["license"],
                "partner_claimed": r["licensed_content"],
                "tier_prelim": r["tier_prelim"],
                "licence_evidence_url": r["wayback_url"],
                "licence_checked_at": r["licence_checked_at"],
                "seed_known_licence": r["seed_known_licence"],
                "metadata_status": r["meta_status"],
                "metadata_reason": r["meta_reason"],
                "discovered_via": r["discovered_via"],
                "discovered_at": r["discovered_at"],
                "channel_vetting": r["vetting"],
                "channel_vetting_note": r["vetting_note"],
                "download_status": r["download_status"],
                "downloaded_at": r["downloaded_at"],
                "orig_codec": r["orig_codec"],
                "orig_sample_rate": r["orig_sr"],
                "orig_channels": r["orig_channels"],
                "orig_bitrate_kbps": round(r["orig_bitrate"] / 1000)
                if r["orig_bitrate"]
                else "",
                "ytdlp_version": r["ytdlp_version"],
                "sha256_original": r["sha256_orig"],
                "sha256_wav": r["sha256_wav"],
                "wav_path": r["wav_path"],
                "info_json": str(info_path) if info_path else "",
                "probe_score": r["probe_score"],
                "full_score": r["full_score"],
                "speech_pct": pct(r["speech_ratio"]),
                "music_pct": pct(r["music_ratio"]),
                "noise_pct": pct(r["noise_ratio"]),
                "urdu_hindi_pct": pct(r["urdu_hindi_frac"])
                if r["lid_windows"]
                else ("unknown" if r["full_score"] is not None else ""),
                "english_pct": pct(r["english_frac"]) if r["lid_windows"] else "",
                "lid_windows": r["lid_windows"],
                "n_speakers": r["n_speakers"],
                "overlap_pct": pct(r["overlap_ratio"]),
                "max_concurrent": r["max_concurrent"],
                "changes_per_min": round(r["changes_per_min"], 1)
                if r["changes_per_min"] is not None
                else "",
                "diar_model": r["diar_model"],
                "rttm_path": r["rttm_path"],
                "triage_decision": r["decision"],
                "triage_tier": r["tier"],
                "triage_reviewer": r["reviewer"],
                "triage_at": r["reviewed_at"],
                "triage_notes": r["notes"],
                "format_type": r["format_type"],
                "n_excerpts": r["n_excerpts"],
                "excerpt_hours": r["excerpt_hours"],
                "splits": r["splits"],
                "takedown": r["takedown"],
                "last_event": r["last_event"],
                "last_event_at": r["last_event_at"],
            }
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(records[0]) if records else ["video_id"])
        w.writeheader()
        w.writerows(records)
    print(f"{len(records)} videos -> {out}")


if __name__ == "__main__":
    main()
