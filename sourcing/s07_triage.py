"""s07_triage.py - Step 7: human triage through a shared spreadsheet.

    python s07_triage.py export --top 150 --out triage_batch_03.csv
        -> open in Google Sheets / Excel, split rows between reviewers, fill the empty columns
    python s07_triage.py import triage_batch_03_done.csv

Allowed values (anything else is rejected on import):
  decision:          accept | reject | second_opinion
  tier:              A | B | C | X        (see Section 3.8)
  yes/no columns:    yes | no | unsure
  format_type:       podcast | talk_show | panel | interview | debate | vlog_group | classroom | other
"""
import argparse
import csv
import sys

from common import db, log_event, now

YN = {"yes", "no", "unsure"}
FILL = ["reviewer", "decision", "tier", "original_creator", "conversational", "urdu_dominant",
        "music_bed", "inserted_clips", "minors", "private_info", "sensitive", "format_type", "notes"]
ALLOWED = {
    "decision": {"accept", "reject", "second_opinion"},
    "tier": {"A", "B", "C", "X"},
    "original_creator": YN, "conversational": YN, "urdu_dominant": YN, "music_bed": YN,
    "inserted_clips": YN, "minors": YN, "private_info": YN, "sensitive": YN,
    "format_type": {"podcast", "talk_show", "panel", "interview", "debate", "vlog_group",
                    "classroom", "other"},
}


def export(con, top, out):
    rows = con.execute("""SELECT v.video_id, v.title, c.title AS channel, v.license, v.tier_prelim,
                                 v.duration_s, s.score, s.n_speakers, s.overlap_ratio, s.urdu_hindi_frac,
                                 s.music_ratio, s.stage
                          FROM screening s JOIN videos v USING(video_id)
                          JOIN channels c ON c.channel_id = v.channel_id
                          WHERE s.stage = 'full'
                            AND v.video_id NOT IN (SELECT video_id FROM triage WHERE decision != 'second_opinion')
                          ORDER BY s.score DESC LIMIT ?""", (top,)).fetchall()
    with open(out, "w", newline="", encoding="utf-8-sig") as f:      # utf-8-sig so Excel shows Urdu
        w = csv.writer(f)
        w.writerow(["video_id", "url", "title", "channel", "api_license", "tier_prelim", "minutes",
                    "score", "hyp_speakers", "hyp_overlap", "urdu_frac", "music_frac", "stage"] + FILL)
        for r in rows:
            w.writerow([r["video_id"], f"https://www.youtube.com/watch?v={r['video_id']}", r["title"],
                        r["channel"], r["license"], r["tier_prelim"], round((r["duration_s"] or 0) / 60, 1),
                        r["score"], r["n_speakers"], f"{r['overlap_ratio']:.1%}",
                        f"{r['urdu_hindi_frac']:.0%}", f"{r['music_ratio']:.0%}", r["stage"]]
                       + [""] * len(FILL))
    print(f"wrote {len(rows)} rows to {out}")


def import_(con, path):
    errors, ok = [], 0
    with open(path, encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            if not row.get("decision"):
                continue                                              # not reviewed yet
            vals = {k: (row.get(k) or "").strip() for k in FILL}
            for k, allowed in ALLOWED.items():
                if vals[k] and vals[k] not in allowed:
                    errors.append(f"line {i}: {k}={vals[k]!r} not in {sorted(allowed)}")
            if vals["decision"] == "accept" and vals["tier"] == "A" and vals["original_creator"] != "yes":
                errors.append(f"line {i}: Tier A requires original_creator=yes")
            if vals["decision"] == "accept" and "yes" in (vals["minors"], vals["private_info"]):
                errors.append(f"line {i}: accepted despite minors/private_info=yes - needs second_opinion")
            if not vals["reviewer"]:
                errors.append(f"line {i}: reviewer missing")
            if errors and errors[-1].startswith(f"line {i}:"):
                continue
            con.execute("INSERT INTO triage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row["video_id"], vals["reviewer"], now(), vals["decision"], vals["tier"],
                         vals["original_creator"], vals["conversational"], vals["urdu_dominant"],
                         vals["music_bed"], vals["inserted_clips"], vals["minors"], vals["private_info"],
                         vals["sensitive"], vals["format_type"], vals["notes"]))
            log_event(con, "video", row["video_id"], f"triage:{vals['decision']}", vals, actor=vals["reviewer"])
            ok += 1
    con.commit()
    print(f"imported {ok} decisions")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--top", type=int, default=150)
    e.add_argument("--out", required=True)
    i = sub.add_parser("import")
    i.add_argument("path")
    args = ap.parse_args()
    con = db()
    export(con, args.top, args.out) if args.cmd == "export" else import_(con, args.path)


if __name__ == "__main__":
    main()
