"""s11_package.py - Step 10: build the hand-off package for the annotation/transcription stage.

    python s11_package.py --version v0.1

Creates handoff/<version>/ containing sample-exact excerpt WAVs, machine pre-annotation RTTMs,
UEM scoring regions, split lists, a pyannote.database config, manifests, a source/licence table,
ATTRIBUTION.md, public_manifest.csv (what may be redistributed) and SHA-256 checksums.
The package is internal: only the files listed in public_manifest.csv may ever be published.
"""
import argparse
import csv
import hashlib
import json

import soundfile as sf

from common import HANDOFF_DIR, TARGET_SR, db, log_event, now, read_rttm, write_rttm

CC_BY_3 = "https://creativecommons.org/licenses/by/3.0/"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def release_licence(tier, api_license, known_licence):
    """Licence under which the excerpt AUDIO may be shared."""
    if tier == "A":
        if api_license == "creativeCommon":
            return "CC BY 3.0"
        if (known_licence or "") == "pd":
            return "public domain (US government work)"
        return "CHECK: Tier A without CC or public-domain evidence"
    if tier == "B":
        return "written permission"
    return "reference only"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    args = ap.parse_args()
    con = db()
    out = HANDOFF_DIR / args.version
    for sub in ("audio", "rttm_machine", "uem", "lists"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    rows = con.execute("""
        SELECT e.*, d.wav_path, s.rttm_path, s.diar_model, v.title, v.published_at, v.license,
               c.title AS channel_title,
               (SELECT known_licence FROM channel_profile cp WHERE cp.channel_id = e.channel_id
                  ORDER BY profiled_at DESC LIMIT 1) AS known_licence,
               (SELECT wayback_url FROM licence_evidence le WHERE le.video_id=e.video_id
                  AND le.wayback_url IS NOT NULL ORDER BY checked_at DESC LIMIT 1) AS wayback
        FROM excerpts e JOIN downloads d USING(video_id) JOIN videos v USING(video_id)
        JOIN channels c ON c.channel_id = e.channel_id
        JOIN screening s ON s.video_id = e.video_id AND s.stage = 'full'
        WHERE e.split IS NOT NULL ORDER BY e.split, e.excerpt_id""").fetchall()
    manifest, per_split = [], {"train": [], "dev": [], "test": []}
    split_rttm = {k: [] for k in per_split}
    for r in rows:
        ex = r["excerpt_id"]
        info = sf.info(r["wav_path"])
        assert info.samplerate == TARGET_SR and info.channels == 1, f"unexpected format for {ex}"
        a, b = int(round(r["start_s"] * TARGET_SR)), int(round(r["end_s"] * TARGET_SR))
        audio, _ = sf.read(r["wav_path"], start=a, stop=b, dtype="int16")
        wav_out = out / "audio" / f"{ex}.wav"
        sf.write(str(wav_out), audio, TARGET_SR, subtype="PCM_16")
        dur = len(audio) / TARGET_SR
        # machine hypotheses clipped to the excerpt, shifted to start at 0, speakers renamed per excerpt
        segs = [(max(s, r["start_s"]) - r["start_s"], min(e, r["end_s"]) - r["start_s"], k)
                for s, e, k in read_rttm(r["rttm_path"]) if e > r["start_s"] and s < r["end_s"]]
        names = {k: f"spk{i:02d}" for i, k in enumerate(sorted({k for *_, k in segs}))}
        segs = [(s, e, names[k]) for s, e, k in segs if e - s > 0.01]
        write_rttm(out / "rttm_machine" / f"{ex}.rttm", ex, segs)
        split_rttm[r["split"]] += [(ex, segs)]
        (out / "uem" / f"{ex}.uem").write_text(f"{ex} 1 0.000 {dur:.3f}\n")
        per_split[r["split"]].append((ex, dur))
        manifest.append({
            "excerpt_id": ex, "split": r["split"], "tier": r["tier"],
            "audio": f"audio/{ex}.wav", "sample_rate": TARGET_SR, "channels": 1, "sample_format": "s16le",
            "num_samples": len(audio), "duration_s": round(dur, 3), "sha256": sha256(wav_out),
            "source": {"platform": "youtube", "video_id": r["video_id"],
                       "url": f"https://www.youtube.com/watch?v={r['video_id']}",
                       "channel_id": r["channel_id"], "channel_title": r["channel_title"],
                       "title": r["title"], "published_at": r["published_at"],
                       "offset_start_s": r["start_s"], "offset_end_s": r["end_s"],
                       "api_license": r["license"], "licence_evidence": r["wayback"],
                       "release_licence": release_licence(r["tier"], r["license"], r["known_licence"])},
            "machine_preannotation": {"rttm": f"rttm_machine/{ex}.rttm", "model": r["diar_model"],
                                      "n_speakers_hyp": len(names), "overlap_ratio_hyp": r["overlap_ratio_hyp"]},
            "status": {"diarization_gold": False, "transcription": False},
        })
    for split, items in per_split.items():
        (out / "lists" / f"{split}.lst").write_text("".join(f"{ex}\n" for ex, _ in items))
        (out / "uem" / f"{split}.uem").write_text("".join(f"{ex} 1 0.000 {d:.3f}\n" for ex, d in items))
        with open(out / "rttm_machine" / f"{split}.rttm", "w", encoding="utf-8") as f:
            for ex, segs in split_rttm[split]:
                for s, e, k in segs:
                    f.write(f"SPEAKER {ex} 1 {s:.3f} {e - s:.3f} <NA> <NA> {k} <NA> <NA>\n")
    (out / "database.yml").write_text(
        "Databases:\n  UrduYT: audio/{uri}.wav\n\nProtocols:\n  UrduYT:\n    SpeakerDiarization:\n"
        "      MachinePreannotation:\n" + "".join(
            f"        {name}:\n          uri: lists/{s}.lst\n          annotation: rttm_machine/{s}.rttm\n"
            f"          annotated: uem/{s}.uem\n"
            for name, s in (("train", "train"), ("development", "dev"), ("test", "test"))))
    with open(out / "manifest.jsonl", "w", encoding="utf-8") as f:
        for m in manifest:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    with open(out / "sources.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["excerpt_id", "split", "tier", "video_id", "url", "channel_title", "title",
                    "start_s", "end_s", "api_license", "licence_evidence"])
        for m in manifest:
            s = m["source"]
            w.writerow([m["excerpt_id"], m["split"], m["tier"], s["video_id"], s["url"], s["channel_title"],
                        s["title"], s["offset_start_s"], s["offset_end_s"], s["api_license"],
                        s["licence_evidence"]])
    with open(out / "public_manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["excerpt_id", "video_id", "start_s", "end_s", "audio_redistributable", "licence"])
        for m in manifest:
            s = m["source"]
            redis = m["tier"] in ("A", "B")
            lic = s["release_licence"]
            w.writerow([m["excerpt_id"], s["video_id"], s["offset_start_s"], s["offset_end_s"], redis, lic])
    with open(out / "ATTRIBUTION.md", "w", encoding="utf-8") as f:
        f.write("# Attribution\n\nAudio excerpts in this corpus that are marked redistributable were derived "
                "(cut and resampled to 16 kHz mono) from the following works.\n\n")
        for m in manifest:
            if m["tier"] == "A":
                s = m["source"]
                terms = (f"licensed under CC BY 3.0 ({CC_BY_3})" if s["release_licence"] == "CC BY 3.0"
                         else s["release_licence"])
                f.write(f"- \"{s['title']}\" by {s['channel_title']} ({s['url']}), {terms}. "
                        f"Excerpt {s['offset_start_s']:.1f}-{s['offset_end_s']:.1f} s; "
                        f"changes: excerpted, down-mixed to mono, resampled to 16 kHz.\n")
            elif m["tier"] == "B":
                s = m["source"]
                f.write(f"- \"{s['title']}\" by {s['channel_title']} ({s['url']}), used with written "
                        f"permission of the rights holder.\n")
    with open(out / "checksums.sha256", "w") as f:
        for p in sorted(out.rglob("*")):
            if p.is_file() and p.name != "checksums.sha256":
                f.write(f"{sha256(p)}  {p.relative_to(out)}\n")
    log_event(con, "package", args.version, "built", {"excerpts": len(manifest), "at": now()})
    con.commit()
    hours = {k: sum(d for _, d in v) / 3600 for k, v in per_split.items()}
    print(f"package {out}: {len(manifest)} excerpts; hours per split: "
          + ", ".join(f"{k}={v:.2f}" for k, v in hours.items()))


if __name__ == "__main__":
    main()
