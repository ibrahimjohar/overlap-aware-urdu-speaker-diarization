"""s06_auto_screen.py - Step 6: automatic audio screening and ranking (GPU recommended).

Two stages keep compute manageable:
    python s06_auto_screen.py --stage probe --limit 100   # 3 x 5-minute probes per video (cheap)
    python s06_auto_screen.py --stage full  --min-score 0.45 --limit 40   # whole file, best probes only

Per video it measures:
  speech / music / noise proportions ........ inaSpeechSegmenter
  Urdu(+Hindi) vs English vs other language .. Whisper language ID on 30 s speech windows
  speakers, overlap, max concurrency, turns .. pyannote speaker diarization
and combines them into a 0-1 ranking score. The full-stage RTTM is kept: it becomes the
machine pre-annotation for the annotation stage.
"""
import argparse
import math
import os

import numpy as np
import soundfile as sf

from common import (HF_TOKEN, PROBE_DIR, RTTM_DIR, TARGET_SR, db, log_event, now, speaker_changes,
                    speech_stats, write_rttm)

DIAR_MODEL = os.environ.get("DIAR_MODEL", "pyannote/speaker-diarization-3.1")
LID_MODEL = os.environ.get("LID_MODEL", "small")
URDU_LIKE = {"ur", "hi"}   # spoken Urdu and Hindi are close; Whisper often labels Urdu speech as "hi"


def load_models():
    import torch
    import whisper
    from inaSpeechSegmenter import Segmenter
    from pyannote.audio import Pipeline
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    try:
        diar = Pipeline.from_pretrained(DIAR_MODEL, token=HF_TOKEN)             # pyannote.audio 4.x
    except TypeError:
        diar = Pipeline.from_pretrained(DIAR_MODEL, use_auth_token=HF_TOKEN)    # pyannote.audio 3.x
    diar.to(dev)
    return Segmenter(vad_engine="smn", detect_gender=False), whisper.load_model(LID_MODEL, device=dev), diar


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


def ina_ratios(segmenter, path, total):
    seg = segmenter(str(path))                       # list of (label, start, end)
    dur = {"speech": 0.0, "music": 0.0, "noise": 0.0}
    speech_regions = []
    for label, s, e in seg:
        if label in dur:
            dur[label] += e - s
        if label == "speech":
            speech_regions.append((s, e))
    return {k: v / total for k, v in dur.items()}, speech_regions


def lid_fractions(model, path, speech_regions, win=30.0, step=120.0):
    import whisper
    audio = whisper.load_audio(str(path))            # float32, 16 kHz
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


def diarize(pipe, path):
    out = pipe(str(path))
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
    args = ap.parse_args()
    con = db()
    if args.stage == "probe":
        rows = con.execute("""SELECT d.video_id, d.wav_path, d.duration_s FROM downloads d
                              WHERE d.status='ok' AND d.video_id NOT IN
                              (SELECT video_id FROM screening WHERE stage='probe') LIMIT ?""",
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
    segmenter, lid, diar = load_models()
    for r in rows:
        vid = r["video_id"]
        path = make_probe(r["wav_path"], vid) if args.stage == "probe" else r["wav_path"]
        total = sf.info(str(path)).duration
        ratios, speech_regions = ina_ratios(segmenter, path, total)
        ur, en, other, n_win = lid_fractions(lid, path, speech_regions)
        segs = diarize(diar, path)
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
            os.remove(path)                                   # probes are temporary
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
