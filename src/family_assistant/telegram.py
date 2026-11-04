import json
import secrets

from family_assistant.http_util import request, request_json


class Telegram:
    def __init__(self, token: str):
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"

    def __repr__(self) -> str:
        return "Telegram(token=***)"

    def call(self, method: str, payload: dict | None = None, timeout: float = 35) -> dict:
        result = request_json(
            f"{self._base}/{method}",
            payload,
            timeout=timeout,
        )
        if not result.get("ok", False):
            raise RuntimeError(f"telegram {method} failed")
        body = result.get("result")
        return body if isinstance(body, dict) else {"result": body}

    def delete_webhook(self) -> None:
        self.call("deleteWebhook", {"drop_pending_updates": False})

    def get_updates(self, offset: int | None) -> list[dict]:
        payload: dict = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload, timeout=40)
        updates = result.get("result")
        return updates if isinstance(updates, list) else []

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        last = {"message_id": 0}
        for part in _chunks(text):
            payload: dict = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
                reply_markup = None
            last = self.call("sendMessage", payload)
        return last

    def send_audio(self, chat_id: int, audio: bytes, filename: str = "reply.mp3") -> None:
        body, boundary = _audio_form(chat_id, audio, filename)
        status_body = request(
            f"{self._base}/sendAudio",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            body=body,
            timeout=60,
        )[2]
        parsed = json.loads(status_body.decode("utf-8"))
        if not parsed.get("ok"):
            raise RuntimeError("telegram sendAudio failed")

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})

    def clear_keyboard(self, chat_id: int, message_id: int) -> None:
        self.call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )

    def download(self, file_id: str) -> bytes:
        meta = self.call("getFile", {"file_id": file_id})
        path = meta.get("file_path")
        if not isinstance(path, str) or not path:
            raise RuntimeError("telegram file has no path")
        _status, _content_type, payload = request(f"{self._file_base}/{path}", method="GET", timeout=60)
        return payload


def _chunks(text: str, size: int = 4000) -> list[str]:
    if not text:
        return ["…"]
    return [text[index : index + size] for index in range(0, len(text), size)]


def _audio_form(chat_id: int, audio: bytes, filename: str) -> tuple[bytes, str]:
    boundary = "fa" + secrets.token_hex(8)
    head = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"audio\"; filename=\"{filename}\"\r\n"
        f"Content-Type: audio/mpeg\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + audio + tail, boundary
