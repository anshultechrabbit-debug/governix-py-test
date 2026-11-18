"""The look of every Governix email: a bank-style message in the recipient's bank's name and colour.

Built for mail clients, not browsers: 600px tables, inline styles, a bulletproof button, a hidden preheader, and
a plain-text twin for clients that show no HTML. Every value is escaped.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

DEFAULT_COLOR = "#f06543"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass
class Brand:
    name: str  # the bank's name, or the platform's for Governix staff
    platform: str  # "Governix"
    color: str = DEFAULT_COLOR
    portal: str | None = None

    @staticmethod
    def of(platform: str, organization=None, portal: str | None = None) -> Brand:
        color = getattr(organization, "primary_color", None) or DEFAULT_COLOR
        return Brand(name=getattr(organization, "name", None) or platform, platform=platform,
                     color=color if _HEX.match(color) else DEFAULT_COLOR, portal=portal)


@dataclass
class Content:
    title: str
    preheader: str
    paragraphs: list[str]
    greeting: str | None = None
    details: list[tuple[str, str]] = field(default_factory=list)  # label, value
    secret: tuple[str, str] | None = None  # label, value: a temporary password, shown large and monospaced
    button: tuple[str, str] | None = None  # label, url
    notice: str | None = None  # a security note, in its own box
    tone: str = "info"  # info | security | alert


TONES = {
    "info": ("#eff6ff", "#1e40af", "#bfdbfe"),
    "security": ("#fff7ed", "#9a3412", "#fed7aa"),
    "alert": ("#fef2f2", "#991b1b", "#fecaca"),
}


def _e(value: str) -> str:
    return html.escape(value or "", quote=True)


def render_text(content: Content, brand: Brand, recipient: str) -> str:
    lines = [f"{brand.name}", "=" * len(brand.name), "", content.title, ""]
    if content.greeting:
        lines += [content.greeting, ""]
    for paragraph in content.paragraphs:
        lines += [paragraph, ""]
    for label, value in content.details:
        lines.append(f"{label}: {value}")
    if content.details:
        lines.append("")
    if content.secret:
        lines += [f"{content.secret[0]}: {content.secret[1]}", ""]
    if content.button:
        lines += [f"{content.button[0]}: {content.button[1]}", ""]
    if content.notice:
        lines += [content.notice, ""]
    lines += ["--", f"Automated message from {brand.name} on {brand.platform}. Please do not reply.",
              f"Sent to {recipient}."]
    return "\n".join(lines)


def render_html(content: Content, brand: Brand, recipient: str) -> str:
    c = _e(brand.color)
    paragraphs = "".join(
        f'<p style="margin:0 0 14px;font-size:15px;line-height:24px;color:#374151">{_e(p).replace(chr(10), "<br>")}</p>'
        for p in content.paragraphs)
    greeting = (f'<p style="margin:0 0 14px;font-size:15px;line-height:24px;color:#111827">{_e(content.greeting)}</p>'
                if content.greeting else "")
    details = ""
    if content.details:
        rows = "".join(
            f'<tr><td style="padding:10px 16px;font-size:13px;color:#6b7280;width:38%;border-bottom:1px solid #eef0f3">'
            f'{_e(label)}</td><td style="padding:10px 16px;font-size:14px;color:#111827;font-weight:600;'
            f'border-bottom:1px solid #eef0f3;word-break:break-all">{_e(value)}</td></tr>'
            for label, value in content.details)
        details = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:6px 0 18px;'
                   f'border:1px solid #e5e7eb;border-radius:10px;border-collapse:separate;overflow:hidden">{rows}</table>')
    secret = ""
    if content.secret:
        secret = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:4px 0 20px">'
                  f'<tr><td style="background:#f9fafb;border:1px dashed #d1d5db;border-radius:10px;padding:16px;'
                  f'text-align:center"><div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;'
                  f'color:#6b7280;margin-bottom:6px">{_e(content.secret[0])}</div><div style="font-family:Consolas,'
                  f'Menlo,monospace;font-size:22px;letter-spacing:.08em;color:#111827;font-weight:700">'
                  f'{_e(content.secret[1])}</div></td></tr></table>')
    button = ""
    if content.button:
        label, url = content.button
        button = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 22px"><tr>'
                  f'<td style="border-radius:8px;background:{c}"><a href="{_e(url)}" target="_blank" '
                  f'style="display:inline-block;padding:13px 28px;font-size:15px;font-weight:700;color:#ffffff;'
                  f'text-decoration:none;border-radius:8px">{_e(label)}</a></td></tr></table>'
                  f'<p style="margin:0 0 18px;font-size:12px;line-height:18px;color:#6b7280">If the button doesn\'t '
                  f'work, copy this address into your browser:<br><a href="{_e(url)}" style="color:{c};'
                  f'word-break:break-all">{_e(url)}</a></p>')
    notice = ""
    if content.notice:
        bg, fg, border = TONES.get(content.tone, TONES["info"])
        notice = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:4px 0 6px">'
                  f'<tr><td style="background:{bg};border:1px solid {border};border-radius:10px;padding:12px 16px;'
                  f'font-size:13px;line-height:20px;color:{fg}">{_e(content.notice)}</td></tr></table>')
    year = datetime.now(UTC).year
    via = (f'<span style="font-size:12px;color:rgba(255,255,255,.8)">on {_e(brand.platform)}</span>'
           if brand.name != brand.platform else "")
    portal = (f' &middot; <a href="{_e(brand.portal)}" style="color:#6b7280">{_e(brand.portal)}</a>'
              if brand.portal else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{_e(content.title)}</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#f3f4f6">{_e(content.preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6">
<tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;
font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
  <tr><td style="background:{c};border-radius:12px 12px 0 0;padding:22px 32px">
    <div style="font-size:20px;font-weight:700;color:#ffffff;letter-spacing:.01em">{_e(brand.name)}</div>{via}
  </td></tr>
  <tr><td style="background:#ffffff;padding:32px;border-left:1px solid #e5e7eb;border-right:1px solid #e5e7eb">
    <h1 style="margin:0 0 18px;font-size:22px;line-height:30px;color:#111827;font-weight:700">{_e(content.title)}</h1>
    {greeting}{paragraphs}{details}{secret}{button}{notice}
  </td></tr>
  <tr><td style="background:#f9fafb;border:1px solid #e5e7eb;border-top:0;border-radius:0 0 12px 12px;padding:20px 32px;
  font-size:12px;line-height:18px;color:#6b7280">
    This is an automated message from {_e(brand.name)} on {_e(brand.platform)}. Please do not reply to it.<br>
    {_e(brand.platform)} will never ask for your password or authenticator code by email or phone.<br>
    Sent to {_e(recipient)}{portal}<br>&copy; {year} {_e(brand.name)}
  </td></tr>
</table></td></tr></table></body></html>"""
