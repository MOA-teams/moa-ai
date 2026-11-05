"""Keep service errors (E-502) distinct from reported HTTP statuses (HTTP-502)."""

import re
from http import HTTPStatus

SERVICE_CODE = re.compile(
    r"(?<![A-Za-z0-9])E(?:[\s-]+([A-Za-z]+)[\s-]+|[\s-]*)(\d{2,3})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
HTTP_LABEL = re.compile(
    r"(?<![A-Za-z0-9_-])(?:HTTP(?:/\d(?:\.\d)?)?(?:[\s_-]*(?:상태|status))?"
    r"(?:[\s_-]*(?:코드|code))?"
    r"|(?:상태|오류|에러)\s*(?:코드)?|(?:status|error)\s*(?:code)?)"
    r"(?:가|는|은|이)?[\s:=_-]*([1-5]\d{2})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
NUMBER = re.compile(r"(?<![\w-])([1-5]\d{2})(?![A-Za-z0-9_-])")
ERROR_SUFFIX = re.compile(
    r"^\s*(?:(?:번\s*)?(?:에러|오류|error)|"
    r"(?:가|이)?\s*(?:떠|뜹|떴|뜬|뜸|발생|표시|나오|나옵|나와|보여|보이))",
    re.IGNORECASE,
)
CUSTOM_CODE = re.compile(
    r"(?<![A-Za-z0-9_-])([A-Za-z]{2,16}(?:[-_][A-Za-z]{1,16})*[-_]\d{2,6})"
    r"(?![A-Za-z0-9_-])"
)
HTTP_PHRASE = re.compile(
    r"^\s+(?:" + "|".join(re.escape(status.phrase) for status in HTTPStatus) + r")(?![A-Za-z])",
    re.I,
)


def service_code(match: re.Match) -> str:
    area, digits = match.groups()
    return f"E-{area.upper()}-{digits}" if area else f"E-{digits}"


def extract_error_codes(text: str, *, explicit_field: bool = False) -> set[str]:
    codes = {service_code(match) for match in SERVICE_CODE.finditer(text)}
    codes.update(f"HTTP-{match.group(1)}" for match in HTTP_LABEL.finditer(text))
    # Keep unfamiliar service codes only when the customer explicitly presents
    # the token as an error. Never infer their meaning or map them to a known code.
    for match in CUSTOM_CODE.finditer(text):
        if ERROR_SUFFIX.match(text[match.end() :]):
            codes.add(match.group(1).upper())
    for match in NUMBER.finditer(text):
        number = match.group(1)
        suffix = text[match.end() :]
        # Users may pair 502 with 'Internal Server Error' (normally 500).
        # Preserve the reported NUMBER; the phrase is context, not a correction.
        if ERROR_SUFFIX.match(suffix) or HTTP_PHRASE.match(suffix):
            codes.add(f"HTTP-{number}")
    if explicit_field:
        reply = re.fullmatch(r"([1-5]\d{2})(?:이?요|입니다|예요)?[.!]?", text.strip())
        if reply:
            codes.add(f"HTTP-{reply[1]}")
    return codes


def resolve_reported_code(value: str, observed: set[str]) -> str | None:
    """Normalize model spelling only when the corresponding code was actually reported."""
    text = value.strip()
    if text.upper() in observed:
        return text.upper()
    service = SERVICE_CODE.fullmatch(text)
    http = HTTP_LABEL.fullmatch(text)
    if service:
        candidate = service_code(service)
    elif http:
        candidate = f"HTTP-{http.group(1)}"
    elif re.fullmatch(r"[1-5]\d{2}", text):
        candidate = f"HTTP-{text}"
    else:
        return None
    if candidate in observed:
        return candidate
    # A model may prepend E to a real numeric HTTP status (the production failure).
    # Repair only the namespace of an identical, source-grounded 3-digit value.
    match = re.fullmatch(r"(?:E|HTTP)-([1-5]\d{2})", candidate)
    if match:
        alternatives = {f"E-{match[1]}", f"HTTP-{match[1]}"} & observed
        if len(alternatives) == 1:
            return alternatives.pop()
    return None
