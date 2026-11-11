"""Exclude authentication values independently of speaker origin and known secret keys."""

import re

# Context is essential: ordinary counts, dates and identifiers are not authentication values.
_LABEL = (r"(?:(?:recovery|backup|verification|authentication|one[ -]time|login|sign[ -]in|2fa)\s+"
          r"(?:code|token|password)s?|otp|password|passphrase|api[ _-]?key|secret[ _-]?key)")
_VALUE = re.compile(
    r"(?P<label>\b" + _LABEL + r"(?:[ \t]*(?:is|are)[ \t]+|[ \t]*[:=][ \t]*|[ \t]+(?=[`'\"]?\d{4,})))"
    r"(?P<value>`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|\S+(?:(?<=[,;])\s+\d{4,}[.,;]?)*)", re.IGNORECASE)
_SENTENCE = re.compile(r".+?(?:[.!?](?=\s|$)|\n|$)")
_STATUS_WORDS = frozenset({"wrong", "required", "none", "missing", "needed", "unset", "unavailable",
                           "incorrect", "invalid", "expired", "enabled", "disabled", "optional",
                           "correct", "set", "stored", "configured", "available", "secure", "insecure",
                           "safe", "unsafe", "strong", "weak", "long", "short",
                           "a", "an", "the", "not", "in", "on", "to", "i", "we", "you", "it",
                           "my", "your", "this", "that", "same", "still", "there"})


def credential_shaped(value: str) -> bool:
    """Unassigned prose needs a long, mixed-case/type token or a long numeric code."""
    value = value.rstrip(".!?")
    classes = (any(c.islower() for c in value), any(c.isupper() for c in value),
               any(c.isdigit() for c in value), any(not c.isalnum() for c in value))
    return len(value) >= 8 and (sum(classes) >= 2 or value.isdecimal())


def _values(text):
    """The same value grammar decides what is held and what later repeats must hide."""
    for match in _VALUE.finditer(text or ""):
        raw = match.group("value").rstrip(".,;")
        items = re.split(r"[,;]\s*", raw) if re.fullmatch(r"\d{4,}(?:[,;]\s*\d{4,})+", raw) else [raw]
        for item in items:
            value = item.strip().strip("`'\"")
            quoted = raw[:1] in "`'\""
            # Label-adjacent numeric codes ("OTP 482913") are an existing explicit
            # code form. Natural-language is/are needs stronger value evidence.
            assigned = re.search(r"(?:is|are)[ \t]+$", match.group("label"), re.IGNORECASE) is None
            if (value and (quoted or value.lower().rstrip(".!?") not in _STATUS_WORDS)
                    and (quoted or assigned or credential_shaped(value))):
                yield match, value


def authentication_values(text):
    return tuple(value for _, value in _values(text))


def exclude_authentication_material(text):
    """Precise values are excluded; ambiguous authentication-bearing sentences stay held.

    No values or value-derived identifiers enter evidence records. Neighboring sentences and
    speaker roles stay intact. This does not attempt to classify human authorship.
    """
    def hold(sentence):
        matches = list(_values(sentence))
        if not matches:
            return sentence
        value = matches[0][0]
        # A partial token, quoted phrase or list is ambiguous. Withhold that sentence
        # rather than certifying the first token while leaking the remainder.
        if sentence[value.end():].strip(" .!?\n"):
            return " [authentication span held]" + ("\n" if sentence.endswith("\n") else "")
        for value in reversed(list({match.start(): match for match, _ in matches}.values())):
            sentence = sentence[:value.start("value")] + "[authentication withheld]" + sentence[value.end():]
        return sentence
    text = text or ""
    boundaries = list(text)
    # Quoted values may contain sentence punctuation. Segment outside their full span,
    # otherwise the held prefix could leave the rest of a password in clear text.
    for match, _ in _values(text):
        if match.group("value")[:1] in "`\"'":
            for index in range(*match.span("value")):
                if boundaries[index] in ".!?":
                    boundaries[index] = "\0"
    return _SENTENCE.sub(lambda match: hold(text[match.start():match.end()]), "".join(boundaries))
