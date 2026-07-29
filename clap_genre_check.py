#!/usr/bin/env python
"""CLAP genre check for Song Forge (2026-07-09).

Runs inside the ACE venv (torch + transformers live there; the forge's own
python has neither). Called by forge_server as a subprocess:

    .venv/bin/python clap_genre_check.py <wav> <requested_label> <enemy1> [enemy2...]

Prints ONE json line: {"verdict": "pass"|"fail"|"skip", "top": [[label, prob]...],
"requested_prob": float}. Conservative on purpose — validated 2026-07-09 that
clap-htsat-unfused reads VOCAL tracks well but misreads instrumentals, so the
forge only calls this for songs that sing, and "fail" needs a confident enemy
verdict (enemy top-1 >= 0.30) with the requested genre nearly absent (<= 0.15).
laion/larger_clap_music is BROKEN in transformers (audio tower collapses to a
constant embedding) — do not "upgrade" to it.
"""
import os, sys, json, subprocess

os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"

import warnings
warnings.filterwarnings("ignore")
import numpy as np


def main():
    wav, requested = sys.argv[1], sys.argv[2]
    enemies = sys.argv[3:]
    fillers = ["pop music", "folk music", "jazz music", "electronic dance music"]
    labels = [requested] + enemies + [f for f in fillers if f != requested and f not in enemies]

    import torch
    from transformers import ClapModel, ClapProcessor
    model = ClapModel.from_pretrained("laion/clap-htsat-unfused")
    model.eval()
    proc = ClapProcessor.from_pretrained("laion/clap-htsat-unfused")

    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-t", "30", "-i", wav,
                    "-ar", "48000", "-ac", "1", "-f", "f32le", "/tmp/clap_gate.raw"],
                   check=True, timeout=60)
    audio = np.fromfile("/tmp/clap_gate.raw", dtype=np.float32)
    if len(audio) < 48000 * 5:
        print(json.dumps({"verdict": "skip", "reason": "audio too short"}))
        return

    with torch.no_grad():
        inputs = proc(text=labels, audios=audio, return_tensors="pt",
                      sampling_rate=48000, padding=True)
        probs = model(**inputs).logits_per_audio[0].softmax(dim=-1).tolist()

    ranked = sorted(zip(labels, probs), key=lambda x: -x[1])
    req_prob = dict(zip(labels, probs))[requested]
    top_label, top_prob = ranked[0]
    verdict = "pass"
    if top_label in enemies and top_prob >= 0.30 and req_prob <= 0.15:
        verdict = "fail"
    print(json.dumps({"verdict": verdict, "top": [[l, round(p, 3)] for l, p in ranked[:3]],
                      "requested_prob": round(req_prob, 3)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"verdict": "skip", "reason": str(e)}))
