"""s02_channels.py - Step 2: channel vetting and channel expansion.

    python s02_channels.py pending                      # list channels waiting for a human decision
    python s02_channels.py vet UCxxxx approved "independent Urdu podcast, own content"
    python s02_channels.py vet UCyyyy rejected "re-uploads TV clips"
    python s02_channels.py expand --max-videos 500      # list uploads of every approved channel

Expansion uses channels.list + playlistItems.list (1 quota unit per 50 videos),
which is ~100x cheaper than search.list.
"""

import argparse

from common import api_error_text, db, is_quota_error, log_event, now, youtube_client
from googleapiclient.errors import HttpError


def pending(con):
    rows = con.execute("""SELECT c.channel_id, c.title, COUNT(v.video_id) AS n,
                                 SUM(v.license='creativeCommon') AS n_cc
                          FROM channels c LEFT JOIN videos v ON v.channel_id = c.channel_id
                          WHERE c.vetting = 'pending' GROUP BY c.channel_id ORDER BY n DESC""").fetchall()
    for r in rows:
        print(
            f"{r['channel_id']}  videos={r['n']:<4} cc={r['n_cc'] or 0:<4} "
            f"https://www.youtube.com/channel/{r['channel_id']}  {r['title']}"
        )
    print(f"\n{len(rows)} channels pending vetting")


def vet(con, channel_id, decision, note):
    con.execute(
        "UPDATE channels SET vetting=?, vetting_note=? WHERE channel_id=?",
        (decision, note, channel_id),
    )
    log_event(con, "channel", channel_id, f"vetted:{decision}", note)
    con.commit()


def expand(con, max_videos):
    yt = youtube_client()
    todo = [
        r["channel_id"]
        for r in con.execute(
            "SELECT channel_id FROM channels WHERE vetting='approved' AND expanded_at IS NULL"
        )
    ]
    try:
        for i in range(0, len(todo), 50):  # channels.list takes up to 50 ids
            batch = todo[i : i + 50]
            resp = (
                yt.channels()
                .list(part="contentDetails,snippet", id=",".join(batch), maxResults=50)
                .execute()
            )
            for ch in resp.get("items", []):
                uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
                con.execute(
                    "UPDATE channels SET uploads_playlist=?, title=? WHERE channel_id=?",
                    (uploads, ch["snippet"]["title"], ch["id"]),
                )
                n, token = 0, None
                while n < max_videos:
                    page = (
                        yt.playlistItems()
                        .list(
                            part="contentDetails",
                            playlistId=uploads,
                            maxResults=min(50, max_videos - n),
                            pageToken=token,
                        )
                        .execute()
                    )
                    for it in page.get("items", [])[: max_videos - n]:
                        con.execute(
                            "INSERT OR IGNORE INTO videos(video_id, channel_id, discovered_via,"
                            " discovered_at) VALUES (?,?,?,?)",
                            (
                                it["contentDetails"]["videoId"],
                                ch["id"],
                                f"channel:{ch['id']}",
                                now(),
                            ),
                        )
                        n += 1
                    token = page.get("nextPageToken")
                    if not token:
                        break
                con.execute(
                    "UPDATE channels SET expanded_at=? WHERE channel_id=?",
                    (now(), ch["id"]),
                )
                log_event(con, "channel", ch["id"], "expanded", {"videos_listed": n})
                con.commit()
                print(f"{ch['snippet']['title']}: {n} uploads listed")
    except HttpError as err:
        con.commit()
        if is_quota_error(err):
            print("Daily quota exhausted - rerun tomorrow; progress is saved.")
            return
        raise SystemExit(f"YouTube API error (key hidden): {api_error_text(err)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending")
    v = sub.add_parser("vet")
    v.add_argument("channel_id")
    v.add_argument("decision", choices=["approved", "rejected", "pending"])
    v.add_argument("note", nargs="?", default="")
    e = sub.add_parser("expand")
    e.add_argument("--max-videos", type=int, default=500)
    args = ap.parse_args()
    con = db()
    if args.cmd == "pending":
        pending(con)
    elif args.cmd == "vet":
        vet(con, args.channel_id, args.decision, args.note)
    else:
        expand(con, args.max_videos)


if __name__ == "__main__":
    main()
