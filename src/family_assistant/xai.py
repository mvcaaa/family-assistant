import json

from family_assistant.http_util import multipart, request, request_json


class XAI:
    def __init__(self, api_key: str, model: str, tts_voice: str, tts_language: str, stt_language: str):
        self.api_key = api_key
        self.model = model
        self.tts_voice = tts_voice
        self.tts_language = tts_language
        self.stt_language = stt_language

    def __repr__(self) -> str:
        return f"XAI(model={self.model})"

    def respond(self, payload: dict) -> dict:
        return request_json(
            "https://api.x.ai/v1/responses",
            payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=120,
        )

    def transcribe(self, audio: bytes) -> str:
        body, boundary = multipart(
            [
                ("model", "grok-voice-transcribe-2.0"),
                ("language", self.stt_language),
            ],
            "voice.ogg",
            audio,
            "audio/ogg",
        )
        _status, content_type, raw = request(
            "https://api.x.ai/v1/stt",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            body=body,
            timeout=90,
        )
        if "json" not in content_type and not raw.lstrip().startswith(b"{"):
            raise RuntimeError("неожиданный ответ распознавания")
        parsed = json.loads(raw.decode("utf-8"))
        text = parsed.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("пустая расшифровка")
        return text.strip()

    def speak(self, text: str) -> bytes:
        spoken = text.strip()
        if len(spoken) > 1500:
            spoken = spoken[:1500]
        _status, content_type, raw = request(
            "https://api.x.ai/v1/tts",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            body=json.dumps(
                {
                    "text": spoken,
                    "voice_id": self.tts_voice,
                    "language": self.tts_language,
                }
            ).encode("utf-8"),
            timeout=90,
        )
        if "json" in content_type or raw.lstrip().startswith(b"{") or raw.lstrip().startswith(b"["):
            raise RuntimeError("синтез речи вернул не аудио")
        return raw
