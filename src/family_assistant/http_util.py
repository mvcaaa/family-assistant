import json
import secrets
import urllib.error
import urllib.request


class HttpError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        super().__init__(f"HTTP {status}: {detail[:400]}")


def request(
    url: str,
    *,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 60,
) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type", "")
            return response.status, content_type, payload
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise HttpError(error.code, detail) from None


def request_json(
    url: str,
    payload: dict | None = None,
    *,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    merged = {"Accept": "application/json"}
    if payload is not None:
        merged["Content-Type"] = "application/json"
    if headers:
        merged.update(headers)
    _status, _content_type, raw = request(url, method=method, headers=merged, body=data, timeout=timeout)
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise HttpError(200, "ответ не объект JSON")
    return parsed


def multipart(fields: list[tuple[str, str]], filename: str, file_bytes: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = "fa" + secrets.token_hex(8)
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    chunks.append(file_bytes)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary
