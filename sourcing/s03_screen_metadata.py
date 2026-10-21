"""s03_screen_metadata.py - Step 3: metadata screening before any audio is downloaded.

    python s03_screen_metadata.py            # checks every video not yet checked
    python s03_screen_metadata.py --recheck  # re-checks everything (monthly refresh, see Step 11)

Uses videos.list (1 quota unit per 50 videos). Writes meta_status = pass / reject / gone / defer
and a preliminary licence tier:  A? (Creative Commons), A-check (CC but flagged as
partner-claimed content), C? (standard YouTube licence).
Arabic-script titles that use none of the Urdu-only letters (ٹ ڈ ڑ ں ے ہ ھ) are deferred as
"possibly_not_urdu": they are usually Persian or Arabic.
"""

import argparse
import re

import isodate
from common import (
    EXCLUDE_TERMS,
    MAX_DURATION_S,
    MIN_DURATION_S,
    api_error_text,
    db,
    is_quota_error,
    log_event,
    looks_not_urdu,
    now,
    youtube_client,
)
from googleapiclient.errors import HttpError

EXCLUDE_RE = re.compile("|".join(re.escape(t) for t in EXCLUDE_TERMS), re.IGNORECASE)


def decide(item):
    sn, cd, st = item["snippet"], item["contentDetails"], item["status"]
    dur = (
        isodate.parse_duration(cd["duration"]).total_seconds()
        if cd.get("duration")
        else 0
    )
    text = f"{sn.get('title', '')} {sn.get('description', '')[:500]}"
    if st.get("privacyStatus") != "public":
        return dur, "reject", "not_public"
    if st.get("madeForKids"):
        return dur, "reject", "made_for_kids"
    if sn.get("liveBroadcastContent") in ("live", "upcoming"):
        return dur, "defer", "live_or_upcoming"
    if dur < MIN_DURATION_S:
        return dur, "reject", "too_short"
    if dur > MAX_DURATION_S:
        return dur, "defer", "very_long_manual_check"
    if EXCLUDE_RE.search(text):
        return dur, "reject", "excluded_genre_term"
    if looks_not_urdu(sn.get("title", "")):
        return (
            dur,
            "defer",
            "possibly_not_urdu",
        )  # Persian/Arabic script without Urdu letters
    return dur, "pass", ""


def tier_prelim(item):
    lic = item["status"].get("license")
    if lic == "creativeCommon":
        return "A-check" if item["contentDetails"].get("licensedContent") else "A?"
    return "C?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recheck", action="store_true")
    args = ap.parse_args()
    yt, con = youtube_client(), db()
    where = "1=1" if args.recheck else "meta_checked_at IS NULL"
    ids = [
        r["video_id"] for r in con.execute(f"SELECT video_id FROM videos WHERE {where}")
    ]
    print(f"{len(ids)} videos to check")
    try:
        for i in range(0, len(ids), 50):
            batch = ids[i : i + 50]
            resp = (
                yt.videos()
                .list(
                    part="snippet,contentDetails,status",
                    id=",".join(batch),
                    maxResults=50,
                )
                .execute()
            )
            seen = set()
            for it in resp.get("items", []):
                seen.add(it["id"])
                dur, status, reason = decide(it)
                old = con.execute(
                    "SELECT license FROM videos WHERE video_id=?", (it["id"],)
                ).fetchone()
                new_lic = it["status"].get("license")
                if old and old["license"] and old["license"] != new_lic:
                    log_event(
                        con,
                        "video",
                        it["id"],
                        "licence_changed",
                        {"from": old["license"], "to": new_lic},
                    )
                sn = it["snippet"]
                con.execute(
                    """UPDATE videos SET channel_id=?, title=?, description=?, published_at=?,
                               duration_s=?, license=?, licensed_content=?, made_for_kids=?,
                               default_audio_lang=?, live=?, privacy=?, meta_checked_at=?,
                               meta_status=?, meta_reason=?, tier_prelim=? WHERE video_id=?""",
                    (
                        sn["channelId"],
                        sn.get("title"),
                        sn.get("description", "")[:5000],
                        sn.get("publishedAt"),
                        dur,
                        new_lic,
                        int(bool(it["contentDetails"].get("licensedContent"))),
                        int(bool(it["status"].get("madeForKids"))),
                        sn.get("defaultAudioLanguage"),
                        sn.get("liveBroadcastContent"),
                        it["status"].get("privacyStatus"),
                        now(),
                        status,
                        reason,
                        tier_prelim(it),
                        it["id"],
                    ),
                )
                con.execute(
                    "INSERT OR IGNORE INTO channels(channel_id, title, discovered_via, discovered_at)"
                    " VALUES (?,?,?,?)",
                    (
                        sn["channelId"],
                        sn.get("channelTitle"),
                        f"video:{it['id']}",
                        now(),
                    ),
                )
            for gone in set(batch) - seen:  # deleted, private or region-blocked
                con.execute(
                    "UPDATE videos SET meta_checked_at=?, meta_status='gone', "
                    "meta_reason='not_returned_by_api' WHERE video_id=?",
                    (now(), gone),
                )
                log_event(con, "video", gone, "gone")
            con.commit()
    except HttpError as err:
        con.commit()
        if is_quota_error(err):
            print("Daily quota exhausted - rerun tomorrow; progress is saved.")
            return
        raise SystemExit(f"YouTube API error (key hidden): {api_error_text(err)}")
    for r in con.execute(
        "SELECT meta_status, tier_prelim, COUNT(*) n FROM videos "
        "GROUP BY meta_status, tier_prelim ORDER BY n DESC"
    ):
        print(dict(r))


if __name__ == "__main__":
    main()
