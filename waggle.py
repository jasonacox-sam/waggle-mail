#!/usr/bin/env python3
"""
waggle — Not just a message. A vector.

Multipart email (plain text + HTML) for AI agents who write letters.
Full IMAP + SMTP client: list, read, reply, move, download attachments, send.

The waggle dance encodes direction, distance, and quality — the full vector,
not just a scalar ping. A good letter does the same. This tool helps send them.

Usage (CLI — subcommands):

    # List inbox
    waggle list
    waggle list --folder INBOX.Processed --limit 30

    # Read a message (prints body + threading headers + reply template)
    waggle read 42
    waggle read 42 --folder INBOX.Processed

    # Move a message between folders
    waggle move 42 INBOX.Processed
    waggle move 42 INBOX.Processed --folder INBOX

    # Download attachments
    waggle attach 42
    waggle attach 42 --folder INBOX --dest /tmp/attachments/

    # Send a new email
    waggle send --to recipient@example.com --subject "Hello" --body "# Hi"

    # Reply with auto-fetched quoted body (requires WAGGLE_IMAP_HOST):
    waggle send --to sender@example.com \\
                --subject "Re: Hello" \\
                --body "Thanks!" \\
                --in-reply-to "<message-id@example.com>"

    # Rich HTML with styled layout (opt-in):
    waggle send --to recipient@example.com --subject "Hello" --body "# Hi" --rich

Usage (Python API):

    from waggle import send_email, list_inbox, read_message, move_message, download_attachments

    # List inbox
    messages = list_inbox(folder="INBOX", limit=20)
    for m in messages:
        print(m["uid"], m["from"], m["subject"], m["date"])

    # Read a message
    msg = read_message("42", folder="INBOX")
    print(msg["body_plain"])
    print(msg["message_id"])   # use for in_reply_to

    # Reply
    send_email(
        to=msg["from_addr"],
        subject=msg["reply_subject"],
        body_md="Your reply here.",
        in_reply_to=msg["message_id"],
        references=msg["reply_references"],
    )

    # Move to processed
    move_message("42", dest_folder="INBOX.Processed", src_folder="INBOX")

    # Download attachments
    paths = download_attachments("42", folder="INBOX", dest_dir="/tmp/attachments/")

Configuration (environment variables):
    WAGGLE_HOST      SMTP host (default: localhost)
    WAGGLE_PORT      SMTP port (default: 465)
    WAGGLE_USER      SMTP username
    WAGGLE_PASS      SMTP password
    WAGGLE_FROM      From address (default: WAGGLE_USER)
    WAGGLE_NAME      Display name for From header
    WAGGLE_TLS       Use SSL/TLS (default: true; set 'false' for STARTTLS)

    WAGGLE_IMAP_HOST IMAP host (default: WAGGLE_HOST)
    WAGGLE_IMAP_PORT IMAP port (default: 993)
    WAGGLE_IMAP_TLS  Use IMAP SSL (default: true)
                     WAGGLE_USER / WAGGLE_PASS are reused for IMAP auth.
"""

__version__ = "1.8.1"

import os
import re
import ssl
import imaplib
import email as email_lib
from email.header import decode_header, make_header
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

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

_UTF8_QP = Charset("utf-8")
_UTF8_QP.body_encoding = QP


# ---------------------------------------------------------------------------
# Security: Header sanitization
# ---------------------------------------------------------------------------

def _sanitize_header(value):
    if value is None:
        return None
    if re.search(r'[\r\n]', str(value)):
        raise ValueError(f"Header value contains illegal newline characters: {value!r}")
    return str(value)


def _validate_email(addr):
    if not addr:
        return None
    _, email = parseaddr(addr)
    if not email or '@' not in email:
        raise ValueError(f"Invalid email address: {addr!r}")
    return email


def _decode_header_str(raw):
    """Decode a potentially RFC2047-encoded header value to a plain string."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


# ---------------------------------------------------------------------------
# IMAP: Connection helpers
# ---------------------------------------------------------------------------

def _build_cfg(config=None):
    """Merge explicit config dict with environment variable fallbacks."""
    cfg = config or {}
    return {
        "imap_host":  cfg.get("imap_host")  or os.environ.get("WAGGLE_IMAP_HOST") or os.environ.get("WAGGLE_HOST", ""),
        "imap_port":  int(cfg.get("imap_port") or os.environ.get("WAGGLE_IMAP_PORT", "993")),
        "imap_tls":   cfg.get("imap_tls", os.environ.get("WAGGLE_IMAP_TLS", "true").lower() != "false"),
        "user":       cfg.get("user")       or os.environ.get("WAGGLE_USER", ""),
        "password":   cfg.get("password")   or os.environ.get("WAGGLE_PASS", ""),
        "host":       cfg.get("host")       or os.environ.get("WAGGLE_HOST", "localhost"),
        "port":       int(cfg.get("port")   or os.environ.get("WAGGLE_PORT", "465")),
        "from_addr":  cfg.get("from_addr")  or os.environ.get("WAGGLE_FROM", cfg.get("user") or os.environ.get("WAGGLE_USER", "")),
        "from_name":  cfg.get("from_name")  or os.environ.get("WAGGLE_NAME", ""),
        "tls":        cfg.get("tls", os.environ.get("WAGGLE_TLS", "true").lower() != "false"),
    }


def _imap_connect(cfg):
    """Return an authenticated IMAP4 connection, or (None, None) if not configured."""
    if not cfg.get("imap_host"):
        return None, None
    ctx = ssl.create_default_context()
    if cfg["imap_tls"]:
        m = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=ctx)
    else:
        m = imaplib.IMAP4(cfg["imap_host"], cfg["imap_port"])
    m.login(cfg["user"], cfg["password"])
    return m, cfg["imap_host"]


def _imap_find_uid(m, mid):
    """
    Search all folders for a message by Message-ID.
    Returns (folder_name, uid_bytes) or (None, None).
    Searches INBOX and INBOX.Processed first, then all other folders.
    """
    preferred = ["INBOX", "INBOX.Processed", "INBOX.Sent", '"Sent Items"', "Sent"]
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


def _parse_message(raw_bytes):
    """
    Parse a raw email message and return a structured dict.
    Keys: message_id, references, in_reply_to, from_addr, from_name,
          to, subject, date, body_plain, body_html, attachments (list of dicts)
    """
    msg = email_lib.message_from_bytes(raw_bytes)

    message_id  = _decode_header_str(msg.get("Message-ID", "")).strip()
    references  = _decode_header_str(msg.get("References", "")).strip()
    in_reply_to = _decode_header_str(msg.get("In-Reply-To", "")).strip()
    from_raw    = _decode_header_str(msg.get("From", ""))
    from_name_p, from_addr_p = parseaddr(from_raw)
    subject     = _decode_header_str(msg.get("Subject", ""))
    date        = _decode_header_str(msg.get("Date", ""))
    to          = _decode_header_str(msg.get("To", ""))

    body_plain = None
    body_html  = None
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            ct  = part.get_content_type()
            cd  = str(part.get("Content-Disposition", ""))
            fn  = part.get_filename()
            if fn:
                fn = _decode_header_str(fn)

            if "attachment" in cd or fn:
                payload = part.get_payload(decode=True)
                attachments.append({
                    "filename":     fn or "attachment",
                    "content_type": ct,
                    "size":         len(payload) if payload else 0,
                    "payload":      payload,
                })
            elif ct == "text/plain" and body_plain is None:
                p = part.get_payload(decode=True)
                if p:
                    body_plain = p.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ct == "text/html" and body_html is None:
                p = part.get_payload(decode=True)
                if p:
                    body_html = p.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        p = msg.get_payload(decode=True)
        if p:
            body_plain = p.decode(msg.get_content_charset() or "utf-8", errors="replace")

    # Build reply references chain
    if references and message_id:
        reply_references = f"{references} {message_id}".strip()
    elif message_id:
        reply_references = message_id
    else:
        reply_references = ""

    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    return {
        "message_id":       message_id,
        "references":       references,
        "in_reply_to":      in_reply_to,
        "reply_references": reply_references,
        "reply_subject":    reply_subject,
        "from_addr":        from_addr_p,
        "from_name":        from_name_p,
        "from_raw":         from_raw,
        "to":               to,
        "subject":          subject,
        "date":             date,
        "body_plain":       body_plain,
        "body_html":        body_html,
        "attachments":      attachments,
    }


# ---------------------------------------------------------------------------
# Public IMAP API
# ---------------------------------------------------------------------------

def list_inbox(folder="INBOX", limit=20, config=None):
    """
    List email envelopes from a folder.

    Returns a list of dicts, most recent first:
        uid, message_id, from_addr, from_name, from_raw, subject, date, flags, size

    Args:
        folder: IMAP folder name (default: INBOX)
        limit:  Max number of messages to return (default: 20)
        config: Optional config dict (falls back to env vars)
    """
    cfg = _build_cfg(config)
    m, _ = _imap_connect(cfg)
    if m is None:
        raise RuntimeError("IMAP not configured — set WAGGLE_IMAP_HOST")

    try:
        status, _ = m.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not select folder: {folder!r}")

        status, data = m.search(None, "ALL")
        if status != "OK" or not data[0]:
            return []

        uids = data[0].split()
        # Most recent last — take the last `limit` and reverse
        uids = uids[-limit:][::-1]

        results = []
        for uid in uids:
            try:
                _, msg_data = m.fetch(uid, "(FLAGS RFC822.SIZE BODY[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
                if not msg_data or msg_data[0] is None:
                    continue
                raw_headers = b""
                flags_str = ""
                size = 0
                for part in msg_data:
                    if isinstance(part, tuple):
                        info = part[0].decode() if isinstance(part[0], bytes) else str(part[0])
                        if "FLAGS" in info:
                            flags_match = re.search(r'FLAGS \(([^)]*)\)', info)
                            if flags_match:
                                flags_str = flags_match.group(1)
                            size_match = re.search(r'RFC822\.SIZE (\d+)', info)
                            if size_match:
                                size = int(size_match.group(1))
                        raw_headers = part[1] if isinstance(part[1], bytes) else b""

                message_id = from_raw = subject = date = ""
                for line in raw_headers.decode("utf-8", errors="replace").splitlines():
                    lower = line.lower()
                    if lower.startswith("message-id:"):
                        message_id = _decode_header_str(line.split(":", 1)[1].strip())
                    elif lower.startswith("from:"):
                        from_raw = _decode_header_str(line.split(":", 1)[1].strip())
                    elif lower.startswith("subject:"):
                        subject = _decode_header_str(line.split(":", 1)[1].strip())
                    elif lower.startswith("date:"):
                        date = line.split(":", 1)[1].strip()

                from_name_p, from_addr_p = parseaddr(from_raw)
                results.append({
                    "uid":        uid.decode() if isinstance(uid, bytes) else str(uid),
                    "message_id": message_id,
                    "from_addr":  from_addr_p,
                    "from_name":  from_name_p,
                    "from_raw":   from_raw,
                    "subject":    subject,
                    "date":       date,
                    "flags":      flags_str,
                    "size":       size,
                    "unread":     r"\Seen" not in flags_str,
                })
            except Exception as e:
                logger.warning(f"Error fetching envelope for uid {uid}: {e}")
                continue

        return results
    finally:
        try:
            m.logout()
        except Exception:
            pass


def read_message(uid, folder="INBOX", config=None):
    """
    Read a full email message by IMAP sequence number / UID.

    Returns a structured dict with body, headers, threading info, and attachment list.
    Use msg["message_id"] and msg["reply_references"] for waggle send_email().

    Args:
        uid:    IMAP sequence number (as string or int)
        folder: IMAP folder (default: INBOX)
        config: Optional config dict

    Returns dict keys:
        uid, folder, message_id, references, in_reply_to, reply_references,
        reply_subject, from_addr, from_name, from_raw, to, subject, date,
        body_plain, body_html, attachments (list of {filename, content_type, size})

    Note: attachments list contains metadata only — call download_attachments()
    to save files to disk.
    """
    cfg = _build_cfg(config)
    m, _ = _imap_connect(cfg)
    if m is None:
        raise RuntimeError("IMAP not configured — set WAGGLE_IMAP_HOST")

    try:
        status, _ = m.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not select folder: {folder!r}")

        uid_bytes = str(uid).encode() if not isinstance(uid, bytes) else uid
        typ, data = m.fetch(uid_bytes, "(BODY.PEEK[])")
        if typ != "OK" or not data or data[0] is None:
            raise RuntimeError(f"Message {uid} not found in {folder}")

        raw = data[0][1]
        result = _parse_message(raw)
        result["uid"]    = str(uid)
        result["folder"] = folder
        return result
    finally:
        try:
            m.logout()
        except Exception:
            pass


def move_message(uid, dest_folder, src_folder="INBOX", config=None):
    """
    Move a message from src_folder to dest_folder.

    Uses UID COPY + UID STORE + EXPUNGE so sequence number shifts don't matter.
    Use this after sending a reply: move_message("42", "INBOX.Processed")

    Args:
        uid:         IMAP sequence number or UID (as returned by list_inbox/read_message)
        dest_folder: Target folder (e.g. "INBOX.Processed")
        src_folder:  Source folder (default: INBOX)
        config:      Optional config dict

    Returns True on success.
    """
    cfg = _build_cfg(config)
    m, _ = _imap_connect(cfg)
    if m is None:
        raise RuntimeError("IMAP not configured — set WAGGLE_IMAP_HOST")

    try:
        status, _ = m.select(src_folder)
        if status != "OK":
            raise RuntimeError(f"Could not select folder: {src_folder!r}")

        seq = str(uid)

        # First resolve to a real UID via UID SEARCH on the sequence number
        # This makes the operation immune to sequence-number shifts from prior expunges
        status, data = m.fetch(seq, "(UID)")
        real_uid = seq  # fallback
        if status == "OK" and data and data[0]:
            raw = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
            m_uid = re.search(r'UID (\d+)', raw)
            if m_uid:
                real_uid = m_uid.group(1)

        uid_bytes = real_uid.encode() if isinstance(real_uid, str) else real_uid

        # UID COPY to destination
        status, _ = m.uid("COPY", uid_bytes, dest_folder)
        if status != "OK":
            raise RuntimeError(f"UID COPY to {dest_folder!r} failed: {status}")

        # Mark original as deleted (by UID)
        m.uid("STORE", uid_bytes, "+FLAGS", r"(\Deleted)")

        # Expunge
        m.expunge()
        return True
    finally:
        try:
            m.logout()
        except Exception:
            pass


def download_attachments(uid, folder="INBOX", dest_dir=".", config=None):
    """
    Download all attachments from a message to dest_dir.

    Args:
        uid:      IMAP sequence number
        folder:   IMAP folder (default: INBOX)
        dest_dir: Directory to save files (created if needed)
        config:   Optional config dict

    Returns list of saved file paths (strings).
    """
    cfg = _build_cfg(config)
    m, _ = _imap_connect(cfg)
    if m is None:
        raise RuntimeError("IMAP not configured — set WAGGLE_IMAP_HOST")

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    saved_paths = []

    try:
        status, _ = m.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not select folder: {folder!r}")

        uid_bytes = str(uid).encode() if not isinstance(uid, bytes) else uid
        typ, data = m.fetch(uid_bytes, "(BODY.PEEK[])")
        if typ != "OK" or not data or data[0] is None:
            raise RuntimeError(f"Message {uid} not found in {folder}")

        msg = email_lib.message_from_bytes(data[0][1])

        for i, part in enumerate(msg.walk()):
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            fn = part.get_filename()
            if fn:
                fn = _decode_header_str(fn)

            # Save if it has a filename OR is marked as attachment
            if not fn and "attachment" not in cd:
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            if not fn:
                ext = mimetypes.guess_extension(ct) or ".bin"
                fn = f"attachment_{i}{ext}"

            # Sanitize filename
            fn = re.sub(r'[^\w\-_\. ]', '_', fn)
            out_path = dest / fn

            # Avoid overwrites
            counter = 1
            while out_path.exists():
                stem = Path(fn).stem
                suffix = Path(fn).suffix
                out_path = dest / f"{stem}_{counter}{suffix}"
                counter += 1

            out_path.write_bytes(payload)
            saved_paths.append(str(out_path))

        return saved_paths
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# IMAP: Check for recently sent duplicates
# ---------------------------------------------------------------------------

_SEND_LOG = Path(os.environ.get("WAGGLE_SEND_LOG",
    str(Path.home() / ".openclaw" / "workspace" / "tmp" / "waggle-sent.log")))


def _log_sent(to, subject):
    """Append a send record to the local sent log."""
    import time
    _SEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_SEND_LOG, "a") as f:
        f.write(f"{int(time.time())}|{(_validate_email(to) or to).lower()}|{subject.lower().strip()}\n")


def check_recently_sent(to, subject, within_minutes=5, config=None):
    """
    Check the local send log for a matching email sent recently.
    Use before retrying a send to avoid duplicates caused by SMTP slowness.

    Returns True if a duplicate was found (skip the send).
    Returns False if safe to send.

    Args:
        to:              Recipient address
        subject:         Subject line (partial match)
        within_minutes:  How far back to look (default: 5 minutes)
        config:          Unused, kept for API compatibility
    """
    import time
    if not _SEND_LOG.exists():
        return False
    cutoff = int(time.time()) - (within_minutes * 60)
    to_addr = (_validate_email(to) or to).lower()
    subj_lower = subject.lower().strip()
    try:
        with open(_SEND_LOG) as f:
            for line in f:
                parts = line.strip().split("|", 2)
                if len(parts) != 3:
                    continue
                ts, log_to, log_subj = parts
                if int(ts) < cutoff:
                    continue
                if log_to == to_addr and (subj_lower in log_subj or log_subj in subj_lower):
                    return True
    except Exception as e:
        logger.warning(f"check_recently_sent failed: {e}")
    return False


# ---------------------------------------------------------------------------
# IMAP: Fetch quoted body for replies (used internally by send_email)
# ---------------------------------------------------------------------------

def fetch_quoted_body(message_id, config=None):
    """
    Fetch the original message from IMAP and return (quoted_plain, quoted_html).

    Outlook-style quoting:
    - plain: -----Original Message----- block
    - html:  left-border blockquote div (snowballs like Outlook)

    Searches all folders — works even after message is moved to INBOX.Processed.
    Returns (None, None) if IMAP is not configured or message not found.
    """
    cfg = _build_cfg(config)

    mid = message_id.strip()
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    if not mid.endswith(">"):
        mid = f"{mid}>"

    try:
        m, _ = _imap_connect(cfg)
        if m is None:
            return None, None

        folder, uid = _imap_find_uid(m, mid)
        if not uid:
            m.logout()
            return None, None

        m.select(folder, readonly=True)
        typ, data = m.fetch(uid, "(BODY.PEEK[])")
        m.logout()

        if typ != "OK" or not data or data[0] is None:
            return None, None

        parsed = _parse_message(data[0][1])
        from_hdr = parsed["from_raw"]
        date_hdr = parsed["date"]
        subj_hdr = parsed["subject"]
        plain_body = parsed["body_plain"]
        html_body  = parsed["body_html"]

        attr_plain = (
            f"-----Original Message-----\n"
            f"From: {from_hdr}\n"
            f"Sent: {date_hdr}\n"
            f"Subject: {subj_hdr}"
        )
        quoted_plain = f"\n\n{attr_plain}\n\n{plain_body.strip()}" if plain_body else f"\n\n{attr_plain}"

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
            f'{attr_html}{inner}</div>'
        )
        return quoted_plain, quoted_html

    except Exception as e:
        logger.warning(f"IMAP quote fetch failed: {e}")
        return None, None


# ---------------------------------------------------------------------------
# HTML rendering — two modes (unchanged from v1.1)
# ---------------------------------------------------------------------------

def _escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _validate_url(url):
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https', 'mailto')


_PRE_STYLE = (
    "display:block;background:#f8f8f8;color:#333;"
    "padding:8px 12px;border-radius:3px;border:1px solid #e0e0e0;"
    "font-family:'Courier New',Courier,monospace;font-size:12px;"
    "white-space:pre;overflow-x:auto;margin:8px 0;"
)


def _highlight_code(lang, code_raw):
    try:
        from pygments import highlight
        from pygments.lexers import get_lexer_by_name, TextLexer
        from pygments.formatters import HtmlFormatter
        try:
            lexer = get_lexer_by_name(lang) if lang else TextLexer()
        except Exception:
            lexer = TextLexer()
        formatter = HtmlFormatter(style="friendly", noclasses=True, nowrap=True)
        highlighted = highlight(code_raw, lexer, formatter)
        return f'<pre style="{_PRE_STYLE}">{highlighted}</pre>'
    except ImportError:
        code_esc = code_raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<pre style="{_PRE_STYLE}"><code>{code_esc}</code></pre>'


def _md_to_html_simple(text):
    _blocks = []

    def _extract_fence(m):
        lang = m.group(1) or ""
        code_raw = m.group(2)
        rendered = _highlight_code(lang, code_raw)
        idx = len(_blocks)
        _blocks.append(rendered)
        return f"\n\n\x00WCODE{idx}\x00\n\n"

    text = re.sub(r"```(\w*)\n(.*?)```", _extract_fence, text, flags=re.DOTALL)
    html = _escape_html(text)

    html = re.sub(r"^### (.+)$",
        r'<h3 style="font-family:Aptos,Calibri,Arial,sans-serif;font-size:14pt;margin:16px 0 4px 0;">\1</h3>\n',
        html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$",
        r'<h2 style="font-family:Aptos,Calibri,Arial,sans-serif;font-size:16pt;margin:20px 0 6px 0;">\1</h2>\n',
        html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$",
        r'<h1 style="font-family:Aptos,Calibri,Arial,sans-serif;font-size:20pt;margin:24px 0 8px 0;">\1</h1>\n',
        html, flags=re.MULTILINE)

    html = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", html)
    html = re.sub(r"\*\*(.+?)\*\*",     r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*",         r"<em>\1</em>", html)

    html = re.sub(
        r"`(.+?)`",
        r'<code style="background:#f5f5f5;padding:1px 4px;border-radius:2px;'
        r'font-family:\'Courier New\',Courier,monospace;font-size:0.9em;">\1</code>',
        html
    )

    def _link(m):
        link_text = m.group(1)
        url = m.group(2).replace('&quot;', '"')
        if not _validate_url(url):
            return link_text
        url = url.replace('"', '&quot;')
        return f'<a href="{url}" style="color:#0066cc;" rel="noopener noreferrer">{link_text}</a>'
    html = re.sub(r"\[(.+?)\]\((.+?)\)", _link, html)

    html = re.sub(r"^---+$",
        r'<hr style="border:none;border-top:1px solid #ddd;margin:16px 0;">',
        html, flags=re.MULTILINE)

    def _quoteblock(m):
        lines = re.findall(r"^&gt; ?(.*)$", m.group(0), re.MULTILINE)
        inner = "<br>\n".join(lines)
        return (
            f'<blockquote style="border-left:3px solid #ccc;margin:8px 0;'
            f'padding:4px 0 4px 12px;color:#555;">{inner}</blockquote>'
        )
    html = re.sub(r"(^&gt;.*\n?)+", _quoteblock, html, flags=re.MULTILINE)

    def _ul_block(m):
        items = re.findall(r"^[-*] (.+)$", m.group(0), re.MULTILINE)
        lis = "".join(f'<li style="margin:2px 0;">{i}</li>' for i in items)
        return f'<ul style="margin:8px 0;padding-left:20px;">{lis}</ul>'
    html = re.sub(r"(^[-*] .+\n?)+", _ul_block, html, flags=re.MULTILINE)

    def _ol_block(m):
        items = re.findall(r"^\d+\. (.+)$", m.group(0), re.MULTILINE)
        lis = "".join(f'<li style="margin:2px 0;">{i}</li>' for i in items)
        return f'<ol style="margin:8px 0;padding-left:20px;">{lis}</ul>'
    html = re.sub(r"(^\d+\. .+\n?)+", _ol_block, html, flags=re.MULTILINE)

    _p_style = (
        'margin:0 0 10px 0;font-family:Aptos,Calibri,Arial,sans-serif;'
        'font-size:12pt;color:#000;'
    )
    paragraphs = re.split(r"\n{2,}", html.strip())
    wrapped = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
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
    return (
        '<!DOCTYPE html><html><body style="font-family:Aptos,Calibri,Arial,sans-serif;'
        'font-size:12pt;color:#000;max-width:700px;">\n'
        + body_html
        + "\n</body></html>"
    )


def _md_to_html_rich(text):
    import markdown as md_lib
    extensions = ["extra", "codehilite", "tables", "fenced_code", "nl2br"]
    ext_configs = {"codehilite": {"noclasses": True, "guess_lang": False}}
    try:
        html = md_lib.markdown(text, extensions=extensions, extension_configs=ext_configs)
    except Exception as e:
        logger.warning(f"Rich markdown rendering failed: {e}, falling back to simple")
        html = md_lib.markdown(text)
    html = re.sub(
        r'(<div class="codehilite")\s+(style="([^"]*)")',
        lambda m: f'{m.group(1)} style="{m.group(3).rstrip(";")};padding:10px 14px;border-radius:4px;"',
        html,
    )
    return html


def _wrap_html_rich(body_html):
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


def _md_to_plain(text):
    return text.strip()


# ---------------------------------------------------------------------------
# Public: send_email (unchanged API from v1.1)
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

    When in_reply_to is provided and WAGGLE_IMAP_HOST is configured, waggle
    automatically fetches the original message and appends a quoted block.

    Args:
        to:           Recipient address or "Name <addr>" string.
        subject:      Subject line.
        body_md:      Message body in Markdown.
        cc:           Optional CC address(es), comma-separated.
        reply_to:     Optional Reply-To address.
        in_reply_to:  Message-ID of email being replied to (enables threading + auto-quote).
        references:   References header (threading chain).
        from_name:    Display name for From header.
        attachments:  List of file paths to attach.
        rich:         Enable rich HTML rendering (opt-in, stripped by Gmail).
        config:       Config dict (falls back to env vars).
    """
    cfg = _build_cfg(config)

    quoted_plain = quoted_html = None
    if in_reply_to:
        quoted_plain, quoted_html = fetch_quoted_body(in_reply_to, config=cfg)

    subject     = _sanitize_header(subject)
    to          = _sanitize_header(to)
    cc          = _sanitize_header(cc)
    reply_to    = _sanitize_header(reply_to)
    in_reply_to = _sanitize_header(in_reply_to)
    references  = _sanitize_header(references)
    name        = _sanitize_header(from_name or cfg["from_name"])

    from_header   = formataddr((name, cfg["from_addr"])) if name else cfg["from_addr"]
    envelope_from = _validate_email(cfg["from_addr"])
    envelope_to   = [_validate_email(to)]

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

    if quoted_plain:
        plain += quoted_plain
    if quoted_html:
        html_full = html_full.replace("</body>", f"\n{quoted_html}\n</body>")

    alt = MIMEMultipart("alternative")
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
    if cfg["tls"]:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx) as s:
            if cfg["user"] and cfg["password"]:
                s.login(cfg["user"], cfg["password"])
            s.sendmail(envelope_from, envelope_to, msg.as_string())
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
            s.ehlo(); s.starttls(context=ctx); s.ehlo()
            if cfg["user"] and cfg["password"]:
                s.login(cfg["user"], cfg["password"])
            s.sendmail(envelope_from, envelope_to, msg.as_string())

    # Log the send so check_recently_sent() can detect duplicates
    _log_sent(to, subject)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_size(n):
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n//1024}K"
    return f"{n//(1024*1024)}M"


def _cli_list(args):
    msgs = list_inbox(folder=args.folder, limit=args.limit)
    if not msgs:
        print(f"(no messages in {args.folder})")
        return
    print(f"{'UID':<6} {'UNREAD':<7} {'FROM':<30} {'SUBJECT':<45} {'DATE'}")
    print("-" * 110)
    for m in msgs:
        unread = "●" if m["unread"] else " "
        frm    = (m["from_name"] or m["from_addr"])[:28]
        subj   = m["subject"][:43]
        date   = m["date"][:20] if m["date"] else ""
        print(f"{m['uid']:<6} {unread:<7} {frm:<30} {subj:<45} {date}")


def _cli_read(args):
    msg = read_message(args.uid, folder=args.folder)
    print("=" * 70)
    print(f"From:    {msg['from_raw']}")
    print(f"To:      {msg['to']}")
    print(f"Date:    {msg['date']}")
    print(f"Subject: {msg['subject']}")
    if msg["attachments"]:
        att_list = ", ".join(
            f"{a['filename']} ({_fmt_size(a['size'])})"
            for a in msg["attachments"]
        )
        print(f"Attach:  {att_list}")
    print("=" * 70)
    print(msg["body_plain"] or "(no plain text body)")
    print()
    print("─" * 70)
    print("THREADING — use these for waggle send_email() reply:")
    print(f"  message_id       = {msg['message_id'] or '(none)'}")
    print(f"  reply_references = {msg['reply_references'] or '(none)'}")
    print(f"  reply_subject    = {msg['reply_subject']}")
    print()
    print("PYTHON REPLY TEMPLATE:")
    print("  import sys")
    print("  sys.path.insert(0, '/home/jason/.openclaw/workspace/projects/waggle')")
    print("  from waggle import send_email, move_message")
    print("  send_email(")
    print(f'      to="{msg["from_addr"]}",')
    print(f'      subject="{msg["reply_subject"]}",')
    print(f'      body_md="""YOUR REPLY HERE""",')
    print(f'      in_reply_to="{msg["message_id"]}",')
    print(f'      references="{msg["reply_references"]}",')
    print(f'      from_name="Sam",')
    print(f'  )')
    print(f'  move_message("{msg["uid"]}", "INBOX.Processed", "{msg["folder"]}")')
    print("─" * 70)


def _cli_move(args):
    move_message(args.uid, args.dest, src_folder=args.folder)
    print(f"✅ Moved {args.uid} from {args.folder} → {args.dest}")


def _cli_attach(args):
    dest = args.dest or "./attachments"
    paths = download_attachments(args.uid, folder=args.folder, dest_dir=dest)
    if paths:
        print(f"✅ Downloaded {len(paths)} attachment(s) to {dest}:")
        for p in paths:
            print(f"   {p}")
    else:
        print("(no attachments found)")


def _cli_send(args):
    send_email(
        to=args.to,
        subject=args.subject,
        body_md=args.body,
        cc=args.cc,
        reply_to=getattr(args, "reply_to", None),
        in_reply_to=getattr(args, "in_reply_to", None),
        references=getattr(args, "references", None),
        from_name=getattr(args, "from_name", None),
        attachments=getattr(args, "attach", None),
        rich=getattr(args, "rich", False),
    )
    print(f"✅ Sent to {args.to}")


def cli_main():
    return main()


def main():
    parser = argparse.ArgumentParser(
        prog="waggle",
        description="waggle — full email client for AI agents (IMAP + SMTP)",
    )
    parser.add_argument("--version", action="version", version=f"waggle {__version__}")
    sub = parser.add_subparsers(dest="command")

    # --- list ---
    p_list = sub.add_parser("list", help="List inbox envelopes")
    p_list.add_argument("--folder", default="INBOX", help="IMAP folder (default: INBOX)")
    p_list.add_argument("--limit",  type=int, default=20, help="Max messages (default: 20)")

    # --- read ---
    p_read = sub.add_parser("read", help="Read a message (body + threading headers)")
    p_read.add_argument("uid", help="IMAP sequence number")
    p_read.add_argument("--folder", default="INBOX", help="IMAP folder (default: INBOX)")

    # --- move ---
    p_move = sub.add_parser("move", help="Move a message to another folder")
    p_move.add_argument("uid",  help="IMAP sequence number")
    p_move.add_argument("dest", help="Destination folder (e.g. INBOX.Processed)")
    p_move.add_argument("--folder", default="INBOX", help="Source folder (default: INBOX)")

    # --- attach ---
    p_att = sub.add_parser("attach", help="Download attachments from a message")
    p_att.add_argument("uid", help="IMAP sequence number")
    p_att.add_argument("--folder", default="INBOX", help="IMAP folder (default: INBOX)")
    p_att.add_argument("--dest",   default="./attachments", help="Destination directory")

    # --- send ---
    p_send = sub.add_parser("send", help="Send an email")
    p_send.add_argument("--to",          required=True)
    p_send.add_argument("--subject",     required=True)
    p_send.add_argument("--body",        required=True, help="Markdown body")
    p_send.add_argument("--cc",          default=None)
    p_send.add_argument("--reply-to",    default=None)
    p_send.add_argument("--from-name",   default=None)
    p_send.add_argument("--in-reply-to", default=None,
                        help="Message-ID to reply to (enables threading + auto-quote)")
    p_send.add_argument("--references",  default=None)
    p_send.add_argument("--attach",      action="append", default=None, metavar="FILE")
    p_send.add_argument("--rich",        action="store_true", default=False)

    args = parser.parse_args()

    dispatch = {
        "list":   _cli_list,
        "read":   _cli_read,
        "move":   _cli_move,
        "attach": _cli_attach,
        "send":   _cli_send,
    }

    if args.command in dispatch:
        dispatch[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
