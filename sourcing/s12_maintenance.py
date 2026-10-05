"""s12_maintenance.py - Step 11: monthly re-check report and takedown handling.

Monthly (after running `python s03_screen_metadata.py --recheck`):
    python s12_maintenance.py report
        -> lists selected excerpts whose source video disappeared or changed licence

When a rights holder or a person in a recording asks for removal:
    python s12_maintenance.py takedown --request-id TD-2026-001 --video VIDEO_ID \
        --requester "channel owner, email on file" --action "excerpts removed, audio deleted"
        -> marks every excerpt from that video as tier X, deletes its local audio and machine RTTM,
           and records the request. Rebuild the hand-off package afterwards.
"""
import argparse
import json
import os

from common import RTTM_DIR, db, log_event, now


def report(con):
    gone = con.execute("""SELECT DISTINCT e.video_id, v.meta_reason FROM excerpts e JOIN videos v USING(video_id)
                          WHERE v.meta_status='gone' AND e.tier != 'X'""").fetchall()
    changed = con.execute("""SELECT entity_id, detail, ts FROM events WHERE action='licence_changed'
                             AND entity_id IN (SELECT video_id FROM excerpts WHERE tier != 'X')""").fetchall()
    print(f"{len(gone)} excerpted videos no longer available on YouTube:")
    for r in gone:
        print("  ", r["video_id"], r["meta_reason"])
    print(f"{len(changed)} licence changes affecting excerpts:")
    for r in changed:
        d = json.loads(r["detail"])
        print(f"   {r['entity_id']} {d['from']} -> {d['to']} (seen {r['ts']})")
    print("\nTier A material stays usable under the licence recorded at collection time (keep the "
          "evidence); still review each case with your supervisor. Tier C items that are gone can no "
          "longer be re-downloaded by users - consider replacing them.")


def takedown(con, request_id, video, requester, action):
    con.execute("INSERT INTO takedowns VALUES (?,?,?,?,?,?)",
                (request_id, now(), requester, video, action, None))
    con.execute("UPDATE excerpts SET tier='X', split=NULL WHERE video_id=?", (video,))
    row = con.execute("SELECT orig_path, wav_path FROM downloads WHERE video_id=?", (video,)).fetchone()
    removed = []
    for p in ([row["orig_path"], row["wav_path"]] if row else []) + [str(RTTM_DIR / f"{video}.rttm")]:
        if p and os.path.exists(p):
            os.remove(p)
            removed.append(p)
    con.execute("UPDATE downloads SET status='removed_takedown' WHERE video_id=?", (video,))
    con.execute("UPDATE takedowns SET completed_at=? WHERE request_id=?", (now(), request_id))
    log_event(con, "video", video, "takedown", {"request": request_id, "removed_files": removed})
    con.commit()
    print(f"takedown {request_id}: {video} removed ({len(removed)} files). Rebuild the package.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report")
    t = sub.add_parser("takedown")
    t.add_argument("--request-id", required=True)
    t.add_argument("--video", required=True)
    t.add_argument("--requester", required=True)
    t.add_argument("--action", default="excerpts removed, audio deleted")
    args = ap.parse_args()
    con = db()
    if args.cmd == "report":
        report(con)
    else:
        takedown(con, args.request_id, args.video, args.requester, args.action)


if __name__ == "__main__":
    main()
