#!/usr/bin/env python3
"""
waggle — Not just a message. A vector.

Multipart email (plain text + HTML) for AI agents who write letters.

The waggle dance encodes direction, distance, and quality — the full vector,
not just a scalar ping. A good letter does the same. This tool helps send them.

Usage (CLI):
    python3 waggle.py --to recipient@example.com \\
                      --subject "Hello" \\
                      --body "# Hi\\n\\nThis is **markdown**."

Usage (Python):
    from waggle import send_email
    send_email(to="recipient@example.com", subject="Hello", body_md="# Hi")

Configuration (environment variables):
    WAGGLE_HOST      SMTP host (default: localhost)
    WAGGLE_PORT      SMTP port (default: 465)
    WAGGLE_USER      SMTP username
    WAGGLE_PASS      SMTP password
    WAGGLE_FROM      From address (default: WAGGLE_USER)
    WAGGLE_NAME      Display name for From header
    WAGGLE_TLS       Use SSL/TLS (default: true; set 'false' for STARTTLS)

Or pass a config dict directly to send_email().
"""

import os
import re
import ssl
import smtplib
import argparse
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr, formataddr
from urllib.parse import urlparse

# Set up basic logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security: Header sanitization
# ---------------------------------------------------------------------------

def _sanitize_header(value: str) -> str:
    """Remove CR/LF characters to prevent header injection."""
    if value is None:
        return None
    if re.search(r'[\r\n]', str(value)):
        raise ValueError(f"Header value contains illegal newline characters: {value!r}")
    return str(value)


def _validate_email(addr: str) -> str:
    """Extract bare email address from 'Name <addr>' format."""
    if not addr:
        return None
    _, email = parseaddr(addr)
    if not email or '@' not in email:
        raise ValueError(f"Invalid email address: {addr!r}")
    return email


# ---------------------------------------------------------------------------
# Markdown → HTML
# Uses python-markdown + pygments when available (preferred: syntax
# highlighting with inline styles that survive Gmail's CSS stripping).
# Falls back to a lightweight regex renderer with no dependencies.
# ---------------------------------------------------------------------------

def _md_to_html_rich(text: str) -> str:
    """Full markdown rendering with syntax-highlighted code blocks."""
    import markdown as md_lib

    extensions = ["extra", "codehilite", "tables", "fenced_code", "nl2br"]
    ext_configs = {
        "codehilite": {
            # noclasses=True → pygments writes style= attributes directly on
            # each span. No <style> block needed, so it survives Gmail's
            # aggressive CSS stripping. Safe for all major email clients.
            "noclasses": True,
            "guess_lang": False,
        }
    }
    try:
        html = md_lib.markdown(text, extensions=extensions, extension_configs=ext_configs)
    except Exception as e:
        logger.warning(f"Rich markdown rendering failed: {e}, falling back to simple")
        html = md_lib.markdown(text)

    # Add padding + monospace font to codehilite wrapper and its <pre>.
    html = re.sub(
        r'(<div class="codehilite")\s+(style="([^"]*)")',
        lambda m: (
            f'{m.group(1)} style="{m.group(3).rstrip(";")};'
            f' padding:10px 14px; border-radius:4px;"'
        ),
        html,
    )
    html = re.sub(
        r'(<pre)\s+(style="([^"]*)")',
        lambda m: (
            f'{m.group(1)} style="{m.group(3).rstrip(";")};'
            f" font-family:'SF Mono','Fira Code',Consolas,monospace;"
            f' font-size:12px; margin:0;"'
        ),
        html,
    )
    return html


def _escape_html(text: str) -> str:
    """Escape HTML entities."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _validate_url(url: str) -> bool:
    """Validate URL scheme for safe links (http/https/mailto only)."""
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https', 'mailto')


def _md_to_html_simple(text: str) -> str:
    """Lightweight fallback renderer — no dependencies."""
    # Escape HTML entities first
    html = _escape_html(text)

    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$",  r"<h2>\1</h2>", html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$",   r"<h1>\1</h1>", html, flags=re.MULTILINE)

    html = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", html)
    html = re.sub(r"\*\*(.+?)\*\*",     r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*",         r"<em>\1</em>", html)

    # Fenced code blocks (``` ... ```)
    def _fence(m):
        lang = m.group(1) or ""
        # Content is already escaped above
        code = m.group(2)
        style = (
            "background:#1e1e1e;color:#d4d4d4;padding:10px 14px;"
            "border-radius:4px;font-family:'SF Mono','Fira Code',Consolas,monospace;"
            "font-size:12px;overflow-x:auto;"
        )
        return f'<pre style="{style}"><code>{code}</code></pre>'
    html = re.sub(r"```(\w*)\n(.*?)```", _fence, html, flags=re.DOTALL)

    html = re.sub(r"`(.+?)`", r'<code style="background:#f4f4f4;padding:2px 5px;border-radius:3px;font-size:0.9em;">\1</code>', html)
    
    # Links with validation
    def _link(m):
        text = m.group(1)
        url = m.group(2).replace('&quot;', '"')
        if not _validate_url(url):
            return text  # Return plain text if URL is unsafe
        url = url.replace('"', '&quot;')
        return f'<a href="{url}" rel="noopener noreferrer">{text}</a>'
    html = re.sub(r"\[(.+?)\]\((.+?)\)", _link, html)
    
    html = re.sub(r"^---+$", r"<hr>", html, flags=re.MULTILINE)

    def _listblock(m):
        items = re.findall(r"^[-*] (.+)$", m.group(0), re.MULTILINE)
        lis = "".join(f"<li>{i}</li>" for i in items)
        return f"<ul>{lis}</ul>"
    html = re.sub(r"(^[-*] .+\n?)+", _listblock, html, flags=re.MULTILINE)

    paragraphs = re.split(r"\n{2,}", html.strip())
    wrapped = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if p.startswith("<"):
            wrapped.append(p)
        else:
            p = p.replace("\n", "<br>\n")
            wrapped.append(f"<p>{p}</p>")
    return "\n".join(wrapped)


def _md_to_html(text: str) -> str:
    """Convert markdown to HTML, preferring the rich renderer."""
    try:
        return _md_to_html_rich(text)
    except ImportError:
        return _md_to_html_simple(text)


def _wrap_html(body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: Georgia, serif; font-size: 16px; line-height: 1.7;
         color: #222; max-width: 680px; margin: 40px auto; padding: 0 24px; }}
  h1, h2, h3 {{ font-family: system-ui, sans-serif; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 32px 0; }}
  a {{ color: #2563eb; }}
  blockquote {{ border-left: 3px solid #ccc; margin: 0; padding-left: 16px;
                color: #555; font-style: italic; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #f4f4f4; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------

def _md_to_plain(text: str) -> str:
    """
    Return markdown source as plain text — no stripping.

    AI agents reading with himalaya or similar tools parse markdown natively.
    Raw markdown is cleaner and more faithful than a stripped approximation.
    """
    return text.strip()


# ---------------------------------------------------------------------------
# Core send function
# ---------------------------------------------------------------------------

def send_email(
    to: str,
    subject: str,
    body_md: str,
    *,
    cc: str | None = None,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    from_name: str | None = None,
    config: dict | None = None,
) -> None:
    """
    Send a multipart email rendered from Markdown.

    Plain text body: raw markdown (AI-agent friendly).
    HTML body: fully rendered with syntax-highlighted code blocks.

    Args:
        to:           Recipient address or "Name <addr>" string.
        subject:      Email subject line.
        body_md:      Message body in Markdown.
        cc:           Optional CC address(es), comma-separated.
        reply_to:     Optional Reply-To header.
        in_reply_to:  Message-ID of the email being replied to (threading).
        references:   References header value (threading).
        from_name:    Display name for the From header.
        config:       Dict with keys: host, port, user, password, from_addr,
                      from_name, tls (bool). Falls back to env vars if omitted.
    """
    cfg = config or {}

    # Validate port
    raw_port = cfg.get("port") or os.environ.get("WAGGLE_PORT", "465")
    try:
        port = int(raw_port)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid SMTP port: {raw_port!r}")

    host      = cfg.get("host")      or os.environ.get("WAGGLE_HOST", "localhost")
    user      = cfg.get("user")      or os.environ.get("WAGGLE_USER", "")
    password  = cfg.get("password")  or os.environ.get("WAGGLE_PASS", "")
    from_addr = cfg.get("from_addr") or os.environ.get("WAGGLE_FROM", user)
    name      = from_name or cfg.get("from_name") or os.environ.get("WAGGLE_NAME", "")
    use_tls   = cfg.get("tls", os.environ.get("WAGGLE_TLS", "true").lower() != "false")

    # Security: Sanitize all header values
    subject = _sanitize_header(subject)
    to = _sanitize_header(to)
    cc = _sanitize_header(cc)
    reply_to = _sanitize_header(reply_to)
    in_reply_to = _sanitize_header(in_reply_to)
    references = _sanitize_header(references)
    name = _sanitize_header(name)

    # Use formataddr for proper RFC 5322 quoting
    from_header = formataddr((name, from_addr)) if name else from_addr

    # Extract bare addresses for SMTP envelope
    envelope_from = _validate_email(from_addr)
    envelope_to = [_validate_email(to)]
    
    msg = MIMEMultipart("alternative")
    msg["Subject"]  = subject
    msg["From"]     = from_header
    msg["To"]       = to
    if cc:
        msg["Cc"]         = cc
        for addr in cc.split(","):
            envelope_to.append(_validate_email(addr.strip()))
    if reply_to:
        msg["Reply-To"]   = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    plain     = _md_to_plain(body_md)
    html_body = _md_to_html(body_md)
    html_full = _wrap_html(html_body)

    msg.attach(MIMEText(plain,    "plain", "utf-8"))
    msg.attach(MIMEText(html_full, "html", "utf-8"))

    # Security: Create SSL context for both TLS and STARTTLS
    ctx = ssl.create_default_context()

    if use_tls:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            if user and password:
                s.login(user, password)
            s.sendmail(envelope_from, envelope_to, msg.as_string())
    else:
        with smtplib.SMTP(host, port) as s:
            s.ehlo()
            s.starttls(context=ctx)  # Security: Added SSL context
            s.ehlo()
            if user and password:
                s.login(user, password)
            s.sendmail(envelope_from, envelope_to, msg.as_string())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="waggle — send multipart email from Markdown"
    )
    parser.add_argument("--to",          required=True,  help="Recipient address")
    parser.add_argument("--subject",     required=True,  help="Subject line")
    parser.add_argument("--body",        required=True,  help="Message body (Markdown)")
    parser.add_argument("--cc",          default=None,   help="CC address(es)")
    parser.add_argument("--reply-to",    default=None,   help="Reply-To address")
    parser.add_argument("--from-name",   default=None,   help="Display name for From header")
    parser.add_argument("--in-reply-to", default=None,   help="Message-ID for threading")
    parser.add_argument("--references",  default=None,   help="References header for threading")
    args = parser.parse_args()

    send_email(
        to=args.to,
        subject=args.subject,
        body_md=args.body,
        cc=args.cc,
        reply_to=args.reply_to,
        in_reply_to=args.in_reply_to,
        references=args.references,
        from_name=args.from_name,
    )
    print(f"✅ Sent to {args.to}")


if __name__ == "__main__":
    main()
