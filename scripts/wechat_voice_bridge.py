"""Owner-loop speech coordinator. Preparation never creates a send intent."""
from __future__ import annotations

import ctypes
from dataclasses import replace

from scripts.wechat_native_bridge_core import (BridgeError, native_event, timestamp,
    uuid_text, VOICE_MAX_AGE)
from scripts.wechat_voice_runtime import SpeechError, sha256

NOTICE = "语音暂时不可用，先用文字回复：\n"


def voice_snapshot(raw, settings, session, now, validator=None, diagnostics=None):
    details = diagnostics if diagnostics is not None else {}
    details.clear()
    details['stage'] = 'identity'
    event = native_event(raw, settings, session, now, voice=True)
    if event is None:
        # Only inspect metadata for the bound incoming conversation. Never log IDs/XML/audio.
        if (getattr(raw, 'type', None) == 34
                and getattr(raw, 'receiver', None) == settings.expected_self.encode('ascii')
                and getattr(raw, 'sender', None) in tuple(value.encode('ascii') for value in
                    (settings.allowed_room, settings.private_peer) if value)):
            xml = getattr(raw, 'image_xml', b'')
            if isinstance(xml, bytes):
                details.update(xml_bytes=len(xml), group_author_prefix=b':\n' in xml,
                    message_id_present=bool(getattr(raw, 'native_message_id', 0)),
                    sequence_present=bool(getattr(raw, 'sequence', 0)))
        return None
    details['stage'] = 'abi'
    # Check exact ABI before reading an embedded binary slice past the XML NUL.
    cls = type(raw)
    if (not isinstance(raw, ctypes.Structure) or ctypes.sizeof(raw) != 267568
            or getattr(cls, "image_xml", None) is None or cls.image_xml.offset != 5408
            or cls.voice_offset.offset != 267560 or cls.voice_size.offset != 267564):
        return None
    offset, size = raw.voice_offset, raw.voice_size
    xml = raw.image_xml
    details.update(stage='payload_bounds', xml_bytes=len(xml), payload_bytes=size,
                   payload_follows_xml=offset == len(xml) + 1)
    if offset != len(xml) + 1 or not 12 < size <= 262144 - offset:
        return None
    payload = ctypes.string_at(ctypes.addressof(raw) + cls.image_xml.offset + offset, size)
    details['stage'] = 'payload_validation'
    if validator is None:
        # Imported only after the pinned NativeTransport installed its source root.
        from wxbot_framework.voice_receiver import _voice_data
        validator = _voice_data
    try:
        payload, duration = validator(payload, xml.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeError) as error:
        # These pinned validator messages are mapped to static codes, never copied verbatim.
        codes = {'完整 SILK 音频不可用': 'silk_unavailable', '语音元数据无效': 'invalid_xml',
                 '语音元数据缺少唯一的音频信息': 'missing_voice_metadata',
                 '语音尚未接收完整': 'incomplete_voice', '语音时长无效': 'invalid_duration',
                 '语音声明长度与音频不一致': 'length_mismatch',
                 '语音声明时长与音频不一致': 'duration_mismatch'}
        details['reason'] = codes.get(str(error), 'invalid_audio')
        return None
    details.update(stage='validated', duration_ms=duration)
    event["text"] = ""
    return event, payload, duration


class VoiceBridge:
    def __init__(self, core, runtime):
        self.core, self.runtime = core, runtime
        self.inputs, self.preparations, self.seen = {}, {}, {}
        self.last_input = {}
        self.counts = dict(received=0, transcribed=0, silence=0, rejected=0,
                           prepared=0, fallback=0, cancelled=0)

    def observe(self, raw, now):
        self.seen = {key: at for key, at in self.seen.items() if at + VOICE_MAX_AGE > now}
        snapshot = voice_snapshot(raw, self.core.settings, self.core.session, now, diagnostics=self.last_input)
        if snapshot is None:
            self.counts["rejected"] += 1
            return
        event, payload, _ = snapshot
        key = event["room"], event["id"]
        if (key in self.seen or len(self.seen) >= 4096 or len(self.inputs) >= 4
                or self.core.journal.event_time(self.core.settings, self.core.session,
                                                event["id"], event["room"]) is not None):
            return
        self.seen[key] = now
        try:
            job = self.runtime.recognize(payload)
        except Exception:
            self.last_input['stage'] = 'asr_submit_failed'
            self.counts["rejected"] += 1
            return
        self.inputs[job] = event
        self.last_input['stage'] = 'asr_pending'
        self.counts["received"] += 1

    def _validate(self, frame, now):
        self.core._session(frame)
        event = self.core.events.get(frame.get("reply_to"))
        text = frame.get("text")
        if (event is None or frame.get("room") != event["room"]
                or not timestamp(now) or not event["received_at"] <= now < event["received_at"] + VOICE_MAX_AGE
                or not isinstance(text, str) or not text.strip() or "\0" in text
                or len((NOTICE + text).encode("utf-8")) > 1023
                or frame.get("profile", "chat") not in ("chat", "research")):
            raise BridgeError("invalid_voice_binding")
        return event

    def prepare(self, frame, now):
        fields = {"type", "session", "request_id", "reply_to", "room", "text"}
        if (set(frame) not in (fields, fields | {"profile"}) or frame.get("type") != "prepare_voice"
                or not uuid_text(frame.get("request_id"))):
            raise BridgeError("invalid_voice_prepare")
        event = self._validate(frame, now)
        request = frame["request_id"]
        if request in self.preparations or len(self.preparations) >= 8:
            raise BridgeError("voice_prepare_capacity")
        entry = dict(frame=frame, expires=min(now + 170, event["received_at"] + VOICE_MAX_AGE),
                     job=None, ready=False, audio=None)
        self.preparations[request] = entry
        try:
            entry["job"] = self.runtime.synthesize(frame["text"])
        except Exception:
            entry["ready"] = True
            self.counts["fallback"] += 1
            return self._result(request, False)
        return None

    def _result(self, request, okay):
        return {"type": "voice_prepared", "session": self.core.session,
                "request_id": request, "status": "ready" if okay else "fallback"}

    def cancel(self, frame):
        self.core._session(frame)
        if set(frame) != {"type", "session", "request_id"} or not uuid_text(frame.get("request_id")):
            raise BridgeError("invalid_voice_cancel")
        entry = self.preparations.pop(frame["request_id"], None)
        if entry is not None and entry["job"] is not None:
            self.runtime.cancel(entry["job"])
        self.counts["cancelled"] += 1

    def poll(self, now):
        events, responses = [], []
        for job, event in tuple(self.inputs.items()):
            try:
                if now >= event["received_at"] + 25:
                    raise SpeechError("transcription_expired")
                result = self.runtime.result(job)
                if result is None:
                    continue
                text = result["result"]["text"]
                if not text.strip():
                    self.last_input['stage'] = 'silence'
                    self.counts["silence"] += 1
                else:
                    event["text"] = text
                    if self.core.journal.claim_event(self.core.settings, self.core.session, event):
                        events.append(event)
                        self.last_input['stage'] = 'transcribed'
                        self.counts["transcribed"] += 1
            except Exception:
                self.last_input['stage'] = 'asr_failed'
                self.counts["rejected"] += 1
            self.inputs.pop(job)
            self.runtime.cancel(job)
        for request, entry in tuple(self.preparations.items()):
            if entry["ready"]:
                if now >= entry["expires"]:
                    self.cancel(dict(type="cancel_voice", session=self.core.session, request_id=request))
                continue
            try:
                if now >= entry["expires"]:
                    raise SpeechError("preparation_expired")
                result = self.runtime.result(entry["job"])
                if result is None:
                    continue
                entry["audio"] = self.runtime.encoded(entry["job"])
                self.counts["prepared"] += 1
            except Exception:
                self.runtime.cancel(entry["job"])
                entry["job"] = None
                self.counts["fallback"] += 1
            entry["ready"] = True
            # Keep definite-failure handles valid through the dialogue deadline.
            entry["expires"] = self.core.events[entry["frame"]["reply_to"]]["received_at"] + VOICE_MAX_AGE
            responses.append(self._result(request, entry["audio"] is not None))
        self.runtime.reap()
        return events, responses

    def commit(self, frame, now):
        fields = {"type", "session", "request_id", "reply_to", "room", "text", "expires_at", "handle"}
        if (set(frame) not in (fields, fields | {"profile"}) or frame.get("type") != "commit_voice"
                or not uuid_text(frame.get("request_id"))):
            raise BridgeError("invalid_voice_commit")
        self._validate(frame, now)
        handle = frame["handle"]
        entry = self.preparations.get(handle) if isinstance(handle, str) else None
        if handle is not None and (entry is None or not entry["ready"] or now >= entry["expires"]
                or any(frame.get(key) != entry["frame"].get(key)
                       for key in ("room", "reply_to", "text", "profile"))):
            return None, self.core.result(frame["request_id"], "rejected"), None
        audio = entry["audio"] if entry else None
        if audio is not None:
            try:
                if (sha256(audio["path"]) != audio["sha256"] or
                    sha256(self.runtime.settings["reference_audio"]) != audio["reference_sha256"]):
                    audio = None
            except OSError:
                audio = None
        plain = {key: value for key, value in frame.items() if key != "handle"}
        plain["type"] = "send"
        prepared, immediate = self.core.prepare(plain, now, voice_authorized=True)
        if prepared is not None and audio is None:
            prepared = replace(prepared, text=NOTICE + prepared.text)
        return prepared, immediate, audio

    def completed(self, prepared, outcome, pid, audio):
        if audio is None:
            return self.core.complete(prepared, outcome, pid)
        status = "native_completed" if (isinstance(outcome, dict)
            and outcome.get("state") == "NATIVE_COMPLETED"
            and outcome.get("duration_ms") == audio["duration_ms"]) else "unknown"
        self.core.journal.finish(prepared.request_id, status)
        self.core.counts[status] += 1
        return self.core.result(prepared.request_id, status)

    def release(self, frame):
        handle = frame.get("handle")
        if handle in self.preparations:
            self.cancel(dict(type="cancel_voice", session=self.core.session, request_id=handle))
