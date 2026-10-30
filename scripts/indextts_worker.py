"""One-shot worker executed only by the existing IndexTTS Python 3.10 runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import wave


def wav_info(path):
    with wave.open(str(path), "rb") as audio:
        rate, frames = audio.getframerate(), audio.getnframes()
        if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                or audio.getcomptype() != "NONE" or rate != 24000
                or not 0 < frames <= 60 * rate
                or len(audio.readframes(frames)) != frames * 2):
            raise ValueError("invalid_wechat_wav")
    return {"sample_rate": rate, "channels": 1, "sample_width": 2,
            "frames": frames, "duration_seconds": frames / rate,
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def run(request, directory):
    started = time.perf_counter()
    root = Path(request["model_root"]).resolve(strict=True)
    reference = Path(request["reference_audio"]).resolve(strict=True)
    text = request["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 200 or "\0" in text:
        raise ValueError("invalid_synthesis_text")
    if not reference.is_file():
        raise ValueError("reference_not_file")
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(HF_HOME=str(root / "checkpoints"),
                      HF_HUB_CACHE=str(root / "checkpoints/hub"),
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1")
    # Cache constants are initialized before the legacy module sets hf_cache.
    import huggingface_hub.constants  # noqa: F401 -- initialize cache before IndexTTS imports
    import torch
    import indextts.infer_v2 as inference

    if not torch.cuda.is_available():
        raise RuntimeError("cuda_unavailable")

    class NoTextEmotion:
        """This experiment uses reference emotion, never text emotion inference."""
        def __init__(self, *_args, **_kwargs):
            pass

        def inference(self, *_args, **_kwargs):
            raise RuntimeError("text_emotion_not_enabled")

    # Avoid an unused 0.6B classifier on a 6 GB GPU; model source stays intact.
    inference.QwenEmotion = NoTextEmotion
    print("worker: loading_model", flush=True)
    load_started = time.perf_counter()
    model = inference.IndexTTS2(cfg_path=str(root / "checkpoints/config.yaml"),
                               model_dir=str(root / "checkpoints"),
                               device="cuda:0", is_fp16=True, use_cuda_kernel=False)
    load_seconds = time.perf_counter() - load_started
    torch.cuda.reset_peak_memory_stats()
    print("worker: synthesizing", flush=True)
    synth_started = time.perf_counter()
    sample_rate, samples = model.infer(
        spk_audio_prompt=str(reference), text=text, output_path=None,
        use_emo_text=False, use_random=False, verbose=False, num_beams=1,
    )
    synth_seconds = time.perf_counter() - synth_started
    raw = directory / "generated.wav"
    with wave.open(str(raw), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(samples.astype("<i2").tobytes())
    target = directory / "wechat.wav"
    subprocess.run([str(root / "ffmpeg.exe"), "-nostdin", "-v", "error", "-n",
                    "-i", str(raw), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
                    str(target)], check=True, capture_output=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"state": "synthesized", "audio": wav_info(target),
            "reference_name": reference.name,
            "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "load_seconds": round(load_seconds, 3),
            "synthesis_seconds": round(synth_seconds, 3),
            "total_seconds": round(time.perf_counter() - started, 3),
            "peak_cuda_allocated_mib": round(torch.cuda.max_memory_allocated() / 2**20, 1),
            "fp16": True, "text_emotion_model": False, "num_beams": 1}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request_path = args.request.resolve(strict=True)
    directory = request_path.parent
    try:
        result = run(json.loads(request_path.read_text("utf-8")), directory)
    except Exception as error:
        traceback.print_exc()
        result = {"state": "failed", "error_type": type(error).__name__}
    (directory / "synthesis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["state"] == "synthesized" else 1


if __name__ == "__main__":
    raise SystemExit(main())
