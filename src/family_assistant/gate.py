import json
from datetime import datetime

from family_assistant.db import Store, StoreError
from family_assistant.phones import normalize_phone
from family_assistant.prompts import SENSITIVE_KINDS


class ScopeError(Exception):
    pass


class Gate:
    """Tools the model can reach. Every method is bound to one pass."""

    def __init__(self, store: Store, telegram, token: str, now: datetime, approval_ttl: int):
        row = store.valid_pass(token, now)
        if row is None:
            raise ScopeError("пропуск недействителен")
        self.store = store
        self.telegram = telegram
        self.chat_id = int(row["chat_id"])
        self.user_id = int(row["user_id"])
        self.now = now
        self.approval_ttl = approval_ttl
        self.proposals = 0

    def history(self, limit: int = 20) -> list[tuple[str, str]]:
        return [(str(row["role"]), str(row["text"])) for row in self.store.history(self.chat_id, limit)]

    def memory(self) -> str:
        return self.store.memory(self.user_id)

    def reply(self, text: str) -> None:
        self.telegram.send_message(self.chat_id, text)

    def append_memory(self, text: str) -> dict:
        try:
            body = self.store.append_memory(self.user_id, text, self.now)
        except StoreError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "chars": len(body)}

    def propose_action(
        self,
        kind: str,
        summary: str,
        target_phone: str = "",
        new_body: str | None = None,
    ) -> dict:
        if kind not in SENSITIVE_KINDS:
            return {"ok": False, "error": "неизвестный тип действия"}
        summary = " ".join(summary.split())
        if not summary or len(summary) > 500:
            return {"ok": False, "error": "нужна короткая суть, до 500 символов"}
        if self.store.pending_count(self.chat_id, self.now) >= 5:
            return {"ok": False, "error": "слишком много неподтверждённых действий"}
        owner = self.store.owner()
        if owner is None:
            return {"ok": False, "error": "владелец ещё не открыл бота"}
        target_id = self.user_id
        if target_phone.strip():
            phone = normalize_phone(target_phone)
            person = self.store.user_by_phone(phone)
            if person is None:
                return {"ok": False, "error": "такого номера нет среди своих"}
            target_id = int(person["telegram_id"])
        if kind == "memory_replace":
            if new_body is None:
                return {"ok": False, "error": "для замены заметки нужен полный новый текст"}
            if len(new_body) > 8000:
                return {"ok": False, "error": "новая заметка длиннее 8000 символов"}
        else:
            new_body = None
        approval_id = self.store.create_approval(
            chat_id=self.chat_id,
            user_id=self.user_id,
            target_user_id=target_id,
            kind=kind,
            summary=summary,
            new_body=new_body,
            ttl_seconds=self.approval_ttl,
            now=self.now,
        )
        requester = self.store.user_by_telegram(self.user_id)
        who = str(requester["name"]) if requester else str(self.user_id)
        preview = ""
        if new_body is not None:
            preview = "\n\nНовый текст:\n" + new_body[:800]
        try:
            self._send_approval(int(owner["telegram_id"]), who, kind, summary, preview, approval_id)
        except Exception:
            self.store.mark_approval(approval_id, "failed")
            return {"ok": False, "error": "не удалось отправить кнопку владельцу"}
        self.proposals += 1
        return {
            "ok": True,
            "waiting_for_owner": True,
            "expires_in_seconds": self.approval_ttl,
            "note": "Действие не выполнено. Ждёт одноразовую кнопку владельца.",
        }

    def _send_approval(self, owner_id: int, who: str, kind: str, summary: str, preview: str, approval_id: str) -> None:
        self.telegram.send_message(
            owner_id,
            (
                f"Подтверди одно действие. Кнопка живёт {self.approval_ttl // 60} мин.\n"
                f"Кто просит: {who}\n"
                f"Тип: {kind}\n"
                f"Суть: {summary}"
                f"{preview}"
            ),
            reply_markup={
                "inline_keyboard": [[
                    {"text": "Да, сделать это", "callback_data": f"y{approval_id}"},
                    {"text": "Нет", "callback_data": f"n{approval_id}"},
                ]]
            },
        )


def dispatch_tool(gate: Gate, name: str, arguments: str | dict) -> str:
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "битые аргументы"}, ensure_ascii=False)
    else:
        parsed = arguments
    if not isinstance(parsed, dict):
        return json.dumps({"ok": False, "error": "аргументы должны быть объектом"}, ensure_ascii=False)
    if name == "append_memory":
        result = gate.append_memory(str(parsed.get("text", "")))
    elif name == "propose_action":
        result = gate.propose_action(
            kind=str(parsed.get("kind", "")),
            summary=str(parsed.get("summary", "")),
            target_phone=str(parsed.get("target_phone", "") or ""),
            new_body=parsed.get("new_body") if isinstance(parsed.get("new_body"), str) else None,
        )
    else:
        result = {"ok": False, "error": "неизвестный инструмент"}
    return json.dumps(result, ensure_ascii=False)
