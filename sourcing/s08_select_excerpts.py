"""s08_select_excerpts.py - Step 8: choose 10-15 minute excerpts to send to annotation.

    python s08_select_excerpts.py --target-min 12 --per-video 2 --channel-cap-hours 3

For every video accepted in triage (Tier A, B or C) it slides a window over the machine
diarization, scores each window (overlap, number of speakers, speech density), keeps the best
non-overlapping windows, and moves each boundary to the nearest pause so no turn is cut in half.
A per-channel cap stops one prolific channel from dominating the corpus.
"""
import argparse
import math

from common import db, log_event, now, read_rttm, speech_stats, speech_union


def window_score(segs, s, e):
    inside = [(max(a, s), min(b, e), k) for a, b, k in segs if b > s and a < e]
    speech, overlap, _ = speech_stats(inside)
    n_spk = len({k for _, _, k in inside})
    ovl = min((overlap / speech if speech else 0) / 0.15, 1.0)
    spk = min(math.log2(max(n_spk, 1)) / math.log2(8), 1.0)
    return 0.5 * ovl + 0.3 * spk + 0.2 * (speech / (e - s)), n_spk, (overlap / speech if speech else 0)


def cut_points(segs, duration, min_gap=0.4):
    """Mid-points of pauses of at least min_gap seconds (nobody speaking), plus file start/end."""
    pts, prev_end = [0.0], 0.0
    for s, e in speech_union(segs):
        if s - prev_end >= min_gap:
            pts.append((prev_end + s) / 2)
        prev_end = e
    pts.append(duration)
    return pts


def snap(t, pts, tol=45.0):
    best = min(pts, key=lambda p: abs(p - t))
    return (best, True) if abs(best - t) <= tol else (t, False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-min", type=float, default=12)
    ap.add_argument("--min-min", type=float, default=8)
    ap.add_argument("--max-min", type=float, default=15)
    ap.add_argument("--per-video", type=int, default=2)
    ap.add_argument("--channel-cap-hours", type=float, default=3.0)
    ap.add_argument("--step-s", type=float, default=30)
    args = ap.parse_args()
    con = db()
    rows = con.execute("""
        SELECT v.video_id, v.channel_id, d.duration_s, s.rttm_path, s.score, t.tier
        FROM triage t
        JOIN (SELECT video_id, MAX(reviewed_at) m FROM triage GROUP BY video_id) last
             ON last.video_id = t.video_id AND last.m = t.reviewed_at
        JOIN videos v ON v.video_id = t.video_id
        JOIN downloads d ON d.video_id = t.video_id
        JOIN screening s ON s.video_id = t.video_id AND s.stage = 'full'
        WHERE t.decision = 'accept' AND t.tier IN ('A','B','C')
          AND t.video_id NOT IN (SELECT video_id FROM excerpts)
        ORDER BY s.score DESC""").fetchall()
    used = {r["channel_id"]: r["h"] for r in con.execute(
        "SELECT channel_id, SUM(end_s - start_s)/3600.0 h FROM excerpts GROUP BY channel_id")}
    L = args.target_min * 60
    made = 0
    for r in rows:
        segs, dur = read_rttm(r["rttm_path"]), r["duration_s"]
        if not segs or dur < args.min_min * 60:
            continue
        if dur <= args.max_min * 60:
            candidates = [(window_score(segs, 0, dur), 0.0, dur)]
        else:
            candidates, t = [], 0.0
            while t + L <= dur:
                candidates.append((window_score(segs, t, t + L), t, t + L))
                t += args.step_s
        candidates.sort(key=lambda c: c[0][0], reverse=True)
        pts, chosen = cut_points(segs, dur), []
        for (sc, n_spk, ovl), s, e in candidates:
            if len(chosen) >= args.per_video:
                break
            if used.get(r["channel_id"], 0) >= args.channel_cap_hours:
                break
            if any(s < ce + 60 and e > cs - 60 for cs, ce in chosen):    # keep excerpts apart
                continue
            s2, ok_s = snap(s, pts)
            e2, ok_e = snap(e, pts)
            if not (args.min_min * 60 <= e2 - s2 <= args.max_min * 60 + 90):
                s2, e2 = s, e
            chosen.append((s2, e2))
            ex_id = f"{r['video_id']}__{int(round(s2 * 1000)):09d}"
            con.execute("INSERT OR REPLACE INTO excerpts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (ex_id, r["video_id"], r["channel_id"], round(s2, 3), round(e2, 3),
                         n_spk, round(ovl, 4), round(sc, 4), r["tier"], None, now()))
            log_event(con, "excerpt", ex_id, "selected",
                      {"start": s2, "end": e2, "snapped": [ok_s, ok_e], "score": sc})
            used[r["channel_id"]] = used.get(r["channel_id"], 0) + (e2 - s2) / 3600
            made += 1
        con.commit()
    total = con.execute("SELECT COUNT(*) n, SUM(end_s-start_s)/3600.0 h FROM excerpts").fetchone()
    print(f"selected {made} new excerpts; corpus now {total['n']} excerpts, {total['h'] or 0:.1f} h")


if __name__ == "__main__":
    main()
