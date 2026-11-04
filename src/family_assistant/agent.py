from family_assistant.gate import Gate, dispatch_tool
from family_assistant.prompts import history_input, tool_definitions


def split_output(data: dict) -> tuple[list[dict], str]:
    calls: list[dict] = []
    texts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            name = item.get("name")
            arguments = item.get("arguments")
            function = item.get("function")
            if isinstance(function, dict):
                name = name or function.get("name")
                if arguments is None:
                    arguments = function.get("arguments")
            call_id = item.get("call_id") or item.get("id")
            if name and call_id:
                calls.append({"name": name, "arguments": arguments or "{}", "call_id": call_id})
        elif item.get("type") == "message":
            text = _message_text(item)
            if text:
                texts.append(text)
    return calls, "\n".join(texts).strip()


def _message_text(item: dict) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts).strip()


def run_turn(xai, gate: Gate, instructions: str, enable_web_search: bool) -> str:
    tools = tool_definitions(enable_web_search)
    conversation: list[dict] = [
        {"role": "system", "content": instructions},
        *history_input(gate.history()),
    ]
    final = ""
    previous: str | None = None
    outputs: list[dict] = []
    for _ in range(5):
        payload: dict = {"model": xai.model, "input": conversation, "tools": tools}
        if previous is not None:
            payload["previous_response_id"] = previous
            payload["input"] = outputs
        data = xai.respond(payload)
        if isinstance(data.get("id"), str):
            previous = data["id"]
        calls, text = split_output(data)
        if text:
            final = text
        if not calls:
            return final
        outputs = [
            {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": dispatch_tool(gate, call["name"], call["arguments"]),
            }
            for call in calls
        ]
        if previous is None:
            conversation = [*conversation, *outputs]
    return final
