"""
Helpers for extracting structured JSON payloads from LLM text responses.
"""
import json
import re


def parse_json_response(text: str) -> dict:
    """
    Parse a JSON object from an LLM response.

    Handles common response shapes:
    - raw JSON object
    - fenced ```json blocks
    - short prose preamble before the JSON block
    """
    candidates: list[str] = []
    clean = text.strip()

    if clean:
        candidates.append(clean)

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", clean, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        fenced_body = fenced.group(1).strip()
        if fenced_body:
            candidates.append(fenced_body)

    for candidate in list(candidates):
        extracted = _extract_first_json_object(candidate)
        if extracted:
            candidates.append(extracted)

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    raise ValueError("No JSON object could be parsed from the LLM response")


def _extract_first_json_object(text: str) -> str | None:
    """Return the first balanced JSON object found in the text."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]

    return None
