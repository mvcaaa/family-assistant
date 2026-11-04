import os
from dataclasses import dataclass
from pathlib import Path

from family_assistant.phones import normalize_phone


def load_env_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    bot_token: str
    xai_api_key: str
    xai_model: str
    backend: str
    assistant_name: str
    allowed_phones: frozenset[str]
    owner_phone: str
    data_dir: Path
    enable_web_search: bool
    voice: bool
    tts_voice: str
    tts_language: str
    stt_language: str
    approval_ttl: int
    review_days: int
    webhook_url: str = ""
    webhook_key: str = ""
    gateway_public_url: str = ""
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8787
    pass_ttl: int = 900

    @property
    def database(self) -> Path:
        return self.data_dir / "assistant.sqlite"

    @property
    def rules_path(self) -> Path:
        return self.data_dir / "rules.md"

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is required")
        backend = os.environ.get("ASSISTANT_BACKEND", "api").strip()
        if backend not in {"api", "echo", "bot"}:
            raise SystemExit("ASSISTANT_BACKEND must be api, echo, or bot")
        api_key = os.environ.get("XAI_API_KEY", "").strip()
        if backend == "api" and not api_key:
            raise SystemExit("XAI_API_KEY is required when ASSISTANT_BACKEND=api")
        webhook_url = os.environ.get("GROK_BOT_WEBHOOK_URL", "").strip()
        webhook_key = os.environ.get("GROK_BOT_WEBHOOK_KEY", "").strip()
        public_url = os.environ.get("GATEWAY_PUBLIC_URL", "").strip().rstrip("/")
        if backend == "bot":
            if not webhook_url.startswith("https://"):
                raise SystemExit("GROK_BOT_WEBHOOK_URL must be an https URL")
            if not webhook_key:
                raise SystemExit("GROK_BOT_WEBHOOK_KEY is required when ASSISTANT_BACKEND=bot")
            if not public_url.startswith(("https://", "http://")):
                raise SystemExit("GATEWAY_PUBLIC_URL must be an http(s) URL")
        phones = _phones(os.environ.get("ALLOWED_PHONES", ""))
        if not phones:
            raise SystemExit("ALLOWED_PHONES is required")
        owner = normalize_phone(os.environ.get("OWNER_PHONE", ""))
        if owner not in phones:
            raise SystemExit("OWNER_PHONE must be one of ALLOWED_PHONES")
        data_dir = Path(os.environ.get("DATA_DIR", "./data")).expanduser()
        return cls(
            bot_token=token,
            xai_api_key=api_key,
            xai_model=os.environ.get("XAI_MODEL", "grok-4.7").strip() or "grok-4.7",
            backend=backend,
            assistant_name=os.environ.get("ASSISTANT_NAME", "Марфа").strip() or "Марфа",
            allowed_phones=phones,
            owner_phone=owner,
            data_dir=data_dir,
            enable_web_search=_flag("ENABLE_WEB_SEARCH", True),
            voice=_flag("VOICE", True),
            tts_voice=os.environ.get("TTS_VOICE", "ara").strip() or "ara",
            tts_language=os.environ.get("TTS_LANGUAGE", "ru").strip() or "ru",
            stt_language=os.environ.get("STT_LANGUAGE", "ru").strip() or "ru",
            approval_ttl=_positive("APPROVAL_TTL", 600),
            review_days=_positive("REVIEW_DAYS", 7),
            webhook_url=webhook_url,
            webhook_key=webhook_key,
            gateway_public_url=public_url,
            gateway_host=os.environ.get("GATEWAY_HOST", "127.0.0.1").strip() or "127.0.0.1",
            gateway_port=_positive("GATEWAY_PORT", 8787),
            pass_ttl=_positive("PASS_TTL", 900),
        )


def _phones(raw: str) -> frozenset[str]:
    found: set[str] = set()
    for part in raw.split(","):
        number = normalize_phone(part)
        if number:
            found.add(number)
    return frozenset(found)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _positive(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value <= 0:
        raise SystemExit(f"{name} must be positive")
    return value
