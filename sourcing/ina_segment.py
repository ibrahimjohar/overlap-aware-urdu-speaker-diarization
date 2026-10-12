"""ina_segment.py - speech / music / noise segmentation with inaSpeechSegmenter.

This helper runs ONLY inside the separate .venv-ina environment (TensorFlow lives there).
s06_auto_screen.py calls it automatically; you do not normally run it yourself.

    <.venv-ina python> ina_segment.py --list files.txt --out results.json

--list   UTF-8 text file with one audio path per line
--out    JSON file written on success:
         {"<path>": {"segments": [["speech", 0.0, 2.4], ...]}  or  {"error": "..."}}

Results go to a file, not to the screen, because TensorFlow/Keras print progress
messages that would otherwise mix with the data. The model is loaded once per batch.
"""
import argparse
import json
import os
import sys

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")   # quieter TensorFlow start-up


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.list, encoding="utf-8") as f:
        paths = [p.strip() for p in f if p.strip()]

    from inaSpeechSegmenter import Segmenter
    seg = Segmenter(vad_engine="smn", detect_gender=False)

    results = {}
    for i, p in enumerate(paths, 1):
        try:
            results[p] = {"segments": [[lab, float(s), float(e)] for lab, s, e in seg(p)]}
        except Exception as exc:                          # one bad file must not stop the batch
            results[p] = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"ina {i}/{len(paths)} done", file=sys.stderr, flush=True)

    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)
    os.replace(tmp, args.out)                             # the file only appears once it is complete


if __name__ == "__main__":
    main()