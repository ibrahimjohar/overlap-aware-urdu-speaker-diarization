"""s09_assign_splits.py - Step 9a: channel-disjoint train / dev / test assignment.

    python s09_assign_splits.py --test 0.10 --dev 0.10 --seed 2026

Whole channels go to exactly one split, so a podcast host can never appear in both training
and test data. Channels that already have a split keep it (assignments are frozen once made);
new channels are used to top up whichever split is furthest below its target.
Dev/test prefer redistributable channels (Tier A/B) so the evaluation sets do not decay when
YouTube videos disappear.
"""
import argparse
import random

from common import db, log_event


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", type=float, default=0.10)
    ap.add_argument("--dev", type=float, default=0.10)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()
    con = db()
    rows = con.execute("""SELECT channel_id, SUM(end_s-start_s)/3600.0 h,
                                 MIN(tier) best_tier, MAX(split) split
                          FROM excerpts GROUP BY channel_id""").fetchall()
    total = sum(r["h"] for r in rows)
    have = {"train": 0.0, "dev": 0.0, "test": 0.0}
    new = []
    for r in rows:
        if r["split"]:
            have[r["split"]] += r["h"]
        else:
            new.append(r)
    target = {"test": args.test * total, "dev": args.dev * total}
    rng = random.Random(args.seed)
    rng.shuffle(new)
    new.sort(key=lambda r: r["best_tier"] not in ("A", "B"))   # redistributable channels first (stable sort)
    for r in new:
        split = "train"
        for s in ("test", "dev"):
            deficit = target[s] - have[s]
            if deficit > 0 and r["h"] <= max(deficit * 2, 0.5):     # stop one big channel overshooting
                split = s
                break
        have[split] += r["h"]
        con.execute("UPDATE excerpts SET split=? WHERE channel_id=?", (split, r["channel_id"]))
        log_event(con, "channel", r["channel_id"], "split_assigned", {"split": split, "hours": r["h"]})
    con.commit()
    for s, h in have.items():
        print(f"{s:5s} {h:6.2f} h  ({h / total:.0%})" if total else s)


if __name__ == "__main__":
    main()
