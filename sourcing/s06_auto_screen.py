"""s06_auto_screen.py - Step 6: automatic audio screening and ranking (GPU recommended).

Two stages keep compute manageable:
    python s06_auto_screen.py --stage probe --limit 100   # 3 x 5-minute probes per video (cheap)
    python s06_auto_screen.py --stage full  --min-score 0.45 --limit 40   # whole file, best probes only

Per video it measures:
  speech / music / noise proportions ........ inaSpeechSegmenter (runs in the separate .venv-ina,
                                              called through ina_segment.py)
  Urdu(+Hindi) vs English vs other language .. Whisper language ID on 30 s speech windows
  speakers, overlap, max concurrency, turns .. pyannote speaker diarization
and combines them into a 0-1 ranking score. The full-stage RTTM is kept: it becomes the
machine pre-annotation for the annotation stage.

Videos are processed in batches (--batch, default 10): probes for one batch are cut, passed to
inaSpeechSegmenter in one call, scored with Whisper + pyannote on the GPU, then deleted, so
temporary files never take more than about one batch of disk space.

inaSpeechSegmenter needs TensorFlow, which lives in its own environment. Its Python is found at
.venv-ina/Scripts/python.exe (Windows) or .venv-ina/bin/python, or wherever INA_PYTHON in .env points.
"""
import argparse
import json
import math
import os
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from common import (HF_TOKEN, PROBE_DIR, RTTM_DIR, TARGET_SR, db, log_event, now, speaker_changes,
                    speech_stats, write_rttm)

DIAR_MODEL = os.environ.get("DIAR_MODEL", "pyannote/speaker-diarization-3.1")
LID_MODEL = os.environ.get("LID_MODEL", "small")
URDU_LIKE = {"ur", "hi"}   # spoken Urdu and Hindi are close; Whisper often labels Urdu speech as "hi"

HERE = Path(__file__).resolve().parent                 # sourcing/
REPO_ROOT = HERE.parent
INA_HELPER = HERE / "ina_segment.py"


def ina_python():
    """Path to the Python of the .venv-ina environment (INA_PYTHON in .env overrides the default)."""
    p = os.environ.get("INA_PYTHON")
    if p:
        p = Path(p)
        if not p.is_absolute():
            p = REPO_ROOT / p
    else:
        win = REPO_ROOT / ".venv-ina" / "Scripts" / "python.exe"
        p = win if win.exists() else REPO_ROOT / ".venv-ina" / "bin" / "python"
    if not p.exists():
        raise SystemExit(f"inaSpeechSegmenter environment not found at {p}. Create .venv-ina "
                         "(setup Step 12) or set INA_PYTHON in .env.")
    return p


def load_models():
    import torch
    import whisper
    from pyannote.audio import Pipeline
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    try:
        diar = Pipeline.from_pretrained(DIAR_MODEL, token=HF_TOKEN)             # pyannote.audio 4.x
    except TypeError:
        diar = Pipeline.from_pretrained(DIAR_MODEL, use_auth_token=HF_TOKEN)    # pyannote.audio 3.x
    diar.to(dev)
    return whisper.load_model(LID_MODEL, device=dev), diar


def make_probe(wav, vid, minutes=5):
    """Concatenate three 5-minute probes (10 %, 50 %, 80 % into the file) into one temporary WAV."""
    info = sf.info(str(wav))
    n = int(minutes * 60 * info.samplerate)
    parts = []
    for frac in (0.10, 0.50, 0.80):
        start = max(0, min(int(info.frames * frac), info.frames - n))
        data, _ = sf.read(str(wav), start=start, frames=n, dtype="int16")
        parts.append(data)
    out = PROBE_DIR / f"{vid}.probe.wav"
    sf.write(str(out), np.concatenate(parts), info.samplerate, subtype="PCM_16")
    return out


def run_ina(paths, tag):
    """Run inaSpeechSegmenter on a batch of files in the .venv-ina environment.

    Returns {path: list of (label, start, end)} for files that worked, and {path: error} for the rest.
    """
    lst, out = PROBE_DIR / f"ina_{tag}.list.txt", PROBE_DIR / f"ina_{tag}.json"
    lst.write_text("".join(f"{p}\n" for p in paths), encoding="utf-8")
    if out.exists():
        out.unlink()
    res = subprocess.run([str(ina_python()), str(INA_HELPER), "--list", str(lst), "--out", str(out)],
                         stdout=subprocess.DEVNULL)    # progress goes to the console via stderr
    if res.returncode != 0 or not out.exists():
        raise SystemExit(f"inaSpeechSegmenter failed (exit code {res.returncode}); see the messages above.")
    data = json.loads(out.read_text(encoding="utf-8"))
    lst.unlink()
    out.unlink()
    ok = {p: [tuple(x) for x in v["segments"]] for p, v in data.items() if "segments" in v}
    bad = {p: v["error"] for p, v in data.items() if "error" in v}
    return ok, bad


def ina_ratios(segments, total):
    """Speech / music / noise shares of the clip, plus the speech regions used to pick LID windows."""
    dur = {"speech": 0.0, "music": 0.0, "noise": 0.0}
    speech_regions = []
    for label, s, e in segments:
        if label in dur:
            dur[label] += e - s
        if label == "speech":
            speech_regions.append((s, e))
    return {k: v / total for k, v in dur.items()}, speech_regions


def load_audio(path):
    """16 kHz mono float32 samples, read once and shared by Whisper and pyannote."""
    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if sr != TARGET_SR:
        raise ValueError(f"{path}: expected {TARGET_SR} Hz, got {sr} Hz")
    return audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]


def lid_fractions(model, audio, speech_regions, win=30.0, step=120.0):
    import whisper
    counts, t, total = {}, 0.0, len(audio) / TARGET_SR
    while t + win <= total:
        covered = sum(max(0.0, min(e, t + win) - max(s, t)) for s, e in speech_regions)
        if covered >= 0.7 * win:                     # only judge windows that are mostly speech
            seg = whisper.pad_or_trim(audio[int(t * TARGET_SR):int((t + win) * TARGET_SR)])
            mel = whisper.log_mel_spectrogram(seg, n_mels=model.dims.n_mels).to(model.device)
            _, probs = model.detect_language(mel)
            top = max(probs, key=probs.get)
            counts[top] = counts.get(top, 0) + 1
        t += step
    n = sum(counts.values())
    if n == 0:
        return 0.0, 0.0, 0.0, 0
    ur = sum(counts.get(k, 0) for k in URDU_LIKE) / n
    en = counts.get("en", 0) / n
    return ur, en, 1.0 - ur - en, n


def diarize(pipe, audio):
    """Diarize in-memory audio. Passing samples (not a file path) avoids pyannote 4's file decoder,
    which needs FFmpeg shared libraries that are often missing on Windows."""
    import torch
    out = pipe({"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": TARGET_SR})
    ann = getattr(out, "speaker_diarization", out)   # 4.x wraps the annotation in an output object
    return [(turn.start, turn.end, spk) for turn, _, spk in ann.itertracks(yield_label=True)]


def score(m):
    """Ranking score in [0, 1]. No upper limit on speakers: more speakers never lowers the score."""
    spk = min(math.log2(max(m["n_speakers"], 1)) / math.log2(8), 1.0)   # 1 spk->0, 2->0.33, 8+->1
    ovl = min(m["overlap_ratio"] / 0.15, 1.0)                           # 15 % overlap saturates
    s = (0.30 * ovl + 0.25 * spk + 0.25 * m["urdu_hindi_frac"] + 0.20 * m["speech_ratio"]
         - 0.40 * m["music_ratio"])
    if m["n_speakers"] < 2:
        s = 0.0                                                         # single speaker: not usable
    return round(max(0.0, min(1.0, s)), 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["probe", "full"], required=True)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--min-score", type=float, default=0.45, help="full stage: probe score needed")
    ap.add_argument("--batch", type=int, default=10, help="files per inaSpeechSegmenter call")
    args = ap.parse_args()
    con = db()
    if args.stage == "probe":
        rows = con.execute("""SELECT d.video_id, d.wav_path, d.duration_s FROM downloads d
                              WHERE d.status='ok' AND d.video_id NOT IN
                              (SELECT video_id FROM screening WHERE stage='probe')
                              AND d.video_id NOT IN (SELECT video_id FROM triage WHERE decision='reject')
                              LIMIT ?""",
                           (args.limit,)).fetchall()
    else:
        rows = con.execute("""SELECT d.video_id, d.wav_path, d.duration_s FROM downloads d
                              JOIN screening s ON s.video_id=d.video_id AND s.stage='probe'
                              WHERE s.score >= ? AND d.video_id NOT IN
                              (SELECT video_id FROM screening WHERE stage='full')
                              ORDER BY s.score DESC LIMIT ?""", (args.min_score, args.limit)).fetchall()
    if not rows:
        print("nothing to screen")
        return
    ina_python()                                      # fail early if .venv-ina is missing
    lid, diar = load_models()
    for b in range(0, len(rows), args.batch):
        batch = rows[b:b + args.batch]
        paths = {}
        for r in batch:
            paths[r["video_id"]] = (str(make_probe(r["wav_path"], r["video_id"])) if args.stage == "probe"
                                    else str(r["wav_path"]))
        print(f"batch {b // args.batch + 1}: inaSpeechSegmenter on {len(paths)} file(s)...")
        ina_ok, ina_bad = run_ina(list(paths.values()), f"{args.stage}_{b}")
        for r in batch:
            vid, path = r["video_id"], paths[r["video_id"]]
            if path not in ina_ok:
                print(f"{vid}: inaSpeechSegmenter error, skipped (will retry next run): {ina_bad.get(path)}")
                log_event(con, "video", vid, f"ina_error_{args.stage}", ina_bad.get(path, "missing"))
                con.commit()
                if args.stage == "probe":
                    os.remove(path)
                continue
            audio = load_audio(path)
            total = len(audio) / TARGET_SR
            ratios, speech_regions = ina_ratios(ina_ok[path], total)
            ur, en, other, n_win = lid_fractions(lid, audio, speech_regions)
            segs = diarize(diar, audio)
            del audio
            speech, overlap, max_c = speech_stats(segs)
            m = dict(speech_ratio=ratios["speech"], music_ratio=ratios["music"], noise_ratio=ratios["noise"],
                     urdu_hindi_frac=ur, english_frac=en, other_lang_frac=other, lid_windows=n_win,
                     n_speakers=len({s[2] for s in segs}),
                     overlap_ratio=(overlap / speech) if speech else 0.0, max_concurrent=max_c,
                     changes_per_min=speaker_changes(segs) / (speech / 60) if speech else 0.0)
            rttm = None
            if args.stage == "full":
                rttm = RTTM_DIR / f"{vid}.rttm"
                write_rttm(rttm, vid, segs)
            else:
                os.remove(path)                               # probes are temporary
            m["score"] = score(m)
            con.execute("""INSERT OR REPLACE INTO screening VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (vid, args.stage, now(), m["speech_ratio"], m["music_ratio"], m["noise_ratio"],
                         m["urdu_hindi_frac"], m["english_frac"], m["other_lang_frac"], m["lid_windows"],
                         m["n_speakers"], m["overlap_ratio"], m["max_concurrent"], m["changes_per_min"],
                         str(rttm) if rttm else None, DIAR_MODEL, m["score"]))
            log_event(con, "video", vid, f"screened_{args.stage}", m)
            con.commit()
            print(f"{vid} [{args.stage}] spk={m['n_speakers']} ovl={m['overlap_ratio']:.1%} "
                  f"urdu={ur:.0%} music={ratios['music']:.0%} score={m['score']}")

if __name__ == "__main__":
    main()
