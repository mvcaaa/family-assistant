def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def mask_phone(value: str) -> str:
    digits = normalize_phone(value)
    if len(digits) < 4:
        return "****"
    return f"…{digits[-4:]}"
