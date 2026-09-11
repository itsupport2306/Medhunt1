"""Conservative normalization for names captured from recruiting platforms.

LinkedIn frequently appends professional credentials to the visible member
name.  Provider APIs need the person's legal/display name, not those
credentials (for example, ``Silvia Lopez-Clarke, CST, BSN, RN, CNOR``).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# Keep this list deliberately explicit.  A token is removed only when it is a
# recognized professional/academic credential at the end of a captured name.
_CREDENTIALS = {
    "aa", "aas", "adn", "anp", "aprn", "as", "asn",
    "ba", "bba", "bfa", "bhs", "bs", "bsn",
    "ccrn", "cma", "cnm", "cnor", "cns", "cpa", "cphq", "cst",
    "dnp", "do", "dpt", "edd", "emt",
    "fnp", "fnpbc", "fnp-c", "jd", "lcsw", "lmft", "lpn", "lcsw",
    "ma", "mba", "md", "mha", "mph", "ms", "msn", "msw",
    "np", "nrcma", "ot", "otr", "pa", "pac", "pharmd", "phd", "phn",
    "pccn", "pmhnp", "pmhnpbc", "pt", "rd", "rma", "rn", "rnc", "rnbc",
    "rnfa", "rt", "rtt", "shrmcp", "shrmscp",
}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
_SURNAME_PARTICLES = {
    "da", "de", "del", "della", "der", "di", "du", "la", "le", "van", "von",
}
_CONNECTION_MARKER_RE = re.compile(
    r"\s*[\u00b7\u2022]\s*(?:1st|2nd|3rd\+?|out\s+of\s+network)\b.*$",
    re.IGNORECASE,
)

# These are deliberately healthcare-specific, high-confidence descriptors.
# Generic words such as "manager" are not stripped from an unpunctuated name.
_ROLE_PHRASE = (
    r"(?:licensed\s+(?:registered|practical|vocational)\s+nurse|"
    r"registered\s+nurse|practical\s+nurse|vocational\s+nurse|"
    r"nurse\s+practitioner|family\s+nurse\s+practitioner|"
    r"certified\s+(?:registered\s+)?nurse\s+anesthetist|"
    r"certified\s+nursing\s+assistant|nursing\s+assistant|"
    r"clinical\s+nurse\s+specialist|staff\s+nurse|charge\s+nurse|"
    r"travel\s+nurse|school\s+nurse|home\s+health\s+nurse|"
    r"registered\s+nursing|nursing|nurse|"
    r"infirmi[eè]re(?:\s+auxiliaire)?|infirmier(?:\s+auxiliaire)?)"
)
_ROLE_CHAIN_RE = re.compile(
    rf"(?:\s+|,\s*|\|\s*|[\u2013\u2014]\s*)"
    rf"(?P<roles>{_ROLE_PHRASE}(?:\s*[-/|,&]\s*{_ROLE_PHRASE})*)\s*$",
    re.IGNORECASE,
)
_ALTERNATE_NAME_PREFIXES = ("alternate name:", "source alternate name:")


@dataclass(frozen=True)
class ParsedPersonName:
    name: str
    descriptors: tuple[str, ...] = ()
    alternate_names: tuple[str, ...] = ()


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", str(value or "").casefold())


def _is_credential_chunk(value: str) -> bool:
    tokens = [token for token in re.split(r"[\s/&]+", str(value or "").strip()) if token]
    return bool(tokens) and all(_key(token) in _CREDENTIALS for token in tokens)


def _is_role_chunk(value: str) -> bool:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" ,")
    if not cleaned:
        return False
    return bool(re.fullmatch(rf"{_ROLE_PHRASE}(?:\s*[-/|,&]\s*{_ROLE_PHRASE})*", cleaned, re.IGNORECASE))


def _words(value: str) -> list[str]:
    return re.findall(r"[^\W\d_][^\W_]*(?:['\u2019-][^\W\d_][^\W_]*)*", value, re.UNICODE)


def _unique(values: list[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" ,")
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            output.append(cleaned)
    return tuple(output)


def parse_person_name(value: str) -> ParsedPersonName:
    """Separate a provider-safe identity from captured titles and aliases."""
    raw = re.sub(r"\s+", " ", str(value or "")).strip(" ,")
    raw = _CONNECTION_MARKER_RE.sub("", raw).strip(" ,")
    if not raw:
        return ParsedPersonName("")

    descriptors: list[str] = []
    alternate_names: list[str] = []

    # A nested Facebook node may contain only ``(Rhonda Hampton)``. Unwrap a
    # plausible person, but never turn ``(Registered Nurse)`` into an identity.
    parenthetical_only = re.fullmatch(r"\(([^()]{1,120})\)", raw)
    if parenthetical_only:
        inside = parenthetical_only.group(1).strip()
        if _is_role_chunk(inside) or _is_credential_chunk(inside):
            return ParsedPersonName("", (inside,), ())
        raw = inside
    else:
        parenthetical = re.fullmatch(r"(.*?)\s*\(([^()]{1,120})\)", raw)
        if parenthetical and _words(parenthetical.group(1)):
            base = parenthetical.group(1).strip(" ,")
            inside = parenthetical.group(2).strip()
            raw = base
            if _is_role_chunk(inside) or _is_credential_chunk(inside):
                descriptors.append(inside)
            elif re.sub(r"\W+", "", inside.casefold()) != re.sub(r"\W+", "", base.casefold()):
                alternate_names.append(inside)

    comma_parts = [part.strip() for part in raw.split(",")]
    if len(comma_parts) > 1:
        kept = [comma_parts[0]]
        for part in comma_parts[1:]:
            if not part:
                continue
            if _is_credential_chunk(part) or _is_role_chunk(part):
                descriptors.append(part)
            else:
                kept.append(part)
        raw = " ".join(kept)

    # Explicit multiword healthcare roles are descriptors. The ambiguous
    # single word "Nurse" is stripped only when a full two-part name remains.
    while True:
        match = _ROLE_CHAIN_RE.search(raw)
        if not match:
            break
        base = raw[:match.start()].strip(" ,|\u2013\u2014-")
        role = match.group("roles").strip()
        base_words = _words(base)
        unambiguous = role.casefold() not in {"nurse", "nursing"}
        if len(base_words) < 2 and not unambiguous:
            break
        descriptors.append(role)
        raw = base

    # Also support the no-comma credential form ("Jane Doe BSN RN").
    tokens = raw.split()
    while len(tokens) > 1 and _is_credential_chunk(tokens[-1]):
        descriptors.insert(0, tokens.pop())
    raw = " ".join(tokens)

    output = re.sub(r"\s+", " ", raw).strip(" ,")
    return ParsedPersonName(
        output,
        _unique(descriptors),
        _unique(alternate_names),
    )


def normalize_person_name(value: str) -> str:
    """Return a provider-safe name while preserving surnames and name suffixes."""
    return parse_person_name(value).name


def identity_tokens(value: str) -> tuple[str, ...]:
    """Return accent-folded, token-boundary identity components.

    Provider records do not consistently preserve diacritics.  Folding only
    for comparison lets ``Garcia`` corroborate ``García`` without weakening
    first/surname matching into unsafe substring checks.
    """
    normalized = unicodedata.normalize("NFKD", normalize_person_name(value))
    folded = "".join(char for char in normalized if not unicodedata.combining(char))
    return tuple(re.findall(r"[a-z0-9]+", folded.casefold()))


def identity_signature(value: str) -> dict:
    """Return structured first/surname/suffix evidence for identity matching.

    Ordinary middle words remain outside the surname. Hyphenated surnames and
    common surname particles are retained as a terminal surname group, so
    ``Silvia Lopez-Clarke`` cannot collapse into an unsafe ``Silvia Clarke``
    match while ``Jane Marie Doe`` can still match ``Jane Q Doe``.
    """
    normalized = normalize_person_name(value)
    chunks = [chunk for chunk in normalized.split() if chunk]
    suffix = ""
    if chunks and is_name_suffix(chunks[-1]):
        suffix = _key(chunks.pop())
    if len(chunks) < 2:
        tokens = identity_tokens(normalized)
        return {
            "first": tokens[0] if tokens else "", "surname": (),
            "middle": (), "suffix": suffix, "tokens": tokens,
        }

    first_tokens = identity_tokens(chunks[0])
    surname_start = len(chunks) - 1
    while surname_start > 1:
        previous = identity_tokens(chunks[surname_start - 1])
        if len(previous) != 1 or previous[0] not in _SURNAME_PARTICLES:
            break
        surname_start -= 1
    surname = tuple(
        token for chunk in chunks[surname_start:] for token in identity_tokens(chunk)
    )
    middle = tuple(
        token for chunk in chunks[1:surname_start] for token in identity_tokens(chunk)
    )
    all_tokens = tuple(
        token for chunk in chunks for token in identity_tokens(chunk)
    )
    return {
        "first": first_tokens[0] if first_tokens else "",
        "surname": surname, "middle": middle, "suffix": suffix,
        "tokens": all_tokens,
    }


def source_alternate_names(notes: str) -> tuple[str, ...]:
    """Read full alternate names emitted by any supported source adapter.

    Imported platform profiles use ``Source alternate name:`` while older
    adapters/tests used ``Alternate name:``. Keep the compatibility in this
    single helper so lookup, verification, and cache-key code cannot drift.
    One-word nicknames remain display context, not identity/query anchors.
    """
    values: list[str] = []
    for line in str(notes or "").splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        lowered = cleaned.casefold()
        if not any(lowered.startswith(prefix) for prefix in _ALTERNATE_NAME_PREFIXES):
            continue
        alternate = normalize_person_name(cleaned.split(":", 1)[1].strip())
        if len(_words(alternate)) >= 2:
            values.append(alternate)
    return _unique(values)


def is_name_suffix(value: str) -> bool:
    return _key(value) in _SUFFIXES
