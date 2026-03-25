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

    # Reply with auto-fetched quoted body (requires WAGGLE_IMAP_HOST):
    python3 waggle.py --to recipient@example.com \\
                      --subject "Re: Hello" \\
                      --body "# Thanks\\n\\nGreat to hear from you." \\
                      --in-reply-to "<message-id@example.com>"

    # Rich HTML with syntax highlighting and styled layout (opt-in):
    python3 waggle.py --to recipient@example.com \\
                      --subject "Hello" \\
                      --body "# Hi" \\
                      --rich

Usage (Python):
    from waggle import send_email
    send_email(to="recipient@example.com", subject="Hello", body_md="# Hi")
    send_email(..., rich=True)   # opt-in rich rendering

Configuration (environment variables):
    WAGGLE_HOST      SMTP host (default: localhost)
    WAGGLE_PORT      SMTP port (default: 465)
    WAGGLE_USER      SMTP username
    WAGGLE_PASS      SMTP password
    WAGGLE_FROM      From address (default: WAGGLE_USER)
    WAGGLE_NAME      Display name for From header
    WAGGLE_TLS       Use SSL/TLS (default: true; set 'false' for STARTTLS)

    WAGGLE_MAILDIR   Path to a Maildir directory for local reply quoting
                     (optional). Checked before IMAP — useful for agents
                     receiving mail via local delivery (e.g. Cloudflare
                     Workers, procmail, fetchmail) without an IMAP server.

    WAGGLE_IMAP_HOST IMAP host for auto-fetching quoted reply body (optional)
                     Defaults to WAGGLE_HOST if not set.
    WAGGLE_IMAP_PORT IMAP port (default: 993)
    WAGGLE_IMAP_TLS  Use IMAP SSL (default: true)
                     WAGGLE_USER / WAGGLE_PASS are reused for IMAP auth.

Or pass a config dict directly to send_email().
"""

import os
import re
import ssl
import imaplib
import email as email_lib
import smtplib
import argparse
import logging
import mimetypes
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from email.charset import Charset, QP
from email.utils import parseaddr, formataddr
from urllib.parse import urlparse

# Set up basic logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Quoted-printable charset — avoids base64 encoding of text parts
_UTF8_QP = Charset("utf-8")
_UTF8_QP.body_encoding = QP


# ---------------------------------------------------------------------------
# Security: Header sanitization
# ---------------------------------------------------------------------------

def _sanitize_header(value):
    """Remove CR/LF characters to prevent header injection."""
    if value is None:
        return None
    if re.search(r'[\r\n]', str(value)):
        raise ValueError(f"Header value contains illegal newline characters: {value!r}")
    return str(value)


def _validate_email(addr):
    """Extract bare email address from 'Name <addr>' format."""
    if not addr:
        return None
    _, email = parseaddr(addr)
    if not email or '@' not in email:
        raise ValueError(f"Invalid email address: {addr!r}")
    return email


# ---------------------------------------------------------------------------
# Maildir: Local message lookup for reply quoting
# ---------------------------------------------------------------------------

def _maildir_find_message(maildir_path, message_id):
    """
    Search a Maildir for a message by Message-ID header.

    Looks in {maildir_path}/new/ and {maildir_path}/cur/ (standard Maildir
    subdirectories). Uses a fast header-only scan: reads lines until the
    first blank line (end of headers) and only parses the full message if
    the Message-ID matches. Returns the parsed email.message.Message object
    if found, None otherwise.
    """
    mid = message_id.strip()
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    if not mid.endswith(">"):
        mid = f"{mid}>"

    maildir = Path(maildir_path)
    subdirs = [maildir / "new", maildir / "cur"]

    for subdir in subdirs:
        if not subdir.is_dir():
            continue
        for filepath in subdir.iterdir():
            if not filepath.is_file():
                continue
            try:
                # Fast path: scan headers only (up to the first blank line)
                with open(filepath, "rb") as f:
                    header_bytes = bytearray()
                    for line in f:
                        if line.strip() == b"":
                            break
                        header_bytes.extend(line)
                    header_msg = email_lib.message_from_bytes(bytes(header_bytes))
                    file_mid = (header_msg.get("Message-ID") or "").strip()
                    if file_mid != mid:
                        continue

                # Match found — parse the full message
                with open(filepath, "rb") as f:
                    return email_lib.message_from_bytes(f.read())
            except Exception:
                continue

    return None


# ---------------------------------------------------------------------------
# IMAP: Fetch quoted body for replies
# ---------------------------------------------------------------------------

def _imap_connect(cfg):
    """Return an authenticated IMAP connection or None if not configured."""
    imap_host = cfg.get("imap_host") or os.environ.get("WAGGLE_IMAP_HOST") or \
                os.environ.get("WAGGLE_HOST", "")
    if not imap_host:
        return None, None

    raw_port = cfg.get("imap_port") or os.environ.get("WAGGLE_IMAP_PORT", "993")
    try:
        imap_port = int(raw_port)
    except (ValueError, TypeError):
        imap_port = 993

    use_tls  = cfg.get("imap_tls", os.environ.get("WAGGLE_IMAP_TLS", "true").lower() != "false")
    user     = cfg.get("user")     or os.environ.get("WAGGLE_USER", "")
    password = cfg.get("password") or os.environ.get("WAGGLE_PASS", "")

    ctx = ssl.create_default_context()
    if use_tls:
        m = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=ctx)
    else:
        m = imaplib.IMAP4(imap_host, imap_port)
    m.login(user, password)
    return m, imap_host


def _imap_find_uid(m, mid):
    """Search all folders for a message by Message-ID. Returns (folder, uid) or (None, None)."""
    preferred = ["INBOX", "INBOX.Processed", '"Sent Items"', "Sent"]
    try:
        _, folder_data = m.list()
        all_folders = []
        for item in folder_data or []:
            if item:
                parts = item.decode() if isinstance(item, bytes) else item
                name = parts.rsplit(" ", 1)[-1].strip().strip('"')
                if name not in [f.strip('"') for f in preferred]:
                    all_folders.append(name)
    except Exception:
        all_folders = []

    for folder in preferred + all_folders:
        try:
            status, _ = m.select(folder, readonly=True)
            if status != "OK":
                continue
            status, data = m.search(None, f'HEADER Message-ID "{mid}"')
            if status == "OK" and data and data[0]:
                uids = data[0].split()
                if uids:
                    return folder, uids[-1]
        except Exception:
            continue
    return None, None


def fetch_quoted_body(message_id, config=None):
    """
    Fetch the original message and return (quoted_plain, quoted_html).

    Lookup order:
    1. Maildir (if configured via config["maildir_path"] or WAGGLE_MAILDIR)
    2. IMAP   (if configured via WAGGLE_IMAP_HOST or WAGGLE_HOST)

    If Maildir is configured and the message is found there, IMAP is skipped
    entirely. If Maildir is configured but the message isn't found, falls
    through to IMAP. Returns (None, None) if neither source has the message.

    Outlook-style quoting:
    - plain: full body under -----Original Message----- attribution header (snowballs naturally)
    - html:  original email HTML wrapped in a <div> blockquote (snowballs naturally)
    """
    from email.header import decode_header, make_header

    cfg = config or {}

    mid = message_id.strip()
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    if not mid.endswith(">"):
        mid = f"{mid}>"

    msg = None

    # --- Try Maildir first ---
    maildir_path = cfg.get("maildir_path") or os.environ.get("WAGGLE_MAILDIR", "")
    if maildir_path:
        try:
            msg = _maildir_find_message(maildir_path, mid)
            if msg:
                logger.debug(f"Found message {mid} in Maildir: {maildir_path}")
        except Exception as e:
            logger.warning(f"Maildir lookup failed: {e}")

    # --- Fall back to IMAP ---
    if msg is None:
        try:
            m, _ = _imap_connect(cfg)
            if m is None:
                if not maildir_path:
                    return None, None
                # Maildir was configured but message not found, no IMAP either
                return None, None

            folder, uid = _imap_find_uid(m, mid)
            if not uid:
                m.logout()
                return None, None

            # Re-select the folder (find may have left us somewhere else)
            m.select(folder, readonly=True)
            typ, data = m.fetch(uid, "(BODY.PEEK[])")
            m.logout()

            if typ != "OK" or not data or data[0] is None:
                return None, None

            msg = email_lib.message_from_bytes(data[0][1])
        except Exception as e:
            logger.warning(f"IMAP quote fetch failed: {e}")
            return None, None

    if msg is None:
        return None, None

    from_hdr = str(make_header(decode_header(msg.get("From", "")  or "")))
    date_hdr = str(make_header(decode_header(msg.get("Date", "")  or "")))
    subj_hdr = str(make_header(decode_header(msg.get("Subject", "") or "")))

    plain_body = html_body = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                continue
            if ct == "text/plain" and plain_body is None:
                p = part.get_payload(decode=True)
                if p:
                    plain_body = p.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ct == "text/html" and html_body is None:
                p = part.get_payload(decode=True)
                if p:
                    html_body = p.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        p = msg.get_payload(decode=True)
        if p:
            plain_body = p.decode(msg.get_content_charset() or "utf-8", errors="replace")

    # --- Plain text quoted block (Outlook style, full body, no trimming) ---
    attr_plain = (
        f"-----Original Message-----\n"
        f"From: {from_hdr}\n"
        f"Sent: {date_hdr}\n"
        f"Subject: {subj_hdr}"
    )
    if plain_body:
        quoted_plain = f"\n\n{attr_plain}\n\n{plain_body.strip()}"
    else:
        quoted_plain = f"\n\n{attr_plain}"

    # --- HTML quoted block (Outlook-style left-border blockquote) ---
    attr_html = (
        f'<p style="margin:0 0 8px 0;font-size:12px;color:#777;">'
        f'<b>From:</b> {from_hdr}<br>'
        f'<b>Sent:</b> {date_hdr}<br>'
        f'<b>Subject:</b> {subj_hdr}'
        f'</p>'
    )
    if html_body:
        inner = html_body
    elif plain_body:
        safe = plain_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        inner = f'<pre style="font-family:inherit;white-space:pre-wrap;margin:0;">{safe}</pre>'
    else:
        inner = "<p><em>(original message unavailable)</em></p>"

    quoted_html = (
        f'<div style="border-left:2px solid #ccc;padding-left:12px;'
        f'margin-top:16px;color:#555;">'
        f'{attr_html}'
        f'{inner}'
        f'</div>'
    )

    return quoted_plain, quoted_html


# ---------------------------------------------------------------------------
# HTML rendering — two modes
#
# DEFAULT (simple):  inline styles only, no <head>/<style> block.
#   - Works in Gmail (which strips <head> and <style> entirely)
#   - Less likely to trigger spam filters
#   - Looks like a real email from Outlook or Apple Mail
#
# RICH (opt-in via --rich / rich=True):  full pipeline with <head> CSS,
#   syntax-highlighted code blocks via pygments. Beautiful in most clients,
#   but stripped by Gmail and can look like marketing email.
# ---------------------------------------------------------------------------

def _escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _validate_url(url):
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https', 'mailto')


# --- Simple (default) renderer — inline styles, no <head> dependency ---

_PRE_STYLE = (
    "display:block;background:#f8f8f8;color:#333;"
    "padding:8px 12px;border-radius:3px;border:1px solid #e0e0e0;"
    "font-family:'Courier New',Courier,monospace;font-size:12px;"
    "white-space:pre;overflow-x:auto;margin:8px 0;"
)


def _highlight_code(lang, code_raw):
    """Render a code block with pygments inline styles, or plain fallback."""
    try:
        from pygments import highlight
        from pygments.lexers import get_lexer_by_name, TextLexer
        from pygments.formatters import HtmlFormatter
        try:
            lexer = get_lexer_by_name(lang) if lang else TextLexer()
        except Exception:
            lexer = TextLexer()
        formatter = HtmlFormatter(
            style="friendly",
            noclasses=True,   # inline styles — no <head> needed, Gmail-safe
            nowrap=True,      # we supply the <pre> wrapper
        )
        highlighted = highlight(code_raw, lexer, formatter)
        return f'<pre style="{_PRE_STYLE}">{highlighted}</pre>'
    except ImportError:
        code_esc = code_raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<pre style="{_PRE_STYLE}"><code>{code_esc}</code></pre>'


def _md_to_html_simple(text):
    """
    Render markdown to HTML with inline styles. No <head> CSS required.

    Code blocks are extracted first (before any other processing) to prevent
    inline-emphasis and paragraph-splitter from mangling code content.
    """
    # ------------------------------------------------------------------ #
    # Step 1: Extract fenced code blocks FIRST — replace with placeholders #
    # Placeholders use null bytes so _escape_html won't touch them, and    #
    # the paragraph handler can identify and restore them intact.           #
    # ------------------------------------------------------------------ #
    _blocks = []

    def _extract_fence(m):
        lang = m.group(1) or ""
        code_raw = m.group(2)
        rendered = _highlight_code(lang, code_raw)
        idx = len(_blocks)
        _blocks.append(rendered)
        # Surround with newlines so paragraph splitter treats it as a block
        return f"\n\n\x00WCODE{idx}\x00\n\n"

    text = re.sub(r"```(\w*)\n(.*?)```", _extract_fence, text, flags=re.DOTALL)

    # Step 2: HTML-escape everything EXCEPT the placeholders (null bytes safe)
    html = _escape_html(text)

    # Step 3: Markdown processing (headings, emphasis, lists, etc.)
    # Headings — append \n\n so they always create a paragraph boundary,
    # preventing the following content from being absorbed into the heading's
    # HTML block and losing <br> conversion on single newlines.
    html = re.sub(r"^### (.+)$",
        r'<h3 style="font-family:Calibri,Aptos,Arial,sans-serif;font-size:13pt;margin:16px 0 4px 0;">\1</h3>\n',
        html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$",
        r'<h2 style="font-family:Calibri,Aptos,Arial,sans-serif;font-size:15pt;margin:20px 0 6px 0;">\1</h2>\n',
        html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$",
        r'<h1 style="font-family:Calibri,Aptos,Arial,sans-serif;font-size:18pt;margin:24px 0 8px 0;">\1</h1>\n',
        html, flags=re.MULTILINE)

    # Inline emphasis (safe — code content already extracted)
    html = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", html)
    html = re.sub(r"\*\*(.+?)\*\*",     r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*",         r"<em>\1</em>", html)

    # Inline code
    html = re.sub(
        r"`(.+?)`",
        r'<code style="background:#f5f5f5;padding:1px 4px;border-radius:2px;'
        r'font-family:\'Courier New\',Courier,monospace;font-size:0.9em;">\1</code>',
        html
    )

    # Links
    def _link(m):
        link_text = m.group(1)
        url = m.group(2).replace('&quot;', '"')
        if not _validate_url(url):
            return link_text
        url = url.replace('"', '&quot;')
        return f'<a href="{url}" style="color:#0066cc;" rel="noopener noreferrer">{link_text}</a>'
    html = re.sub(r"\[(.+?)\]\((.+?)\)", _link, html)

    # Horizontal rule
    html = re.sub(r"^---+$",
        r'<hr style="border:none;border-top:1px solid #ddd;margin:16px 0;">',
        html, flags=re.MULTILINE)

    # Blockquote (> lines)
    def _quoteblock(m):
        lines = re.findall(r"^&gt; ?(.*)$", m.group(0), re.MULTILINE)
        inner = "<br>\n".join(lines)
        return (
            f'<blockquote style="border-left:3px solid #ccc;margin:8px 0;'
            f'padding:4px 0 4px 12px;color:#555;">{inner}</blockquote>'
        )
    html = re.sub(r"(^&gt;.*\n?)+", _quoteblock, html, flags=re.MULTILINE)

    # Unordered lists
    def _ul_block(m):
        items = re.findall(r"^[-*] (.+)$", m.group(0), re.MULTILINE)
        lis = "".join(f'<li style="margin:2px 0;">{i}</li>' for i in items)
        return f'<ul style="margin:8px 0;padding-left:20px;">{lis}</ul>'
    html = re.sub(r"(^[-*] .+\n?)+", _ul_block, html, flags=re.MULTILINE)

    # Ordered lists
    def _ol_block(m):
        items = re.findall(r"^\d+\. (.+)$", m.group(0), re.MULTILINE)
        lis = "".join(f'<li style="margin:2px 0;">{i}</li>' for i in items)
        return f'<ol style="margin:8px 0;padding-left:20px;">{lis}</ol>'
    html = re.sub(r"(^\d+\. .+\n?)+", _ol_block, html, flags=re.MULTILINE)

    # Step 4: Paragraphs — restore code placeholders as block elements
    _p_style = (
        'margin:0 0 10px 0;font-family:Calibri,Aptos,Arial,sans-serif;'
        'font-size:11pt;color:#000;'
    )
    paragraphs = re.split(r"\n{2,}", html.strip())
    wrapped = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        # Code placeholder — restore rendered block, no <p> wrapping
        m = re.fullmatch(r"\x00WCODE(\d+)\x00", p)
        if m:
            wrapped.append(_blocks[int(m.group(1))])
        elif p.startswith("<"):
            wrapped.append(p)
        else:
            p = p.replace("\n", "<br>\n")
            wrapped.append(f'<p style="{_p_style}">{p}</p>')

    return "\n".join(wrapped)


def _wrap_html_simple(body_html):
    """Minimal HTML wrapper — no <head> CSS, just a font on the body."""
    return (
        '<!DOCTYPE html><html><body style="font-family:Calibri,Aptos,Arial,sans-serif;'
        'font-size:11pt;color:#000;max-width:700px;">\n'
        + body_html
        + "\n</body></html>"
    )


# --- Rich (opt-in) renderer — <head> CSS + pygments syntax highlighting ---

def _md_to_html_rich(text):
    """Full markdown rendering with syntax-highlighted code blocks (opt-in)."""
    import markdown as md_lib

    extensions = ["extra", "codehilite", "tables", "fenced_code", "nl2br"]
    ext_configs = {
        "codehilite": {
            "noclasses": True,
            "guess_lang": False,
        }
    }
    try:
        html = md_lib.markdown(text, extensions=extensions, extension_configs=ext_configs)
    except Exception as e:
        logger.warning(f"Rich markdown rendering failed: {e}, falling back to simple")
        html = md_lib.markdown(text)

    html = re.sub(
        r'(<div class="codehilite")\s+(style="([^"]*)")',
        lambda m: (
            f'{m.group(1)} style="{m.group(3).rstrip(";")};'
            f' padding:10px 14px; border-radius:4px;"'
        ),
        html,
    )
    return html


def _wrap_html_rich(body_html):
    """Styled HTML wrapper with <head> CSS (opt-in)."""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: Georgia, serif; font-size: 16px;
         color: #222; max-width: 680px; margin: 40px auto; padding: 0 24px; }}
  h1, h2, h3 {{ font-family: system-ui, sans-serif; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 32px 0; }}
  a {{ color: #2563eb; }}
  blockquote {{ border-left: 3px solid #ccc; margin: 0; padding: 0 0 0 16px;
                color: #555; font-style: italic; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #f4f4f4; }}
  pre {{ font-family:'SF Mono','Fira Code',Consolas,monospace; font-size:12px; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------

def _md_to_plain(text):
    """
    Return markdown source as plain text — no stripping.
    AI agents reading with himalaya parse markdown natively.
    """
    return text.strip()


# ---------------------------------------------------------------------------
# Core send function
# ---------------------------------------------------------------------------

def send_email(
    to,
    subject,
    body_md,
    *,
    cc=None,
    reply_to=None,
    in_reply_to=None,
    references=None,
    from_name=None,
    attachments=None,
    rich=False,
    config=None,
):
    """
    Send a multipart email rendered from Markdown.

    HTML rendering:
      Default (rich=False): minimal inline-styled HTML — works in Gmail, less
        likely to trigger spam filters, looks like a real email.
      rich=True: full pipeline with <head> CSS and syntax-highlighted code
        blocks. Looks great in Outlook/Apple Mail; stripped by Gmail.

    When in_reply_to is provided and WAGGLE_IMAP_HOST is configured, waggle
    automatically fetches the original message from IMAP and appends a
    formatted quoted block. Falls back gracefully if IMAP is unavailable.

    Args:
        to:           Recipient address or "Name <addr>" string.
        subject:      Email subject line.
        body_md:      Message body in Markdown.
        cc:           Optional CC address(es), comma-separated.
        reply_to:     Optional Reply-To address.
        in_reply_to:  Message-ID of email being replied to (triggers quote fetch + threading).
        references:   References header value (threading).
        from_name:    Display name for the From header.
        attachments:  List of file paths to attach.
        rich:         Enable rich HTML rendering (opt-in).
        config:       Dict with SMTP + IMAP keys. Falls back to env vars if omitted.
    """
    cfg = config or {}

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

    # Auto-fetch quoted body from IMAP when replying
    quoted_plain = quoted_html = None
    if in_reply_to:
        quoted_plain, quoted_html = fetch_quoted_body(in_reply_to, config=cfg)

    # Security: sanitize headers
    subject     = _sanitize_header(subject)
    to          = _sanitize_header(to)
    cc          = _sanitize_header(cc)
    reply_to    = _sanitize_header(reply_to)
    in_reply_to = _sanitize_header(in_reply_to)
    references  = _sanitize_header(references)
    name        = _sanitize_header(name)

    from_header   = formataddr((name, from_addr)) if name else from_addr
    envelope_from = _validate_email(from_addr)
    envelope_to   = [_validate_email(to)]

    # Build body parts — render markdown first, then append quoted content after
    plain = _md_to_plain(body_md)

    if rich:
        try:
            html_body_rendered = _md_to_html_rich(body_md)
        except ImportError:
            html_body_rendered = _md_to_html_simple(body_md)
        html_full = _wrap_html_rich(html_body_rendered)
    else:
        html_body_rendered = _md_to_html_simple(body_md)
        html_full = _wrap_html_simple(html_body_rendered)

    # Append quoted content AFTER rendering (not into markdown source)
    if quoted_plain:
        plain += quoted_plain
    if quoted_html:
        html_full = html_full.replace("</body>", f"\n{quoted_html}\n</body>")

    # Build MIME structure
    alt = MIMEMultipart("alternative")
    # Use quoted-printable (not base64) for text parts
    alt.attach(MIMEText(plain,     "plain", _UTF8_QP))
    alt.attach(MIMEText(html_full, "html",  _UTF8_QP))

    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(alt)
        for path in attachments:
            p = Path(path)
            if not p.exists():
                logger.warning(f"Attachment not found, skipping: {path}")
                continue
            ctype, _ = mimetypes.guess_type(str(p))
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            with open(p, "rb") as f:
                data = f.read()
            if maintype == "image":
                part = MIMEImage(data, _subtype=subtype)
            else:
                part = MIMEBase(maintype, subtype)
                part.set_payload(data)
                encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=p.name)
            msg.attach(part)
    else:
        msg = alt

    msg["Subject"] = subject
    msg["From"]    = from_header
    msg["To"]      = to
    if cc:
        msg["Cc"] = cc
        for addr in cc.split(","):
            envelope_to.append(_validate_email(addr.strip()))
    if reply_to:
        msg["Reply-To"]    = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"]  = references

    ctx = ssl.create_default_context()

    if use_tls:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            if user and password:
                s.login(user, password)
            s.sendmail(envelope_from, envelope_to, msg.as_string())
    else:
        with smtplib.SMTP(host, port) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            if user and password:
                s.login(user, password)
            s.sendmail(envelope_from, envelope_to, msg.as_string())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli_main():
    return main()


def main():
    parser = argparse.ArgumentParser(
        description="waggle — send multipart email from Markdown",
        epilog=(
            "Default HTML is minimal inline-styled (Gmail-safe, spam-friendly). "
            "Use --rich for syntax-highlighted code blocks and a styled layout."
        )
    )
    parser.add_argument("--to",          required=True,  help="Recipient address")
    parser.add_argument("--subject",     required=True,  help="Subject line")
    parser.add_argument("--body",        required=True,  help="Message body (Markdown)")
    parser.add_argument("--cc",          default=None,   help="CC address(es)")
    parser.add_argument("--reply-to",    default=None,   help="Reply-To address")
    parser.add_argument("--from-name",   default=None,   help="Display name for From header")
    parser.add_argument("--in-reply-to", default=None,
                        help="Message-ID to reply to (enables threading + auto-quote)")
    parser.add_argument("--references",  default=None,   help="References header for threading")
    parser.add_argument("--attach",      action="append", default=None, metavar="FILE",
                        help="File to attach (can be specified multiple times)")
    parser.add_argument("--rich",        action="store_true", default=False,
                        help="Rich HTML: <head> CSS + syntax-highlighted code (opt-in)")
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
        attachments=args.attach,
        rich=args.rich,
    )
    print(f"✅ Sent to {args.to}")


if __name__ == "__main__":
    main()
