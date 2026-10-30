"""Private stdio model process. No WeChat API, network service, or chat logging."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import wave


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def owned_path(root, job, name):
    if not isinstance(job, str) or re.fullmatch(r"[a-f0-9]{32}", job) is None:
        raise ValueError("invalid_job_id")
    path = root / job / name
    if path.resolve() != path or path.parent.parent != root:
        raise ValueError("job_path_alias")
    return path


def watch_owner(pid):
    if os.name != "nt":
        return
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        raise RuntimeError("owner_unavailable")

    def monitor():
        kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        kernel.CloseHandle(handle)
        os._exit(0)  # Only this inference process; never a native WeChat thread.

    threading.Thread(target=monitor, daemon=True, name="speech-owner").start()


def offline():
    import socket
    def refused(*_args, **_kwargs):
        raise RuntimeError("speech_worker_network_disabled")
    socket.socket.connect = refused
    socket.socket.connect_ex = refused
    socket.create_connection = refused
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", MODELSCOPE_OFFLINE="1")


class Recognition:
    def __init__(self, settings):
        from funasr import AutoModel
        root = Path(settings["asr_model_root"]).resolve(strict=True)
        names = ("speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
                 "speech_fsmn_vad_zh-cn-16k-common-pytorch",
                 "punc_ct-transformer_zh-cn-common-vocab272727-pytorch")
        paths = [root / name for name in names]
        if any(not (p / "model.pt").is_file() or (p / "requirements.txt").exists() for p in paths):
            raise ValueError("local_asr_models_required")
        options = dict(ngpu=0, ncpu=4, check_latest=False, disable_pbar=True,
                       disable_log=True, log_level="ERROR")
        self.model = AutoModel(model=str(paths[0]), vad_model=str(paths[1]),
                               punc_model=str(paths[2]), device="cpu",
                               vad_kwargs=dict(options), punc_kwargs=dict(options), **options)

    def run(self, request, jobs):
        import numpy as np
        path = owned_path(jobs, request["id"], "input.wav")
        with wave.open(str(path), "rb") as stream:
            if (stream.getnchannels() != 1 or stream.getsampwidth() != 2
                    or stream.getframerate() != 16000 or stream.getcomptype() != "NONE"
                    or not 0 < stream.getnframes() <= 60 * 16000):
                raise ValueError("invalid_recognition_audio")
            pcm = stream.readframes(stream.getnframes())
            if len(pcm) != stream.getnframes() * 2:
                raise ValueError("truncated_recognition_audio")
        audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        result = self.model.generate(input=audio, fs=16000)
        text = "".join(str(row.get("text", "")) for row in result if isinstance(row, dict)).strip()
        if "\0" in text or len(text.encode("utf-8")) > 1023:
            raise ValueError("transcript_too_large")
        return {"text": text, "audio_seconds": len(audio) / 16000}


class Synthesis:
    def __init__(self, settings):
        root = Path(settings["tts_model_root"]).resolve(strict=True)
        reference = Path(settings["reference_audio"]).resolve(strict=True)
        self.root, self.reference = root, reference
        os.chdir(root)
        sys.path.insert(0, str(root))
        os.environ.update(HF_HOME=str(root / "checkpoints"),
                          HF_HUB_CACHE=str(root / "checkpoints/hub"))
        import huggingface_hub.constants  # noqa: F401 -- initialize cache before IndexTTS imports
        import torch
        import indextts.infer_v2 as inference
        if not torch.cuda.is_available():
            raise RuntimeError("cuda_unavailable")
        class NoTextEmotion:
            def __init__(self, *_args, **_kwargs):
                pass
            def inference(self, *_args, **_kwargs):
                raise RuntimeError("text_emotion_not_enabled")
        inference.QwenEmotion = NoTextEmotion
        self.torch = torch
        self.model = inference.IndexTTS2(cfg_path=str(root / "checkpoints/config.yaml"),
            model_dir=str(root / "checkpoints"), device="cuda:0", is_fp16=True, use_cuda_kernel=False)
        self.reference_digest = digest(reference)

    def run(self, request, jobs):
        import subprocess
        text = request.get("text")
        if (not isinstance(text, str) or not text.strip() or "\0" in text
                or len(text.encode("utf-8")) > 1023):
            raise ValueError("invalid_synthesis_text")
        revision = digest(self.reference)
        if revision != request.get("reference_sha256"):
            raise ValueError("reference_changed")
        if revision != self.reference_digest:
            for field in ("cache_spk_cond", "cache_s2mel_style", "cache_s2mel_prompt",
                          "cache_spk_audio_prompt", "cache_emo_cond", "cache_emo_audio_prompt", "cache_mel"):
                setattr(self.model, field, None)
            self.reference_digest = revision
        with self.torch.inference_mode():
            rate, samples = self.model.infer(spk_audio_prompt=str(self.reference), text=text,
                output_path=None, use_emo_text=False, use_random=False, verbose=False, num_beams=1)
        if digest(self.reference) != revision:
            raise ValueError("reference_changed_during_synthesis")
        raw = owned_path(jobs, request["id"], "generated.wav")
        target = owned_path(jobs, request["id"], "speech.wav")
        with wave.open(str(raw), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(samples.astype("<i2").tobytes())
        subprocess.run([str(self.root / "ffmpeg.exe"), "-nostdin", "-v", "error", "-n",
            "-i", str(raw), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(target)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with wave.open(str(target), "rb") as stream:
            duration = stream.getnframes() / stream.getframerate()
        if not 0 < duration <= 60:
            raise ValueError("speech_duration_out_of_range")
        return {"audio_seconds": duration, "sha256": digest(target), "reference_sha256": revision}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("asr", "tts"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--jobs", required=True, type=Path)
    parser.add_argument("--owner", required=True, type=int)
    args = parser.parse_args()
    jobs = args.jobs.resolve(strict=True)
    wire = sys.stdout
    def emit(value):
        wire.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
        wire.flush()
    watch_owner(args.owner)
    offline()
    try:
        settings = json.loads(args.config.read_text("utf-8-sig"))
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            model = Recognition(settings) if args.kind == "asr" else Synthesis(settings)
        emit({"type": "ready", "kind": args.kind})
    except Exception as error:
        emit({"type": "failed", "kind": args.kind, "reason": type(error).__name__})
        return 1
    while True:
        line = sys.stdin.readline(16385)
        if not line:
            return 0
        if len(line.encode("utf-8")) > 16384 or not line.endswith("\n"):
            return 2
        request = json.loads(line)
        if request == {"op": "close"}:
            return 0
        job = request.get("id")
        owned_path(jobs, job, "input.wav")
        began = time.perf_counter()
        try:
            with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = model.run(request, jobs)
            emit({"type": "result", "id": job, "ok": True, "result": result,
                  "seconds": round(time.perf_counter() - began, 3)})
        except Exception as error:
            emit({"type": "result", "id": job, "ok": False, "reason": type(error).__name__})


if __name__ == "__main__":
    raise SystemExit(main())
