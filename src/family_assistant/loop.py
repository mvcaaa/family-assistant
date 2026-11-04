import logging
import re
import threading
import time
from datetime import datetime, timedelta

from family_assistant.agent import run_turn
from family_assistant.approvals import settle
from family_assistant.bot_mode import start_pass_api, wake_bot
from family_assistant.config import Config
from family_assistant.db import Store, iso, parse_iso, utcnow
from family_assistant.gate import Gate
from family_assistant.phones import mask_phone, normalize_phone
from family_assistant.prompts import build_instructions, command_name, is_review
from family_assistant.xai import XAI

log = logging.getLogger("family_assistant")

CONTACT_KEYBOARD = {
    "keyboard": [[{"text": "Поделиться номером", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}
REMOVE_KEYBOARD = {"remove_keyboard": True}
CALLBACK_RE = re.compile(r"^[yn][0-9a-f]{32}$")


def run(cfg: Config, store: Store, telegram, xai: XAI | None = None, sleep=time.sleep, now=utcnow) -> None:
    if cfg.backend == "api" and xai is None:
        xai = XAI(cfg.xai_api_key, cfg.xai_model, cfg.tts_voice, cfg.tts_language, cfg.stt_language)
    log.info("gateway started, backend=%s", cfg.backend)
    if cfg.backend == "bot":
        server = start_pass_api(cfg, store, telegram)
        threading.Thread(target=server.serve_forever, name="pass-api", daemon=True).start()
        log.info("pass api listening on %s:%s", cfg.gateway_host, cfg.gateway_port)
    try:
        telegram.delete_webhook()
    except Exception:
        log.exception("deleteWebhook failed")
    if store.get_meta("last_review") is None:
        store.set_meta("last_review", iso(now()))
    saved = store.get_meta("update_offset")
    offset = int(saved) if saved else None
    while True:
        try:
            updates = telegram.get_updates(offset)
        except Exception:
            log.exception("getUpdates failed")
            sleep(3)
            continue
        for update in updates:
            update_id = int(update["update_id"])
            try:
                handle_update(cfg, store, telegram, xai, update, now())
            except Exception:
                log.exception("update %s failed", update_id)
            offset = update_id + 1
            store.set_meta("update_offset", str(offset))
        try:
            maybe_review(cfg, store, telegram, now())
        except Exception:
            log.exception("memory review failed")


def handle_update(cfg: Config, store: Store, telegram, xai, update: dict, now: datetime) -> None:
    if "callback_query" in update:
        handle_callback(cfg, store, telegram, xai, update["callback_query"], now)
        return
    message = update.get("message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private" or "id" not in sender or "id" not in chat:
        return
    chat_id = int(chat["id"])
    user_id = int(sender["id"])
    if "contact" in message:
        handle_contact(cfg, store, telegram, message, chat_id, user_id, now)
        return
    person = store.user_by_telegram(user_id)
    if person is None:
        telegram.send_message(
            chat_id,
            "Я отвечаю только своим. Нажми кнопку и поделись своим номером.",
            reply_markup=CONTACT_KEYBOARD,
        )
        return
    text = message.get("text") if isinstance(message.get("text"), str) else ""
    command = command_name(text)
    if command == "/start":
        telegram.send_message(chat_id, _help(cfg, person))
        return
    if command == "/memory":
        telegram.send_message(chat_id, store.memory(user_id) or "(пусто)")
        return
    if command == "/whoami":
        role = "владелец" if int(person["is_owner"]) else "семья"
        telegram.send_message(chat_id, f"{person['name']}, {role}, номер {mask_phone(str(person['phone']))}.")
        return
    voice = isinstance(message.get("voice"), dict)
    if voice and cfg.backend == "bot":
        telegram.send_message(chat_id, "В режиме подписки я читаю текст. Напиши словами.")
        return
    if voice:
        text = _transcribe(cfg, telegram, xai, message["voice"])
    if not text.strip():
        notice = "Не разобрал голосовое. Напиши текстом." if voice else "Пока понимаю текст и голосовые."
        telegram.send_message(chat_id, notice)
        return
    answer = answer_person(cfg, store, telegram, xai, person, chat_id, text.strip(), now)
    if voice and cfg.voice and cfg.backend == "api" and xai is not None:
        try:
            telegram.send_audio(chat_id, xai.speak(answer))
        except Exception:
            log.exception("voice reply failed")


def answer_person(cfg, store, telegram, xai, person, chat_id: int, text: str, now: datetime) -> str:
    user_id = int(person["telegram_id"])
    store.add_message(chat_id, user_id, "user", text, now)
    if cfg.backend == "echo":
        reply = f"Эхо: {text}"
    elif cfg.backend == "bot":
        error = wake_bot(cfg, store, person, chat_id, text, now)
        reply = error or "Передал. Ответ придёт в этот чат."
    else:
        reply = _model_reply(cfg, store, telegram, xai, person, chat_id, text, now)
    store.add_message(chat_id, user_id, "assistant", reply, now)
    telegram.send_message(chat_id, reply)
    return reply


def _model_reply(cfg, store, telegram, xai, person, chat_id: int, text: str, now: datetime) -> str:
    token = store.issue_pass(chat_id, int(person["telegram_id"]), cfg.pass_ttl, now)
    gate = Gate(store, telegram, token, now, cfg.approval_ttl)
    notes = family_notes(store, person, text)
    instructions = build_instructions(
        cfg.assistant_name,
        cfg.rules_path.read_text(encoding="utf-8"),
        str(person["name"]),
        gate.memory(),
        notes,
    )
    reply = run_turn(xai, gate, instructions, cfg.enable_web_search).strip()
    if reply:
        return reply
    if gate.proposals:
        return "Жду подтверждения владельца. Кнопка одноразовая и живёт несколько минут."
    return "Не получилось сформулировать ответ. Попробуй ещё раз."


def family_notes(store: Store, person, text: str) -> list[tuple[str, str]] | None:
    if not int(person["is_owner"]) or not is_review(text):
        return None
    return [(str(user["name"]), store.memory(int(user["telegram_id"]))) for user in store.users()]


def handle_contact(cfg, store, telegram, message: dict, chat_id: int, user_id: int, now: datetime) -> None:
    contact = message.get("contact") or {}
    contact_user = contact.get("user_id")
    if contact_user is None or int(contact_user) != user_id:
        telegram.send_message(chat_id, "Нужен именно твой номер. Нажми кнопку «Поделиться номером».")
        return
    phone = normalize_phone(str(contact.get("phone_number", "")))
    if phone not in cfg.allowed_phones:
        log.info("rejected contact %s", mask_phone(phone))
        telegram.send_message(chat_id, "Этого номера нет в списке семьи. Чужим я не отвечаю.")
        return
    sender = message.get("from") or {}
    name = " ".join(part for part in (sender.get("first_name"), sender.get("last_name")) if part) or "свой"
    store.upsert_user(user_id, phone, name, phone == cfg.owner_phone, now)
    telegram.send_message(chat_id, f"Узнал тебя. Можно писать текстом и голосовыми.", reply_markup=REMOVE_KEYBOARD)


def handle_callback(cfg, store, telegram, xai, callback: dict, now: datetime) -> None:
    data = str(callback.get("data") or "")
    callback_id = str(callback.get("id") or "")
    actor = int((callback.get("from") or {}).get("id") or 0)
    if not CALLBACK_RE.fullmatch(data):
        telegram.answer_callback(callback_id, "Не понял кнопку")
        return
    outcome, row = settle(store, actor, data[1:], data[0] == "y", now)
    phrases = {
        "forbidden": "Эту кнопку нажимает только владелец",
        "missing": "Такого подтверждения нет",
        "stale": "Кнопка уже не действует",
    }
    if outcome in phrases:
        telegram.answer_callback(callback_id, phrases[outcome])
        return
    telegram.answer_callback(callback_id, "Принято" if outcome == "approved" else "Отклонено")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    if "id" in chat and "message_id" in message:
        try:
            telegram.clear_keyboard(int(chat["id"]), int(message["message_id"]))
        except Exception:
            log.exception("clear keyboard failed")
    if row is None:
        return
    if outcome == "approved" and str(row["kind"]) == "memory_replace":
        notice = "Владелец подтвердил правку заметки. Прошлая версия сохранена."
    elif outcome == "approved":
        notice = (
            f"Владелец подтвердил: {row['summary']}. "
            "Покупки, письма и доступ к серверу отсюда не выполняются. Могу подготовить черновик."
        )
    else:
        notice = f"Владелец отклонил: {row['summary']}"
    chat_id = int(row["chat_id"])
    store.add_message(chat_id, None, "system", notice, now)
    telegram.send_message(chat_id, notice)
    if outcome == "approved" and str(row["kind"]) != "memory_replace":
        person = store.user_by_telegram(int(row["user_id"]))
        if person is None:
            return
        if cfg.backend == "api" and xai is not None:
            _follow_up(cfg, store, telegram, xai, person, chat_id, now)
        elif cfg.backend == "bot":
            error = wake_bot(cfg, store, person, chat_id, notice, now)
            if error:
                telegram.send_message(chat_id, error)


def _follow_up(cfg, store, telegram, xai, person, chat_id: int, now: datetime) -> None:
    token = store.issue_pass(chat_id, int(person["telegram_id"]), cfg.pass_ttl, now)
    gate = Gate(store, telegram, token, now, cfg.approval_ttl)
    instructions = build_instructions(
        cfg.assistant_name,
        cfg.rules_path.read_text(encoding="utf-8"),
        str(person["name"]),
        gate.memory(),
        None,
    )
    reply = run_turn(xai, gate, instructions, cfg.enable_web_search).strip()
    if not reply:
        reply = "Подтверждение вижу. Напиши, какой черновик подготовить."
    store.add_message(chat_id, int(person["telegram_id"]), "assistant", reply, now)
    telegram.send_message(chat_id, reply)


def maybe_review(cfg: Config, store: Store, telegram, now: datetime) -> None:
    owner = store.owner()
    if owner is None:
        return
    last = store.get_meta("last_review")
    if last is not None and parse_iso(last) + timedelta(days=cfg.review_days) > now:
        return
    lines = [
        f"{user['name']}: {len(store.memory(int(user['telegram_id'])))} символов"
        for user in store.users()
    ]
    telegram.send_message(
        int(owner["telegram_id"]),
        "Пора сверить память. Сам ничего не удаляю.\n"
        + "\n".join(lines)
        + "\nНапиши «ревизия», и я предложу, что убрать. Удаление только после твоей кнопки.",
    )
    store.set_meta("last_review", iso(now))


def _transcribe(cfg: Config, telegram, xai, voice: dict) -> str:
    if cfg.backend != "api" or xai is None:
        return ""
    file_id = voice.get("file_id")
    if not isinstance(file_id, str):
        return ""
    try:
        return xai.transcribe(telegram.download(file_id))
    except Exception:
        log.exception("transcription failed")
        return ""


def _help(cfg: Config, person) -> str:
    role = "Ты владелец: серьёзные действия ждут твою кнопку." if int(person["is_owner"]) else "Серьёзные действия подтверждает владелец."
    return (
        f"Я {cfg.assistant_name}. Пиши текстом или голосовым.\n"
        f"{role}\n"
        "Команды: /memory — твоя заметка, /whoami — кто ты для меня.\n"
        "Владелец может написать «ревизия», чтобы разобрать память."
    )
