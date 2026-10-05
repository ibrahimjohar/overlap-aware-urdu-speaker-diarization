"""s04_licence_evidence.py - Step 4: capture durable evidence of the licence at collection time.

For every Creative Commons candidate that passed metadata screening, ask the Internet
Archive's Wayback Machine to snapshot the watch page, and record the API licence value with
a timestamp. Tier A material needs this record in case an uploader later switches the video
back to the standard licence.

    python s04_licence_evidence.py --limit 200

The public Save Page Now endpoint is rate limited: keep the default pause, and if you get
HTTP 429 wait an hour. Also take a manual screenshot of the licence line in the description
for every video that ends up in Tier A (save it under licence_evidence/<video_id>.png).
"""
import argparse
import time

import requests

from common import db, log_event, now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--pause", type=float, default=15.0, help="seconds between Wayback requests")
    args = ap.parse_args()
    con = db()
    rows = con.execute("""SELECT v.video_id, v.license FROM videos v
                          WHERE v.meta_status='pass' AND v.tier_prelim LIKE 'A%'
                          AND NOT EXISTS (SELECT 1 FROM licence_evidence e
                                          WHERE e.video_id=v.video_id AND e.wayback_url IS NOT NULL)
                          LIMIT ?""", (args.limit,)).fetchall()
    sess = requests.Session()
    sess.headers["User-Agent"] = "FAST-NUCES FYP Urdu corpus licence evidence (contact: see README)"
    for r in rows:
        url = f"https://www.youtube.com/watch?v={r['video_id']}"
        snap, note = None, ""
        try:
            resp = sess.get(f"https://web.archive.org/save/{url}", timeout=120, allow_redirects=True)
            if "/web/" in resp.url:
                snap = resp.url
            else:
                snap = resp.headers.get("Content-Location")
                snap = f"https://web.archive.org{snap}" if snap else None
            note = f"http {resp.status_code}"
            if resp.status_code == 429:
                print("Rate limited by the Wayback Machine - stopping; rerun later.")
                break
        except requests.RequestException as exc:
            note = f"error: {exc}"
        con.execute("INSERT INTO licence_evidence VALUES (?,?,?,?,?,?)",
                    (r["video_id"], now(), r["license"], snap, None, note))
        log_event(con, "video", r["video_id"], "licence_evidence", {"wayback": snap, "note": note})
        con.commit()
        print(r["video_id"], snap or note)
        time.sleep(args.pause)


if __name__ == "__main__":
    main()
