"""s01_discover.py - Step 1: keyword discovery through the YouTube Data API.

Runs every keyword over a series of publication-date windows so that each query
returns fresh results (a single query only ever pages through a few hundred hits).

    python s01_discover.py --keywords keywords.txt --track cc  --since 2012-01-01
    python s01_discover.py --keywords keywords.txt --track all --since 2012-01-01

--track cc   -> only Creative Commons videos (candidates for Tier A)
--track all  -> every licence (candidates for Tier B / Tier C)
Cost: 100 quota units per results page. Default quota: 10,000 units/day/project.
"""
import argparse
from datetime import date, datetime

from googleapiclient.errors import HttpError

from common import db, is_quota_error, log_event, now, youtube_client


def month_windows(since: date, until: date, months: int):
    start = since
    while start < until:
        m = start.month - 1 + months
        end = date(start.year + m // 12, m % 12 + 1, 1)
        yield start, min(end, until)
        start = end


def rfc3339(d: date) -> str:
    return datetime(d.year, d.month, d.day).strftime("%Y-%m-%dT00:00:00Z")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keywords", required=True, help="UTF-8 text file, one keyword per line")
    ap.add_argument("--track", choices=["cc", "all"], required=True)
    ap.add_argument("--since", default="2012-01-01")
    ap.add_argument("--until", default=date.today().isoformat())
    ap.add_argument("--window-months", type=int, default=6)
    ap.add_argument("--max-pages", type=int, default=3, help="pages of 50 results per window")
    ap.add_argument("--order", default="date", choices=["date", "relevance", "viewCount"])
    args = ap.parse_args()

    yt, con = youtube_client(), db()
    keywords = [k.strip() for k in open(args.keywords, encoding="utf-8")
                if k.strip() and not k.startswith("#")]
    since, until = date.fromisoformat(args.since), date.fromisoformat(args.until)

    for kw in keywords:
        for w_start, w_end in month_windows(since, until, args.window_months):
            done = con.execute("SELECT 1 FROM keywords WHERE keyword=? AND track=? AND window_start=?",
                               (kw, args.track, w_start.isoformat())).fetchone()
            if done:
                continue                              # resumable: skip finished windows
            params = dict(part="snippet", q=kw, type="video", maxResults=50,
                          relevanceLanguage="ur", regionCode="PK", order=args.order,
                          videoDuration="any",
                          publishedAfter=rfc3339(w_start), publishedBefore=rfc3339(w_end))
            if args.track == "cc":
                params["videoLicense"] = "creativeCommon"
            n, pages, token = 0, 0, None
            try:
                while pages < args.max_pages:
                    if token:
                        params["pageToken"] = token
                    resp = yt.search().list(**params).execute()
                    pages += 1
                    for item in resp.get("items", []):
                        vid = item["id"]["videoId"]
                        ch = item["snippet"]["channelId"]
                        con.execute("INSERT OR IGNORE INTO videos(video_id, channel_id, discovered_via,"
                                    " discovered_at, title, published_at) VALUES (?,?,?,?,?,?)",
                                    (vid, ch, f"kw:{kw}|{args.track}", now(),
                                     item["snippet"]["title"], item["snippet"]["publishedAt"]))
                        con.execute("INSERT OR IGNORE INTO channels(channel_id, title, discovered_via,"
                                    " discovered_at) VALUES (?,?,?,?)",
                                    (ch, item["snippet"]["channelTitle"], f"kw:{kw}", now()))
                        n += 1
                    token = resp.get("nextPageToken")
                    if not token:
                        break
            except HttpError as err:
                con.commit()
                if is_quota_error(err):
                    print("Daily quota exhausted - rerun tomorrow; progress is saved.")
                    return
                raise
            con.execute("INSERT OR REPLACE INTO keywords VALUES (?,?,?,?,?,?,?)",
                        (kw, args.track, w_start.isoformat(), w_end.isoformat(), pages, n, now()))
            log_event(con, "keyword", kw, "searched",
                      {"track": args.track, "window": [str(w_start), str(w_end)], "results": n})
            con.commit()
            print(f"{kw!r} {w_start}..{w_end} [{args.track}] -> {n} results")


if __name__ == "__main__":
    main()
