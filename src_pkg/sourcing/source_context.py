"""Conservative source-profile context extraction.

Provider lookup code needs the same interpretation of captured profile notes.
Only explicit labels and two stable resume-style layouts are accepted here;
arbitrary prose never becomes an employer, school, alias, or relative.

The returned history is optional matching/ranking context.  Older jobs and
schools can corroborate an identity, but callers must not treat a stale item as
a conflict with a provider's current record.
"""
from __future__ import annotations

import re

from . import person_name


COMPANY_LIMIT = 8
SCHOOL_LIMIT = 6
ROLE_LIMIT = 8
ALIAS_LIMIT = 8
RELATIVE_LIMIT = 8

_DATE_LINE_RE = re.compile(
    r"^\(?\s*"
    r"(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?|spring|summer|fall|autumn|winter)\s+)?"
    r"(?:19|20)\d{2}"
    r"(?:\s*(?:-|\N{EN DASH}|\N{EM DASH}|to)\s*"
    r"(?:(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?|spring|summer|fall|autumn|winter)\s+)?(?:19|20)\d{2}"
    r"|present|current|now))?"
    r"\s*\)?(?:\s*(?:\N{MIDDLE DOT}|\|)\s*[^\r\n]+|\s*\([^\r\n)]*\))?$",
    re.IGNORECASE,
)
_DEGREE_RE = re.compile(
    r"\b(?:"
    r"associate(?:'s)?(?:\s+(?:of|in)\b|\s+degree\b)|"
    r"bachelor(?:'s)?(?:\s+(?:of|in)\b|\s+degree\b)|"
    r"master(?:'s)?(?:\s+(?:of|in)\b|\s+degree\b)|"
    r"doctor(?:ate|al)?(?:\s+(?:of|in)\b|\s+degree\b)?|"
    r"ph\.?\s*d\.?|diploma|degree|"
    r"a\.?a\.?s\.?|a\.?s\.?n\.?|a\.?d\.?n\.?|"
    r"b\.?s\.?n\.?|m\.?s\.?n\.?|d\.?n\.?p\.?|m\.?b\.?a\.?)\b",
    re.IGNORECASE,
)
_SCHOOL_HINT_RE = re.compile(
    r"\b(?:academy|college|conservatory|institute|polytechnic|school|university)\b",
    re.IGNORECASE,
)
_SECTION_HEADINGS = {
    "about", "certifications", "education", "experience", "licenses",
    "licenses certifications", "profile", "resume", "skills", "summary",
    "volunteering", "work experience",
}
_NON_CONTEXT_PREFIXES = (
    "bio:", "facebook profile:", "facebook hometown:", "from:",
    "hometown:", "linkedin profile:", "location:", "professional descriptor:",
    "source display name:", "source professional descriptor:", "visible detail:",
)


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _key(value) -> str:
    return _clean(value).casefold()


def _unique(values, limit: int) -> list[str]:
    output, seen = [], set()
    for value in values or []:
        cleaned = _clean(value)
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
        if len(output) >= limit:
            break
    return output


def _list_values(value) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [
        _clean(item) for item in values
        if not isinstance(item, bool) and isinstance(item, (str, int, float))
        and _clean(item)
    ]


def _date_line(value: str) -> bool:
    return bool(_DATE_LINE_RE.fullmatch(_clean(value)))


def _degree_line(value: str) -> bool:
    return bool(_DEGREE_RE.search(_clean(value)))


def _strip_prefix(value: str, prefixes: tuple[str, ...]) -> str:
    cleaned = _clean(value)
    lowered = cleaned.casefold()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return _clean(cleaned.split(":", 1)[1] if ":" in cleaned else "")
    return cleaned


def _excluded(candidate: dict) -> set[str]:
    return {
        " ".join(person_name.identity_tokens(candidate.get("name") or "")),
        _key(candidate.get("location") or ""),
        _key(candidate.get("hometown") or ""),
    } - {""}


def _usable_context_line(value: str, excluded: set[str]) -> bool:
    cleaned = _clean(value)
    normalized = _key(cleaned).rstrip(":")
    if not cleaned or len(cleaned) > 240 or normalized in excluded:
        return False
    if normalized in _SECTION_HEADINGS or _date_line(cleaned):
        return False
    if any(normalized.startswith(prefix) for prefix in _NON_CONTEXT_PREFIXES):
        return False
    if re.search(r"(?:https?://|www\.|@)", cleaned, re.IGNORECASE):
        return False
    return bool(re.search(r"[A-Za-z]", cleaned))


def _split_role_company(value: str) -> tuple[str, str]:
    """Split a structured headline such as ``Nurse at Example Health``."""
    cleaned = _clean(value)
    parts = re.split(r"\s+at\s+", cleaned, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return cleaned, ""
    role, company = (_clean(part) for part in parts)
    if not role or not company:
        return cleaned, ""
    if role.casefold() in {"employed", "currently works", "worked", "works"}:
        role = ""
    return role, company


def _headline_like(value: str) -> bool:
    cleaned = _clean(value)
    if not cleaned or len(cleaned) > 160 or re.search(r"[.!?]$", cleaned):
        return False
    return not bool(re.search(
        r"\b(?:i|i'm|my|passionate|seeking|looking for|open to)\b",
        cleaned, re.IGNORECASE,
    ))


def extract(candidate: dict | None) -> dict[str, list[str]]:
    """Return bounded source facts suitable for optional provider context.

    Explicit fields win ordering.  Resume-style inferred history follows it so
    current, deliberately labelled facts remain the provider's first options.
    """
    candidate = candidate or {}
    lines = [_clean(line) for line in str(candidate.get("notes") or "").splitlines()]
    lines = [line for line in lines if line]
    excluded = _excluded(candidate)

    companies = [
        *_list_values(candidate.get("companies")),
        *_list_values(candidate.get("employers")),
    ]
    schools = _list_values(candidate.get("schools"))
    roles = _list_values(candidate.get("roles"))
    relatives = _list_values(candidate.get("relatives"))

    company_prefixes = ("employer:", "company:")
    role_prefixes = ("role:", "headline:", "job title:", "profession:")
    for line in lines:
        lowered = line.casefold()
        if lowered.startswith(company_prefixes):
            value = _strip_prefix(line, company_prefixes)
            if _usable_context_line(value, excluded):
                companies.append(value)
        elif lowered.startswith("school:"):
            value = _strip_prefix(line, ("school:",))
            if _usable_context_line(value, excluded):
                schools.append(value)
        elif lowered.startswith(role_prefixes):
            value = _strip_prefix(line, role_prefixes)
            role, company = _split_role_company(value)
            if _usable_context_line(role, excluded):
                roles.append(role)
            if company and _usable_context_line(company, excluded):
                companies.append(company)
        elif lowered.startswith("relative:"):
            value = person_name.normalize_person_name(
                _strip_prefix(line, ("relative:",))
            )
            if len(person_name.identity_tokens(value)) >= 2:
                relatives.append(value)

    # Indeed resume text consistently presents role, company, then a bounded
    # date range.  Require the complete three-line shape; a loose neighboring
    # word is never promoted to an organization.
    for index, line in enumerate(lines):
        if index < 2 or not _date_line(line):
            continue
        role_value = _strip_prefix(lines[index - 2], role_prefixes)
        company_value = _strip_prefix(lines[index - 1], company_prefixes)
        if (
            _usable_context_line(role_value, excluded)
            and _usable_context_line(company_value, excluded)
            and not _degree_line(role_value)
            and not _degree_line(company_value)
        ):
            roles.append(role_value)
            companies.append(company_value)

    # Indeed commonly emits degree then school.  Also support LinkedIn's
    # school-then-degree order, but only when the preceding text names an
    # educational institution explicitly.
    for index, line in enumerate(lines):
        if not _degree_line(line):
            continue
        if index + 1 < len(lines):
            school = _strip_prefix(lines[index + 1], ("school:",))
            if (
                _usable_context_line(school, excluded)
                and not _date_line(school) and not _degree_line(school)
            ):
                schools.append(school)
        if index:
            school = _strip_prefix(lines[index - 1], ("school:",))
            if (
                _SCHOOL_HINT_RE.search(school)
                and _usable_context_line(school, excluded)
                and not _date_line(school) and not _degree_line(school)
            ):
                schools.append(school)

    # The import boundary currently retains Indeed's separate headline as the
    # first notes line.  Use it only as a last-resort role and only for Indeed;
    # names, locations, URLs, sections, dates, and degree lines are excluded.
    if not roles and str(candidate.get("source") or "").casefold() == "indeed" and lines:
        first = lines[0]
        if (
            _headline_like(first) and _usable_context_line(first, excluded)
            and not _degree_line(first)
        ):
            role, company = _split_role_company(first)
            if _usable_context_line(role, excluded):
                roles.append(role)
            if company and _usable_context_line(company, excluded):
                companies.append(company)

    alias_values = [
        *_list_values(candidate.get("aliases")),
        *person_name.source_alternate_names(candidate.get("notes") or ""),
    ]
    aliases = []
    for value in alias_values:
        normalized = person_name.normalize_person_name(value)
        if len(person_name.identity_tokens(normalized)) >= 2:
            aliases.append(normalized)
    normalized_relatives = []
    for value in relatives:
        normalized = person_name.normalize_person_name(value)
        if len(person_name.identity_tokens(normalized)) >= 2:
            normalized_relatives.append(normalized)

    return {
        "companies": _unique(companies, COMPANY_LIMIT),
        "schools": _unique(schools, SCHOOL_LIMIT),
        "roles": _unique(roles, ROLE_LIMIT),
        "aliases": _unique(aliases, ALIAS_LIMIT),
        "relatives": _unique(normalized_relatives, RELATIVE_LIMIT),
    }
