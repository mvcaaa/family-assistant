import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from family_assistant.agent import run_turn
from family_assistant.approvals import settle
from family_assistant.bot_mode import _handler_factory, build_wake_payload
from family_assistant.config import Config
from family_assistant.db import Store, iso
from family_assistant.gate import Gate, ScopeError
from family_assistant.loop import family_notes, handle_update, maybe_review
from family_assistant.phones import normalize_phone
from family_assistant.prompts import build_instructions

NOW = __import__("datetime").datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
OWNER_ID = 101
CHILD_ID = 202


class FakeTelegram:
    def __init__(self, fail_approval: bool = False):
        self.sent = []
        self.callbacks = []
        self.cleared = []
        self.fail_approval = fail_approval

    def send_message(self, chat_id, text, reply_markup=None):
        if self.fail_approval and text.startswith("Подтверди"):
            raise RuntimeError("down")
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def answer_callback(self, callback_id, text):
        self.callbacks.append((callback_id, text))

    def clear_keyboard(self, chat_id, message_id):
        self.cleared.append((chat_id, message_id))


class FakeXAI:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.payloads = []
        self.model = "grok-test"

    def respond(self, payload):
        self.payloads.append(payload)
        return self.outputs.pop(0)


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        (self.data / "rules.md").write_text("Не выдумывай. CANARY-RULES", encoding="utf-8")
        self.store = Store(self.data / "assistant.sqlite")
        self.store.migrate()
        self.telegram = FakeTelegram()
        self.cfg = Config(
            bot_token="secret-token-canary",
            xai_api_key="secret-key-canary",
            xai_model="grok-test",
            backend="echo",
            assistant_name="Aino",
            allowed_phones=frozenset({"358401111111", "358402222222"}),
            owner_phone="358401111111",
            data_dir=self.data,
            enable_web_search=True,
            voice=False,
            tts_voice="ara",
            tts_language="ru",
            stt_language="ru",
            approval_ttl=600,
            review_days=7,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_phone_normalization(self):
        self.assertEqual(normalize_phone("+358 40-111 1111"), "358401111111")
        self.assertEqual(normalize_phone("00358401111111"), "358401111111")

    def test_owner_must_be_on_the_list(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "ALLOWED_PHONES": "358401111111",
                "OWNER_PHONE": "358409999999",
                "ASSISTANT_BACKEND": "echo",
            },
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                Config.from_env()

    def test_only_own_contact_from_the_list_is_admitted(self):
        self._update(self._contact(1, CHILD_ID, "+358402222222", "Ребёнок"))
        self.assertIsNotNone(self.store.user_by_telegram(CHILD_ID))
        self._update(self._contact(2, 303, "+358402222222", "Чужой", contact_user=CHILD_ID))
        self.assertIsNone(self.store.user_by_telegram(303))
        self._update(self._contact(3, 304, "+358409999999", "Вне списка"))
        self.assertIsNone(self.store.user_by_telegram(304))
        self.assertIn("нет в списке", self.telegram.sent[-1]["text"])

    def test_group_message_is_ignored(self):
        self._register()
        self._update(
            {
                "update_id": 10,
                "message": {
                    "chat": {"id": -100, "type": "group"},
                    "from": {"id": CHILD_ID, "first_name": "Ребёнок"},
                    "text": "секрет группы",
                },
            }
        )
        self.assertEqual(self.store.history(-100), [])

    def test_stranger_is_asked_for_a_contact_and_not_answered(self):
        self._update(self._text(1, 303, "привет"))
        self.assertEqual(self.store.history(303), [])
        self.assertIn("request_contact", json.dumps(self.telegram.sent[-1]["reply_markup"]))

    def test_echo_stays_in_the_sender_chat(self):
        self._register()
        self._update(self._text(5, CHILD_ID, "который час"))
        self.assertEqual(self.telegram.sent[-1]["chat_id"], CHILD_ID)
        self.assertIn("Эхо: который час", self.telegram.sent[-1]["text"])

    def test_pass_reads_only_its_chat_and_its_memory(self):
        self._register()
        self.store.add_message(OWNER_ID, OWNER_ID, "user", "PARENT-SECRET", NOW)
        self.store.append_memory(OWNER_ID, "владелец любит чай", NOW)
        child = self._gate(CHILD_ID)
        dumped = json.dumps(child.history(), ensure_ascii=False)
        self.assertNotIn("PARENT-SECRET", dumped)
        self.assertNotIn("чай", child.memory())
        child.append_memory("любит плавание")
        self.assertIn("плавание", self.store.memory(CHILD_ID))
        self.assertNotIn("плавание", self.store.memory(OWNER_ID))
        child.reply("ответ ребёнку")
        self.assertEqual(self.telegram.sent[-1]["chat_id"], CHILD_ID)

    def test_expired_pass_cannot_open_a_gate(self):
        self._register()
        token = self.store.issue_pass(CHILD_ID, CHILD_ID, 60, NOW)
        with self.assertRaises(ScopeError):
            Gate(self.store, self.telegram, token, NOW + timedelta(seconds=61), 600)

    def test_sensitive_action_waits_for_the_owner_button(self):
        self._register()
        child = self._gate(CHILD_ID)
        result = child.propose_action("purchase", "абонемент на ноябрь")
        self.assertTrue(result["waiting_for_owner"])
        button = self.telegram.sent[-1]
        self.assertEqual(button["chat_id"], OWNER_ID)
        approval_id = button["reply_markup"]["inline_keyboard"][0][0]["callback_data"][1:]
        outcome, _row = settle(self.store, CHILD_ID, approval_id, True, NOW)
        self.assertEqual(outcome, "forbidden")
        self.assertEqual(self.store.approval(approval_id)["status"], "pending")
        outcome, _row = settle(self.store, OWNER_ID, approval_id, True, NOW)
        self.assertEqual(outcome, "approved")
        outcome, _row = settle(self.store, OWNER_ID, approval_id, True, NOW)
        self.assertEqual(outcome, "stale")
        self.assertEqual(self.store.approval(approval_id)["status"], "approved")

    def test_expired_button_does_not_approve(self):
        self._register()
        approval_id = self.store.create_approval(
            CHILD_ID, CHILD_ID, CHILD_ID, "email", "письмо тренеру", None, 600, NOW
        )
        outcome, _row = settle(self.store, OWNER_ID, approval_id, True, NOW + timedelta(seconds=601))
        self.assertEqual(outcome, "stale")
        self.assertEqual(self.store.approval(approval_id)["status"], "expired")

    def test_memory_replace_keeps_the_previous_version(self):
        self._register()
        self.store.append_memory(CHILD_ID, "старый факт", NOW)
        child = self._gate(CHILD_ID)
        child.propose_action("memory_replace", "убрать старый факт", new_body="новый факт")
        approval_id = self.telegram.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"][1:]
        self.assertIn("старый факт", self.store.memory(CHILD_ID))
        self._update(
            {
                "update_id": 20,
                "callback_query": {
                    "id": "cb1",
                    "from": {"id": OWNER_ID},
                    "data": f"y{approval_id}",
                    "message": {"message_id": 4, "chat": {"id": OWNER_ID}},
                },
            }
        )
        self.assertEqual(self.store.memory(CHILD_ID), "новый факт")
        self.assertIn("старый факт", self.store.versions(CHILD_ID)[0]["body"])
        self.assertIn("Прошлая версия", self.telegram.sent[-1]["text"])

    def test_failed_button_delivery_does_not_leave_a_live_approval(self):
        self._register()
        self.telegram.fail_approval = True
        result = self._gate(CHILD_ID).propose_action("delete", "удалить файл")
        self.assertFalse(result["ok"])
        row = self.store.conn.execute("SELECT status FROM approvals").fetchone()
        self.assertEqual(row["status"], "failed")

    def test_review_context_is_only_for_the_owner(self):
        self._register()
        self.store.append_memory(CHILD_ID, "CHILD-NOTE", NOW)
        owner = self.store.user_by_telegram(OWNER_ID)
        child = self.store.user_by_telegram(CHILD_ID)
        self.assertIsNone(family_notes(self.store, child, "ревизия"))
        self.assertIsNone(family_notes(self.store, owner, "что завтра"))
        notes = family_notes(self.store, owner, "/review@family_bot")
        self.assertIn("CHILD-NOTE", build_instructions("Aino", "rules", "Я", "", notes))

    def test_model_payload_has_no_token_and_no_other_chat(self):
        self._register()
        self.store.add_message(OWNER_ID, OWNER_ID, "user", "PARENT-SECRET", NOW)
        self.store.add_message(CHILD_ID, CHILD_ID, "user", "мой вопрос", NOW)
        xai = FakeXAI([
            {
                "id": "resp_1",
                "output": [{
                    "type": "function_call",
                    "name": "append_memory",
                    "arguments": json.dumps({"text": "любит плавание"}),
                    "call_id": "call_1",
                }],
            },
            {"id": "resp_2", "output": [{"type": "message", "content": "записал"}]},
        ])
        gate = self._gate(CHILD_ID)
        reply = run_turn(xai, gate, "инструкция без секрета", True)
        blob = json.dumps(xai.payloads, ensure_ascii=False)
        self.assertEqual(reply, "записал")
        self.assertNotIn("PARENT-SECRET", blob)
        self.assertNotIn("secret-token-canary", blob)
        self.assertNotIn("secret-key-canary", blob)
        self.assertIn("мой вопрос", blob)
        self.assertIn("web_search", blob)
        self.assertIn("плавание", self.store.memory(CHILD_ID))
        follow = json.dumps(xai.payloads[1], ensure_ascii=False)
        self.assertIn("function_call_output", follow)
        self.assertNotIn("мой вопрос", follow)

    def test_weekly_review_is_sent_once(self):
        self._register()
        self.store.set_meta("last_review", iso(NOW - timedelta(days=8)))
        maybe_review(self.cfg, self.store, self.telegram, NOW)
        maybe_review(self.cfg, self.store, self.telegram, NOW)
        reviews = [item for item in self.telegram.sent if "сверить память" in item["text"]]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["chat_id"], OWNER_ID)

    def test_bot_mode_requires_https_webhook(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "ASSISTANT_BACKEND": "bot",
                "ALLOWED_PHONES": "358401111111",
                "OWNER_PHONE": "358401111111",
                "GROK_BOT_WEBHOOK_URL": "http://example.test/hook",
                "GROK_BOT_WEBHOOK_KEY": "key",
                "GATEWAY_PUBLIC_URL": "https://family.example",
            },
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                Config.from_env()

    def test_bot_wake_payload_hides_tokens(self):
        self._register()
        cfg = self._bot_cfg()
        person = self.store.user_by_telegram(CHILD_ID)
        payload = build_wake_payload(cfg, person, "привет", "pass-value", NOW)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret-token-canary", blob)
        self.assertNotIn("secret-webhook-key", blob)
        self.assertIn("pass-value", blob)
        self.assertIn("https://family.example/v1/reply", blob)
        with patch("family_assistant.bot_mode.post_wake", return_value=200) as woke:
            handle_update(cfg, self.store, self.telegram, None, self._text(8, CHILD_ID, "привет"), NOW)
        sent = json.dumps(woke.call_args.args[1], ensure_ascii=False)
        self.assertNotIn("secret-webhook-key", sent)
        self.assertNotIn("secret-token-canary", sent)
        self.assertIn("Передал", self.telegram.sent[-1]["text"])

    def test_bot_mode_asks_for_text_instead_of_voice(self):
        self._register()
        cfg = self._bot_cfg()
        update = self._text(9, CHILD_ID, "")
        update["message"].pop("text")
        update["message"]["voice"] = {"file_id": "voice-file"}
        with patch("family_assistant.bot_mode.post_wake") as woke:
            handle_update(cfg, self.store, self.telegram, None, update, NOW)
        woke.assert_not_called()
        self.assertIn("напиши словами", self.telegram.sent[-1]["text"].lower())

    def test_pass_api_replies_only_inside_its_chat(self):
        self._register()
        self.store.add_message(OWNER_ID, OWNER_ID, "user", "PARENT-SECRET", NOW)
        self.store.append_memory(OWNER_ID, "секрет владельца", NOW)
        self.store.add_message(CHILD_ID, CHILD_ID, "user", "мой вопрос", NOW)
        token = self.store.issue_pass(CHILD_ID, CHILD_ID, 600, datetime.now(timezone.utc))
        cfg = self._bot_cfg()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(cfg, self.store, self.telegram))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            context = _api(base, "GET", "/v1/context", token)
            self.assertEqual(context["who"], "Ребёнок")
            self.assertIsNone(context["family_notes"])
            blob = json.dumps(context, ensure_ascii=False)
            self.assertIn("мой вопрос", blob)
            self.assertNotIn("PARENT-SECRET", blob)
            self.assertNotIn("секрет владельца", blob)
            denied = _api(base, "GET", "/v1/context", "no-such-pass", expect=401)
            self.assertFalse(denied["ok"])
            _api(base, "POST", "/v1/reply", token, {"text": "ответ ребёнку"})
            self.assertEqual(self.telegram.sent[-1]["chat_id"], CHILD_ID)
            self.assertEqual(self.telegram.sent[-1]["text"], "ответ ребёнку")
            proposed = _api(
                base,
                "POST",
                "/v1/propose",
                token,
                {"kind": "purchase", "summary": "билет"},
            )
            self.assertTrue(proposed["waiting_for_owner"])
            self.assertEqual(self.telegram.sent[-1]["chat_id"], OWNER_ID)
        finally:
            server.shutdown()
            server.server_close()

    def _bot_cfg(self):
        from dataclasses import replace

        return replace(
            self.cfg,
            backend="bot",
            webhook_url="https://api2.cursor.sh/automations/webhook/abc",
            webhook_key="secret-webhook-key",
            gateway_public_url="https://family.example",
        )

    def _gate(self, user_id: int) -> Gate:
        token = self.store.issue_pass(user_id, user_id, 600, NOW)
        return Gate(self.store, self.telegram, token, NOW, self.cfg.approval_ttl)

    def _register(self):
        self._update(self._contact(1, OWNER_ID, "358401111111", "Андрей"))
        self._update(self._contact(2, CHILD_ID, "358402222222", "Ребёнок"))

    def _update(self, update):
        handle_update(self.cfg, self.store, self.telegram, None, update, NOW)

    def _contact(self, update_id, user_id, phone, name, contact_user=None):
        return {
            "update_id": update_id,
            "message": {
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "first_name": name},
                "contact": {"user_id": user_id if contact_user is None else contact_user, "phone_number": phone},
            },
        }

    def _text(self, update_id, user_id, text):
        return {
            "update_id": update_id,
            "message": {
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "first_name": "кто-то"},
                "text": text,
            },
        }


def _api(base, method, path, token, body=None, expect=200):
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        status = error.code
        payload = json.loads(error.read().decode("utf-8"))
        error.close()
    if status != expect:
        raise AssertionError(f"{method} {path} -> {status}, expected {expect}: {payload}")
    return payload


if __name__ == "__main__":
    unittest.main()
