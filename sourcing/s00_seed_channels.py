"""s00_seed_channels.py - Step 0b: onboard known channels (podcasts, talk shows, news) and measure
what licence their uploads ACTUALLY carry before anyone spends time on them.

    python s00_seed_channels.py --seeds config/seed_channels.csv --sample 100

For every row in the seed file (a @handle, a channel URL, or a UC... channel ID) it:
  1. resolves the channel through the YouTube Data API (1 quota unit)
  2. lists its most recent uploads (--sample, default 100) and reads each video's licence,
     partner-claim flag, length and title (about 4 units per 100 videos)
  3. stores a profile row in the channel_profile table and adds the channel (vetting = pending)
     and the sampled video IDs to the database, so s03 / s02 / s05 can carry on as usual
  4. writes a ranked CSV report to <corpus>/reports/seed_profile_<date>.csv

The licence mix decides the realistic release tier for a channel:
  - mostly Creative Commons and NOT partner-claimed  -> Tier A candidate (still check originality)
  - Creative Commons but partner-claimed             -> usually re-uploads of TV/other owners' content
  - public domain (declared in the seed file, e.g. US-government VOA) -> Tier A candidate, but
    third-party clips inside programmes (agency footage) are not public domain
  - standard YouTube licence                         -> Tier C (reference-only) or Tier B with permission
Nothing is approved automatically: approval stays a human decision (s02_channels.py vet).
Handles in the seed file are best guesses; unresolved rows are listed so you can fix them.
"""

import argparse
import csv
import re
import statistics
from datetime import date

import isodate
from googleapiclient.errors import HttpError

from common import (
    REPORT_DIR,
    URDU_ONLY_LETTERS,
    api_error_text,
    db,
    is_quota_error,
    log_event,
    now,
    youtube_client,
)

URL_HANDLE = re.compile(r"youtube\.com/@([^/?#]+)")
URL_CHANNEL = re.compile(r"youtube\.com/channel/(UC[\w-]{22})")


def parse_ref(ref):
    """Return ('id', UC...) or ('handle', name) or (None, None) for an unsupported URL."""
    ref = ref.strip()
    if m := URL_CHANNEL.search(ref):
        return "id", m.group(1)
    if m := URL_HANDLE.search(ref):
        return "handle", m.group(1)
    if re.fullmatch(r"UC[\w-]{22}", ref):
        return "id", ref
    if ref.startswith("@"):
        return "handle", ref[1:]
    if "/" not in ref and ref:
        return "handle", ref
    return None, None


def resolve(yt, ref):
    kind, value = parse_ref(ref)
    if kind is None:
        return None
    params = {"part": "snippet,contentDetails,statistics"}
    params["id" if kind == "id" else "forHandle"] = value
    items = yt.channels().list(**params).execute().get("items", [])
    return items[0] if items else None


def sample_uploads(yt, uploads_playlist, n):
    ids, token = [], None
    while len(ids) < n:
        page = (
            yt.playlistItems()
            .list(
                part="contentDetails",
                playlistId=uploads_playlist,
                maxResults=min(50, n - len(ids)),
                pageToken=token,
            )
            .execute()
        )
        ids += [it["contentDetails"]["videoId"] for it in page.get("items", [])]
        token = page.get("nextPageToken")
        if not token:
            break
    return ids[:n]


def video_details(yt, ids):
    out = []
    for i in range(0, len(ids), 50):
        resp = (
            yt.videos()
            .list(
                part="snippet,contentDetails,status",
                id=",".join(ids[i : i + 50]),
                maxResults=50,
            )
            .execute()
        )
        out += resp.get("items", [])
    return out


def profile(videos):
    n = len(videos)
    if n == 0:
        return dict(
            n_checked=0,
            cc_share=0.0,
            claimed_share=0.0,
            long_share=0.0,
            median_min=0.0,
            urdu_title_share=0.0,
        )
    mins = [
        isodate.parse_duration(
            v["contentDetails"].get("duration", "PT0S")
        ).total_seconds()
        / 60
        for v in videos
    ]
    return dict(
        n_checked=n,
        cc_share=sum(v["status"].get("license") == "creativeCommon" for v in videos)
        / n,
        claimed_share=sum(
            bool(v["contentDetails"].get("licensedContent")) for v in videos
        )
        / n,
        long_share=sum(m >= 8 for m in mins) / n,
        median_min=statistics.median(mins),
        urdu_title_share=sum(
            bool(URDU_ONLY_LETTERS & set(v["snippet"].get("title", ""))) for v in videos
        )
        / n,
    )


def suggest(p, known_licence):
    if p["n_checked"] == 0:
        return "no public uploads found"
    if known_licence == "pd":
        return "PUBLIC DOMAIN source: Tier A candidate (exclude third-party clips); vet first"
    if p["cc_share"] >= 0.5 and p["claimed_share"] < 0.2:
        return "CC, not claimed: Tier A candidate; vet first and check originality"
    if p["cc_share"] >= 0.5:
        return "CC label but partner-claimed: likely re-uploads; vet carefully (not Tier A)"
    if p["cc_share"] > 0:
        return "mixed licences: CC videos may be Tier A individually; rest Tier C/B"
    return "standard licence: Tier C (reference-only) or Tier B with written permission"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="config/seed_channels.csv")
    ap.add_argument(
        "--sample", type=int, default=100, help="recent uploads to inspect per channel"
    )
    ap.add_argument(
        "--only", default="", help="only rows whose category matches (e.g. podcast)"
    )
    args = ap.parse_args()
    yt, con = youtube_client(), db()
    with open(args.seeds, encoding="utf-8-sig") as f:
        rows = [
            r
            for r in csv.DictReader(f)
            if (r.get("handle_or_url") or "").strip()
            and not r["category"].startswith("#")
        ]
    if args.only:
        rows = [r for r in rows if r["category"] == args.only]
    report, unresolved, failed, stamp = [], [], [], now()
    for r in rows:
        ref, cat = r["handle_or_url"].strip(), r["category"].strip()
        known = (r.get("known_licence") or "unknown").strip().lower()
        try:
            ch = resolve(yt, ref)
            if not ch:
                unresolved.append((cat, r.get("name", ""), ref))
                print(f"  ? could not resolve {ref}  ({r.get('name', '')})")
                continue
            cid, title = ch["id"], ch["snippet"]["title"]
            uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
            vids = video_details(yt, sample_uploads(yt, uploads, args.sample))
        except HttpError as err:
            con.commit()
            if is_quota_error(err):
                print(
                    "Daily quota exhausted - report below covers the channels done so far; rerun tomorrow."
                )
                break
            failed.append((cat, r.get("name", ""), ref, api_error_text(err)))
            print(f"  ! {r.get('name', '') or ref}: {api_error_text(err)} - skipped")
            continue
        p = profile(vids)
        p["suggestion"] = suggest(p, known)
        stats = ch.get("statistics", {})
        subs = int(stats["subscriberCount"]) if stats.get("subscriberCount") else None
        nvid = int(stats["videoCount"]) if stats.get("videoCount") else None
        con.execute(
            "INSERT OR IGNORE INTO channels(channel_id, title, uploads_playlist, discovered_via,"
            " discovered_at) VALUES (?,?,?,?,?)",
            (cid, title, uploads, f"seed:{cat}", now()),
        )
        con.execute(
            "UPDATE channels SET uploads_playlist=?, title=? WHERE channel_id=?",
            (uploads, title, cid),
        )
        for v in vids:
            con.execute(
                "INSERT OR IGNORE INTO videos(video_id, channel_id, discovered_via, discovered_at)"
                " VALUES (?,?,?,?)",
                (v["id"], cid, f"seed:{cid}", now()),
            )
        con.execute(
            """INSERT OR REPLACE INTO channel_profile VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cid,
                stamp,
                cat,
                ref,
                known,
                p["n_checked"],
                p["cc_share"],
                p["claimed_share"],
                p["long_share"],
                p["median_min"],
                p["urdu_title_share"],
                subs,
                nvid,
                p["suggestion"],
            ),
        )
        log_event(
            con, "channel", cid, "seed_profiled", {**p, "seed": ref, "category": cat}
        )
        con.commit()
        vetting = con.execute(
            "SELECT vetting FROM channels WHERE channel_id=?", (cid,)
        ).fetchone()[0]
        report.append(
            dict(
                category=cat,
                name=r.get("name", ""),
                channel_title=title,
                channel_id=cid,
                url=f"https://www.youtube.com/channel/{cid}",
                vetting=vetting,
                subscribers=subs,
                videos=nvid,
                checked=p["n_checked"],
                cc_pct=round(100 * p["cc_share"]),
                claimed_pct=round(100 * p["claimed_share"]),
                long_pct=round(100 * p["long_share"]),
                median_min=round(p["median_min"], 1),
                urdu_title_pct=round(100 * p["urdu_title_share"]),
                known_licence=known,
                suggestion=p["suggestion"],
                notes=r.get("notes", ""),
            )
        )
        print(
            f"  {title[:40]:40s} cc={100 * p['cc_share']:3.0f}% claimed={100 * p['claimed_share']:3.0f}% "
            f"long={100 * p['long_share']:3.0f}% urdu-titles={100 * p['urdu_title_share']:3.0f}%  "
            f"-> {p['suggestion'].split(':')[0]}"
        )
    report.sort(
        key=lambda x: (
            x["known_licence"] != "pd",
            -x["cc_pct"] + x["claimed_pct"],
            -x["long_pct"],
        )
    )
    out = REPORT_DIR / f"seed_profile_{date.today().isoformat()}.csv"
    if report:
        with open(
            out, "w", newline="", encoding="utf-8-sig"
        ) as f:  # utf-8-sig: Excel shows Urdu
            w = csv.DictWriter(f, fieldnames=list(report[0]))
            w.writeheader()
            w.writerows(report)
        print(f"\n{len(report)} channels profiled -> {out}")
    if unresolved:
        print(
            f"\n{len(unresolved)} seeds could not be resolved. Open the channel on YouTube, copy its URL "
            "(youtube.com/@handle or youtube.com/channel/UC...) into the seed file, and rerun:"
        )
        for cat, name, ref in unresolved:
            print(f"  [{cat}] {name}: {ref}")
    if failed:
        print(
            f"\n{len(failed)} channels resolved but could not be profiled (e.g. uploads hidden):"
        )
        for cat, name, ref, why in failed:
            print(f"  [{cat}] {name}: {ref}  ({why})")


if __name__ == "__main__":
    main()
