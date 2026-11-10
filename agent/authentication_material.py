"""Exclude authentication values independently of speaker origin and known secret keys."""

import re

# Context is essential: ordinary counts, dates and identifiers are not authentication values.
_LABEL = (r"(?:(?:recovery|backup|verification|authentication|one[ -]time|login|sign[ -]in|2fa)\s+"
          r"(?:code|token|password)s?|otp|password|passphrase|api[ _-]?key|secret[ _-]?key)")
_VALUE = re.compile(r"(?P<label>\b" + _LABEL + r"\s*(?:is\s+|are\s+|[:=]\s*))"
                    r"(?P<value>`?[A-Za-z0-9][A-Za-z0-9_+/-]{3,127}`?)", re.IGNORECASE)
_AUTH = re.compile(r"\b" + _LABEL + r"\s*(?:[:=]|\bis\b|\bare\b|\s+[`'\"]?\d{4,})", re.IGNORECASE)
_SENTENCE = re.compile(r".+?(?:[.!?](?=\s|$)|\n|$)")


def authentication_values(text):
    # Natural-language status words are ambiguous, not values to erase elsewhere.
    return tuple(match.group("value").strip("`") for match in _VALUE.finditer(text or "")
                 if any(char.isdigit() or char in "`_+/-" for char in match.group("value"))
                 or ":" in match.group("label") or "=" in match.group("label"))


def exclude_authentication_material(text):
    """Precise values are excluded; ambiguous authentication-bearing sentences stay held.

    No values or value-derived identifiers enter evidence records. Neighboring sentences and
    speaker roles stay intact. This does not attempt to classify human authorship.
    """
    def hold(match):
        sentence = match.group()
        if not _AUTH.search(sentence):
            return sentence
        value = _VALUE.search(sentence)
        # A partial token, quoted phrase or list is ambiguous. Withhold that sentence
        # rather than certifying the first token while leaking the remainder.
        if value is None or sentence[value.end():].strip(" .!?\n"):
            return " [authentication span held]" + ("\n" if sentence.endswith("\n") else "")
        return _VALUE.sub(lambda m: m.group("label") + "[authentication withheld]", sentence)
    return _SENTENCE.sub(hold, text or "")
