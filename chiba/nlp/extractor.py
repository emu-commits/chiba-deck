import re
from dataclasses import dataclass, field


@dataclass
class Entities:
    intent: str = ""
    query: str = ""
    amount: float | None = None
    target_node: str | None = None
    item: str | None = None
    raw: str = ""


def extract(intent: str, text: str) -> Entities:
    text = text.strip()
    e = Entities(intent=intent, raw=text)

    if intent == "send_payment":
        m = re.search(r'\$?(\d+(?:\.\d{1,2})?)', text)
        if m:
            e.amount = float(m.group(1))
        m = re.search(r'\bto\s+(\w+)', text, re.IGNORECASE)
        if m:
            e.target_node = m.group(1)

    elif intent == "query_wiki":
        m = re.search(
            r'(?:about|lookup|look up|what is|what are|tell me about|'
            r'search for|find|define|explain|wiki)\s+(.+)',
            text, re.IGNORECASE
        )
        if m:
            e.query = m.group(1).strip()
        elif len(text.split()) <= 3:
            e.query = text
        else:
            e.query = text

    elif intent == "buy_item":
        m = re.search(r'(?:buy|purchase|order|take)\s+(.+)', text, re.IGNORECASE)
        if m:
            e.item = m.group(1).strip()
        else:
            e.item = text

    elif intent == "ping_node":
        m = re.search(r'(?:ping|reach|check)\s+(!?\w+)', text, re.IGNORECASE)
        if m:
            e.target_node = m.group(1)

    elif intent == "send_dm":
        # Capture everything after the verb and optional "to" as a single string.
        # The router resolves handle vs message via greedy DB lookup, handles spaces in names.
        m = re.search(
            r'(?:dm|text|message|msg|tell)\s+(?:to\s+)?(.+)'
            r'|(?:send(?:\s+a)?(?:\s+(?:dm|message|msg|note|private\s+message))?'
            r'|private(?:\s+message)?|direct(?:\s+message)?|write(?:\s+to)?|reach\s+out\s+to)'
            r'\s+(?:to\s+)?(.+)',
            text, re.IGNORECASE
        )
        if m:
            e.target_node = (m.group(1) or m.group(2) or "").strip()
        else:
            e.target_node = text
        e.query = ""

    elif intent == "send_chat":
        # Strip leading verb/noun prefix, keep the actual message
        m = re.search(
            r'(?:say|broadcast|post)\s+(.+)'
            r'|(?:send|tell)\s+(?:(?:a\s+)?(?:message|msg|everyone|the\s+mesh|mesh|channel|to\s+channel)\s+)?(.+)',
            text, re.IGNORECASE
        )
        if m:
            e.query = (m.group(1) or m.group(2) or "").strip()
        else:
            e.query = text

    else:
        e.query = text

    return e
