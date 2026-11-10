"""Exclude authentication values independently of speaker origin and known secret keys."""

import re

# Context is essential: ordinary counts, dates and identifiers are not authentication values.
_LABEL = r"(?:recovery|backup|verification|authentication|one[ -]time|login|sign[ -]in|2fa|otp)\s+(?:code|token|password)s?"
_VALUE = re.compile(r"(?P<label>\b" + _LABEL + r"\s*(?:is\s+|are\s+|[:=]\s*))"
                    r"(?P<value>`?[A-Za-z0-9][A-Za-z0-9_+/-]{3,127}`?)", re.IGNORECASE)
_AUTH = re.compile(r"\b" + _LABEL + r"\b", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def authentication_values(text):
    return tuple(match.group("value").strip("`") for match in _VALUE.finditer(text or ""))


def exclude_authentication_material(text):
    """Precise values are excluded; ambiguous authentication-bearing sentences stay held.

    No values or value-derived identifiers enter evidence records. Neighboring sentences and
    speaker roles stay intact. This does not attempt to classify human authorship.
    """
    masked = _VALUE.sub(lambda m: m.group("label") + "[authentication withheld]", text or "")
    def hold(match):
        sentence = match.group()
        if _AUTH.search(sentence) and "[authentication withheld]" not in sentence:
            return " [authentication span held]" + ("\n" if sentence.endswith("\n") else "")
        return sentence
    return _SENTENCE.sub(hold, masked)
