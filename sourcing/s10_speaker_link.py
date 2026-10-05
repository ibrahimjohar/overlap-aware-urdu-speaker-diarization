"""s10_speaker_link.py - Step 9b: flag the same person appearing in different splits.

    python s10_speaker_link.py --threshold 0.55 --out speaker_link_review.csv

Channel-disjoint splits still leak when a guest, anchor or politician appears on several
channels. For every excerpt this script embeds each machine-hypothesised speaker (up to 30 s
of their longest non-overlapped turns), compares speakers across different splits with cosine
similarity, and writes pairs above the threshold to a CSV for a human to listen to and resolve
(move a channel, or drop the excerpt). The threshold is a starting point: calibrate it on a few
known same/different pairs from your pilot.
"""
import argparse
import csv
import os

import numpy as np

from common import EMB_DIR, HF_TOKEN, db, read_rttm, speech_stats

EMB_MODEL = os.environ.get("EMB_MODEL", "pyannote/wespeaker-voxceleb-resnet34-LM")


def load_inference():
    from pyannote.audio import Inference, Model
    try:
        model = Model.from_pretrained(EMB_MODEL, token=HF_TOKEN)
    except TypeError:
        model = Model.from_pretrained(EMB_MODEL, use_auth_token=HF_TOKEN)
    return Inference(model, window="whole")


def clean_turns(segs, spk, s0, e0):
    """Turns of one speaker inside the excerpt that overlap no-one else, longest first."""
    mine = [(max(a, s0), min(b, e0)) for a, b, k in segs if k == spk and b > s0 and a < e0]
    others = [(a, b, k) for a, b, k in segs if k != spk]
    clean = [(a, b) for a, b in mine
             if b - a >= 1.5 and speech_stats([(a, b, spk)] + [o for o in others if o[1] > a and o[0] < b])[1] == 0]
    return sorted(clean, key=lambda t: t[1] - t[0], reverse=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.55)
    ap.add_argument("--out", default="speaker_link_review.csv")
    args = ap.parse_args()
    from pyannote.core import Segment
    con = db()
    inference = load_inference()
    rows = con.execute("""SELECT e.excerpt_id, e.video_id, e.split, e.start_s, e.end_s, d.wav_path, s.rttm_path
                          FROM excerpts e JOIN downloads d USING(video_id)
                          JOIN screening s ON s.video_id=e.video_id AND s.stage='full'
                          WHERE e.split IS NOT NULL""").fetchall()
    items = []                                   # (excerpt_id, split, speaker, vector)
    for r in rows:
        cache = EMB_DIR / f"{r['excerpt_id']}.npz"
        if cache.exists():
            z = np.load(cache, allow_pickle=True)
            items += [(r["excerpt_id"], r["split"], k, z[k]) for k in z.files]
            continue
        segs, vecs = read_rttm(r["rttm_path"]), {}
        for spk in sorted({k for _, _, k in segs}):
            turns, got, parts = clean_turns(segs, spk, r["start_s"], r["end_s"]), 0.0, []
            for a, b in turns:
                if got >= 30:
                    break
                parts.append(np.asarray(inference.crop(r["wav_path"], Segment(a, b))).reshape(-1))
                got += b - a
            if parts:
                v = np.mean(parts, axis=0)
                vecs[spk] = v / np.linalg.norm(v)
        np.savez(cache, **vecs)
        items += [(r["excerpt_id"], r["split"], k, v) for k, v in vecs.items()]
    flagged = []
    if items:
        mat = np.stack([it[3] for it in items])            # unit vectors -> dot product = cosine
        splits = np.array([it[1] for it in items])
        sims = mat @ mat.T
        ii, jj = np.where(np.triu(sims >= args.threshold, k=1) & (splits[:, None] != splits[None, :]))
        for i, j in zip(ii, jj):
            a, b = items[i], items[j]
            flagged.append((round(float(sims[i, j]), 3), a[0], a[1], a[2], b[0], b[1], b[2]))
    flagged.sort(reverse=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["similarity", "excerpt_a", "split_a", "speaker_a", "excerpt_b", "split_b", "speaker_b",
                    "same_person(yes/no)", "resolution", "reviewer"])
        w.writerows(flagged)
    print(f"{len(items)} speaker embeddings, {len(flagged)} cross-split pairs to review -> {args.out}")


if __name__ == "__main__":
    main()
