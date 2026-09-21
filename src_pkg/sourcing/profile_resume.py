"""Render a transparent, source-attributed professional profile PDF.

These documents summarize public directory facts and are deliberately labeled
as generated profiles. They are not represented as candidate-authored resumes.
"""
from __future__ import annotations

import hashlib
import json
import re
from html import escape
from io import BytesIO


def _text(value, limit: int = 4000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _values(value, *, limit: int = 50, max_chars: int = 500) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        cleaned = _text(item, max_chars)
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
        if len(output) >= limit:
            break
    return output


def normalized_profile(candidate: dict, profile: dict) -> dict:
    """Return the bounded data contract consumed by the PDF renderer."""
    return {
        "name": _text(candidate.get("name"), 200) or "Healthcare Professional",
        "location": _text(profile.get("location") or candidate.get("location"), 300),
        "headline": _text(profile.get("headline"), 300),
        "source_label": _text(profile.get("source_label"), 100) or "Public professional directory",
        "source_url": _text(profile.get("source_url"), 2000),
        "summary": _text(profile.get("summary"), 4000),
        "credentials": _values(profile.get("credentials"), limit=12, max_chars=40),
        "specialties": _values(profile.get("specialties"), limit=20),
        "subspecialties": _values(profile.get("subspecialties"), limit=20),
        "hospitals": _values(profile.get("hospitals"), limit=40),
        "education": _values(profile.get("education"), limit=40),
        "certifications": _values(profile.get("certifications"), limit=40),
        "licenses": _values(profile.get("licenses"), limit=75),
        "languages": _values(profile.get("languages"), limit=20, max_chars=120),
        "years_experience": _text(profile.get("years_experience"), 60),
        "npi": re.sub(r"\D", "", str(profile.get("npi") or ""))[:10],
        "address": _text(profile.get("address"), 500),
    }


def fingerprint(candidate: dict, profile: dict) -> str:
    normalized = normalized_profile(candidate, profile)
    encoded = json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def filename(candidate: dict, profile: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _text(candidate.get("name"), 100).casefold()).strip("-")
    return f"{slug or 'professional'}-public-profile-{fingerprint(candidate, profile)[:12]}.pdf"


def render(candidate: dict, profile: dict) -> bytes:
    """Build a recruiter-readable PDF from a captured public profile."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        PageTemplate,
        Paragraph,
        Spacer,
    )

    data = normalized_profile(candidate, profile)
    buffer = BytesIO()
    width, height = letter

    styles = getSampleStyleSheet()
    navy = colors.HexColor("#102A43")
    teal = colors.HexColor("#0E9384")
    muted = colors.HexColor("#617580")
    rule = colors.HexColor("#D8E3E8")
    body = ParagraphStyle(
        "ProfileBody", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=10.2, leading=14.2, textColor=navy, spaceAfter=5,
    )
    item = ParagraphStyle(
        "ProfileItem", parent=body, leftIndent=11, firstLineIndent=-7,
        bulletIndent=0, spaceAfter=4,
    )
    section = ParagraphStyle(
        "ProfileSection", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=11.5, leading=14, textColor=teal, spaceBefore=10, spaceAfter=6,
        uppercase=True,
    )
    title = ParagraphStyle(
        "ProfileTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=24, leading=28, textColor=navy, alignment=TA_CENTER, spaceAfter=4,
    )
    subtitle = ParagraphStyle(
        "ProfileSubtitle", parent=body, fontSize=11, leading=14,
        textColor=muted, alignment=TA_CENTER, spaceAfter=2,
    )
    disclosure = ParagraphStyle(
        "Disclosure", parent=body, fontSize=8.5, leading=11,
        textColor=muted, borderColor=rule, borderWidth=0.6,
        borderPadding=8, backColor=colors.HexColor("#F5F9FA"), spaceBefore=13,
    )

    def page_canvas(*args, **kwargs):
        kwargs["invariant"] = 1
        return Canvas(*args, **kwargs)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(rule)
        canvas.line(0.65 * inch, 0.53 * inch, width - 0.65 * inch, 0.53 * inch)
        canvas.setFillColor(muted)
        canvas.setFont("Helvetica", 7.5)
        label = "Medhunt | Public professional profile"
        canvas.drawString(0.65 * inch, 0.35 * inch, label)
        page_label = f"Page {doc.page}"
        canvas.drawString(width - 0.65 * inch - stringWidth(page_label, "Helvetica", 7.5), 0.35 * inch, page_label)
        canvas.restoreState()

    document = BaseDocTemplate(
        buffer, pagesize=letter, rightMargin=0.7 * inch, leftMargin=0.7 * inch,
        topMargin=0.62 * inch, bottomMargin=0.72 * inch,
        title=f"{data['name']} - Public Professional Profile",
        author="Medhunt Public Profile Generator",
        subject="Source-attributed public professional profile",
    )
    document.addPageTemplates([PageTemplate(
        id="profile",
        frames=[Frame(document.leftMargin, document.bottomMargin, document.width, document.height, id="body")],
        onPage=footer,
    )])

    story = [Paragraph(escape(data["name"]), title)]
    credentials = ", ".join(data["credentials"])
    headline_parts = [part for part in (credentials, data["headline"], data["location"]) if part]
    if headline_parts:
        story.append(Paragraph(escape(" | ".join(headline_parts)), subtitle))
    story.append(Spacer(1, 8))

    def add_section(label: str, values: list[str] | None = None, paragraph: str = "") -> None:
        clean_values = values or []
        if not clean_values and not paragraph:
            return
        story.append(Paragraph(escape(label.upper()), section))
        if paragraph:
            story.append(Paragraph(escape(paragraph), body))
        for value in clean_values:
            story.append(Paragraph(f"•&nbsp;&nbsp;{escape(value)}", item))

    add_section("Professional Summary", paragraph=data["summary"])
    expertise = [*data["specialties"], *[f"Subspecialty: {value}" for value in data["subspecialties"]]]
    add_section("Clinical Expertise", expertise)
    add_section("Hospital Affiliations", data["hospitals"])
    add_section("Education & Training", data["education"])
    add_section("Board Certifications", data["certifications"])
    add_section("Medical Licensure", data["licenses"])

    details = []
    if data["years_experience"]:
        suffix = "" if "+" in data["years_experience"] else "+"
        details.append(f"Experience: {data['years_experience']}{suffix} years")
    if data["languages"]:
        details.append(f"Languages: {', '.join(data['languages'])}")
    if data["npi"]:
        details.append(f"NPI: {data['npi']}")
    if data["address"]:
        details.append(f"Public practice address: {data['address']}")
    add_section("Professional Details", details)

    source = data["source_label"]
    source_line = f"Source: {source}"
    if data["source_url"]:
        source_line += f" — {data['source_url']}"
    story.append(Paragraph(
        escape(
            "Generated by Medhunt from publicly displayed professional profile information. "
            "It is not candidate-authored. " + source_line
        ),
        disclosure,
    ))
    document.build(story, canvasmaker=page_canvas)
    rendered = buffer.getvalue()
    if not rendered.startswith(b"%PDF-"):
        raise RuntimeError("Professional profile PDF rendering failed.")
    return rendered


__all__ = ["filename", "fingerprint", "normalized_profile", "render"]
