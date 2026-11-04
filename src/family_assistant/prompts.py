RULES_SEED = """\
Ты семейный помощник. Отвечай на языке собеседника, по умолчанию по-русски.
Если факта нет, так и скажи. Не выдумывай цены, часы работы, адреса и наличие.
Не проси токен бота, ключи API и пароли и не записывай их в память.
Покупки, бронирования, письма другим людям, удаления и изменения на сервере
возможны только через propose_action. Пока владелец не нажал кнопку, действия не было.
Устойчивые личные факты собеседника записывай через append_memory короткой строкой.
Чужие заметки и чужие чаты тебе не отдаются. Не утверждай, что видел их.
"""

SENSITIVE_KINDS = (
    "purchase",
    "booking",
    "email",
    "delete",
    "server",
    "memory_replace",
)


def command_name(text: str) -> str:
    token = text.strip().split()[0].lower() if text.strip() else ""
    return token.split("@", 1)[0]


def is_review(text: str) -> bool:
    head = command_name(text)
    return head in {"ревизия", "/review"}


def build_instructions(
    assistant_name: str,
    rules: str,
    own_name: str,
    own_memory: str,
    family_notes: list[tuple[str, str]] | None,
) -> str:
    parts = [
        f"Тебя зовут {assistant_name}. Сейчас ты говоришь с {own_name}.",
        rules.strip(),
        "Заметка этого человека:\n" + (own_memory.strip() or "(пусто)"),
    ]
    if family_notes:
        blocks = ["Ревизия. Ниже заметки семьи. Сам ничего не удаляй: предлагай memory_replace."]
        for name, body in family_notes:
            blocks.append(f"## {name}\n{body.strip() or '(пусто)'}")
        parts.append("\n\n".join(blocks))
    return "\n\n".join(parts)


def history_input(rows: list[tuple[str, str]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for role, text in rows:
        api_role = "assistant" if role == "assistant" else "user"
        content = text if role in {"user", "assistant"} else f"[событие] {text}"
        items.append({"role": api_role, "content": content})
    return items


def tool_definitions(enable_web_search: bool) -> list[dict]:
    tools: list[dict] = []
    if enable_web_search:
        tools.append({"type": "web_search"})
    tools.append(
        {
            "type": "function",
            "name": "append_memory",
            "description": "Добавить одну короткую строку в заметку текущего собеседника. Не удаляет и не меняет чужие заметки.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Один факт, без паролей и платёжных данных."}
                },
                "required": ["text"],
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "name": "propose_action",
            "description": (
                "Запросить одно подтверждение владельца. Ничего не покупает, не отправляет и не удаляет. "
                "Для memory_replace передай полный новый текст заметки."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(SENSITIVE_KINDS)},
                    "summary": {"type": "string", "description": "Что именно подтверждается, одной фразой."},
                    "target_phone": {
                        "type": "string",
                        "description": "Чья заметка. Пусто значит текущий собеседник. Нужно для memory_replace.",
                    },
                    "new_body": {
                        "type": "string",
                        "description": "Полный новый текст заметки. Только для memory_replace.",
                    },
                },
                "required": ["kind", "summary"],
            },
        }
    )
    return tools
