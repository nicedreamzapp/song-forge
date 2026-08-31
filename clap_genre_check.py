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

    # Average three fixed 10s slices (10-20, 20-30, 30-40s). Two reasons
    # (both 2026-07-29): intros are often solo piano/pads, so slice 0-10 was
    # judging the intro not the song; and CLAP truncates anything longer than
    # 10s to ONE RANDOM 10s crop per call (rand_trunc), which made single
    # verdicts flip run-to-run on identical audio. Fixed slices + averaging
    # makes the gate deterministic. Short clips fall back to the file top.
    def _slice_probs(ss, dur):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", ss, "-t", dur,
                        "-i", wav,
                        "-ar", "48000", "-ac", "1", "-f", "f32le", "/tmp/clap_gate.raw"],
                       check=True, timeout=60)
        audio = np.fromfile("/tmp/clap_gate.raw", dtype=np.float32)
        if len(audio) < 48000 * 5:
            return None
        with torch.no_grad():
            inputs = proc(text=labels, audios=audio, return_tensors="pt",
                          sampling_rate=48000, padding=True)
            return model(**inputs).logits_per_audio[0].softmax(dim=-1).tolist()

    slices = [p for p in (_slice_probs(ss, "10") for ss in ("10", "20", "30")) if p]
    if not slices:
        p = _slice_probs("0", "30")
        if p:
            slices = [p]
    if not slices:
        print(json.dumps({"verdict": "skip", "reason": "audio too short"}))
        return
    probs = [sum(col) / len(col) for col in zip(*slices)]

    ranked = sorted(zip(labels, probs), key=lambda x: -x[1])
    req_prob = dict(zip(labels, probs))[requested]
    top_label, top_prob = ranked[0]
    verdict = "pass"
    # Two ways to fail: the original confident-enemy rule, or (2026-07-29)
    # the enemy simply beating the requested genre by a clear margin — the
    # old rule passed a drill render that read 55% country / 5% hip-hop
    # when the requested prob crept over 0.15.
    if top_label in enemies and ((top_prob >= 0.30 and req_prob <= 0.15)
                                 or (top_prob - req_prob >= 0.15)):
        verdict = "fail"
    print(json.dumps({"verdict": verdict, "top": [[l, round(p, 3)] for l, p in ranked[:3]],
                      "requested_prob": round(req_prob, 3)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"verdict": "skip", "reason": str(e)}))
