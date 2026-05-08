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

    from waggle import send_email, reply_all, reply, list_inbox, read_message, move_message, download_attachments, set_flags, clear_flags

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

    # Flag management
    set_flags("42", [r"\\Seen", r"\\Flagged"])
    clear_flags("42", [r"\\Flagged"])

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

    WAGGLE_IMAP_HOST IMAP host (default: WAGGLE_HOST)
    WAGGLE_IMAP_PORT IMAP port (default: 993)
    WAGGLE_IMAP_TLS  Use IMAP SSL (default: true)
                     WAGGLE_USER / WAGGLE_PASS are reused for IMAP auth.
"""

__version__ = "1.9.13"

import html
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
import tempfile
import secrets
import html
import json
import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Duplicate reply guard
# ---------------------------------------------------------------------------

_REPLY_DB_PATH = Path.home() / '.openclaw' / 'waggle-replied.json'
_REPLY_DB_MAX_DAYS = 30  # prune entries older than this


def _load_reply_db():
    """Load the replied Message-ID database."""
    try:
        if _REPLY_DB_PATH.exists():
            return json.loads(_REPLY_DB_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_reply_db(db):
    """Save the replied Message-ID database, pruning old entries."""
    try:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=_REPLY_DB_MAX_DAYS)).isoformat()
        pruned = {mid: ts for mid, ts in db.items() if ts >= cutoff}
        _REPLY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REPLY_DB_PATH.write_text(json.dumps(pruned, indent=2))
    except Exception:
        pass


def check_already_replied(message_id):
    """
    Check if waggle has already sent a reply to the given Message-ID.

    Returns (bool, timestamp_str_or_None)
    """
    if not message_id:
        return False, None
    db = _load_reply_db()
    mid = message_id.strip()
    if mid in db:
        return True, db[mid]
    return False, None


def _mark_replied(message_id):
    """Record that we replied to this Message-ID."""
    if not message_id:
        return
    db = _load_reply_db()
    db[message_id.strip()] = datetime.datetime.now().isoformat()
    _save_reply_db(db)
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from email.charset import Charset, QP
from email.utils import parseaddr, formataddr, getaddresses
from urllib.parse import urlparse

# Security: Size limits for attachment downloads
MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024  # 50MB per file
MAX_TOTAL_ATTACHMENT_SIZE = 200 * 1024 * 1024  # 200MB per message

# IMAP flag whitelist (RFC 3501 section 2.3.2 system flags)
ALLOWED_FLAGS = frozenset({
    r"\Seen",
    r"\Answered",
    r"\Flagged",
    r"\Draft",
    r"\Deleted",
})

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
    # Unfold RFC 2822 folded headers (CRLF + whitespace → single space)
    value = re.sub(r'\r\n[ \t]+', ' ', str(value))
    value = re.sub(r'\n[ \t]+', ' ', value)
    if re.search(r'[\r\n]', value):
        raise ValueError(f"Header value contains illegal newline characters: {value!r}")
    return value


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


def _validate_folder_name(folder):
    """
    Validate IMAP folder name to prevent command injection.

    Per RFC 3501, folder names can contain most characters, but we restrict
    dangerous ones that could be misinterpreted in IMAP commands.
    """
    if not folder:
        raise ValueError("Empty folder name")
    # Reject control characters and IMAP special chars
    if re.search(r'[\r\n\x00-\x1f]', folder):
        raise ValueError(f"Folder name contains control characters: {folder!r}")
    return folder


def _imap_quote_folder(folder):
    """
    Quote an IMAP folder name per RFC 3501 if it contains spaces or special
    characters that would otherwise be misinterpreted by the server.
    Already-quoted names (surrounded by double-quotes) are returned as-is.
    """
    if not folder:
        return folder
    # Already quoted
    if folder.startswith('"') and folder.endswith('"'):
        return folder
    # Quote if contains space or other chars that need quoting
    if ' ' in folder or any(c in folder for c in ('(', ')', '{', '%', '*', '\\')):
        return f'"{folder}"'
    return folder


def _validate_flags(flags):
    """
    Validate flag list against whitelist.

    Raises:
        ValueError: Empty flags list or unknown flag.
    """
    if not flags:
        raise ValueError("flags list cannot be empty")
    for flag in flags:
        if flag not in ALLOWED_FLAGS:
            raise ValueError(f"Unknown IMAP flag: {flag!r}")


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
        "tls":         cfg.get("tls", os.environ.get("WAGGLE_TLS", "true").lower() != "false"),
        "smtp_starttls": cfg.get("smtp_starttls", os.environ.get("WAGGLE_SMTP_STARTTLS", "true").lower() != "false"),
        "sent_folder":  cfg.get("sent_folder") or os.environ.get("WAGGLE_SENT_FOLDER", ""),
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


# Try common Sent folder names in order of modern prevalence
_SENT_FOLDER_CANDIDATES = (
    "Sent",        # Gmail, modern IMAP servers
    "Sent Items",  # Outlook / Exchange
    "INBOX.Sent",  # Courier, older hierarchical IMAP
)


def _imap_append_sent(cfg, msg_bytes, folder=None):
    """
    Append a sent message to the IMAP Sent folder.

    Args:
        cfg:       Config dict from _build_cfg().
        msg_bytes: RFC822 message as bytes.
        folder:    Explicit folder name, or None for auto-detect.

    Returns the folder name used, or None on failure.
    """
    if not msg_bytes or not isinstance(msg_bytes, bytes):
        logger.warning("Invalid message bytes for IMAP append")
        return None

    m, _ = _imap_connect(cfg)
    if m is None:
        return None

    try:
        if folder:
            _validate_folder_name(folder)
            quoted = _imap_quote_folder(folder)
            try:
                status, _ = m.append(quoted, r"(\Seen)", None, msg_bytes)
                if status == "OK":
                    return folder
            except Exception as exc:
                logger.warning(f"IMAP append to {folder!r} failed: {exc}")
                return None

        # RFC 6154: try to find Sent folder via \Sent special-use flag first
        special_sent = None
        try:
            typ, folder_list = m.list()
            if typ == "OK":
                for item in folder_list:
                    if item is None:
                        continue
                    decoded = item.decode() if isinstance(item, bytes) else item
                    if r"\Sent" in decoded or "\\Sent" in decoded:
                        # Parse folder name — last token after separator
                        parts = decoded.rsplit('"', 1)
                        if len(parts) == 2:
                            special_sent = parts[-1].strip().strip('"')
                        else:
                            # unquoted name
                            special_sent = decoded.rsplit(None, 1)[-1].strip()
                        break
        except Exception as exc:
            logger.debug(f"Special-use flag detection failed: {exc}")

        candidates = ([special_sent] if special_sent else []) + list(_SENT_FOLDER_CANDIDATES)
        for candidate in candidates:
            try:
                quoted = _imap_quote_folder(candidate)
                status, _ = m.select(quoted, readonly=True)
                if status != "OK":
                    continue
                try:
                    m.close()
                except Exception:
                    pass
                status, _ = m.append(quoted, r"(\Seen)", None, msg_bytes)
                if status == "OK":
                    return candidate
            except Exception:
                continue

        logger.warning("No suitable Sent folder found for IMAP append")
        return None
    finally:
        try:
            m.logout()
        except Exception:
            pass


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
            status, _ = m.select(_imap_quote_folder(folder), readonly=True)
            if status != "OK":
                continue
            # Try without angle brackets first (Stalwart indexes without <>)
            mid_search = mid.strip("<>")
            status, data = m.uid("SEARCH", None, f'HEADER Message-ID "{mid_search}"')
            # Fallback: try with brackets (standard servers)
            if status != "OK" or not data or not data[0]:
                status, data = m.uid("SEARCH", None, f'HEADER Message-ID "{mid}"')
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
    cc          = _decode_header_str(msg.get("Cc", ""))

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

    # Build reply_cc: everyone on To + Cc except the original sender (they go in To)
    # Caller should also remove their own address before sending.
    _all_recipients = []
    for field in (to, cc):
        if field:
            for name_part, addr_part in getaddresses([field]):
                addr_part = addr_part.strip().lower()
                if addr_part and addr_part != from_addr_p.lower():
                    _all_recipients.append(addr_part)
    # Deduplicate while preserving order
    _seen = set()
    reply_cc_list = []
    for addr in _all_recipients:
        if addr not in _seen:
            _seen.add(addr)
            reply_cc_list.append(addr)
    reply_cc = ", ".join(reply_cc_list)  # comma-separated, ready for send_email(cc=...)

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
        "cc":               cc,
        "reply_cc":         reply_cc,
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
        _validate_folder_name(folder)
        status, _ = m.select(_imap_quote_folder(folder), readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not select folder: {folder!r}")

        status, data = m.uid("SEARCH", None, "ALL")
        if status != "OK" or not data[0]:
            return []

        uids = data[0].split()
        # Most recent last — take the last `limit` and reverse
        uids = uids[-limit:][::-1]

        results = []
        for uid in uids:
            try:
                _, msg_data = m.uid("FETCH", uid, "(FLAGS RFC822.SIZE BODY[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
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


def read_message(uid, folder="INBOX", mark_read=False, config=None):
    """
    Read a full email message by IMAP UID.

    Returns a structured dict with body, headers, threading info, and attachment list.
    Use msg["message_id"] and msg["reply_references"] for waggle send_email().

    Args:
        uid:       IMAP UID (stable integer, as returned by list_inbox/search_messages)
        folder:    IMAP folder (default: INBOX)
        mark_read: If True, mark the message as read (\\Seen) on the server.
                   Default False preserves existing behavior (BODY.PEEK[], readonly=True).
        config:    Optional config dict

    Returns dict keys:
        uid, folder, message_id, references, in_reply_to, reply_references,
        reply_subject, from_addr, from_name, from_raw, to, cc, reply_cc,
        subject, date, body_plain, body_html, attachments (list of {filename, content_type, size})

        reply_cc: comma-separated addresses of all original recipients except the
        sender — ready to pass directly to send_email(cc=msg['reply_cc']).
        Or just use reply_all(msg, body_md) and waggle handles it automatically.

    Note: attachments list contains metadata only — call download_attachments()
    to save files to disk.
    """
    if not isinstance(mark_read, bool):
        raise TypeError(f"mark_read must be bool, got {type(mark_read).__name__}")

    cfg = _build_cfg(config)

    if mark_read and not cfg.get("imap_host"):
        logger.warning("mark_read=True ignored: IMAP not configured")
        mark_read = False

    m, _ = _imap_connect(cfg)
    if m is None:
        raise RuntimeError("IMAP not configured — set WAGGLE_IMAP_HOST")

    try:
        _validate_folder_name(folder)
        status, _ = m.select(_imap_quote_folder(folder), readonly=not mark_read)
        if status != "OK":
            raise RuntimeError(f"Could not select folder: {folder!r}")

        uid_bytes = str(uid).encode() if not isinstance(uid, bytes) else uid
        fetch_cmd = "(BODY[])" if mark_read else "(BODY.PEEK[])"
        typ, data = m.uid("FETCH", uid_bytes, fetch_cmd)
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


def search_messages(query, folders=None, limit=20, config=None):
    """
    Search for messages across one or more folders by FROM, SUBJECT, or TEXT.

    Returns a list of dicts (same shape as list_inbox) with an extra "folder" key
    so the caller always knows which folder the UID belongs to.

    Args:
        query:   dict with one or more IMAP criteria keys:
                   from_addr  → search FROM header  (e.g. "jamie@coder.com")
                   subject    → search SUBJECT header (e.g. "Tavant")
                   text       → full-text BODY search
                   since      → date string e.g. "17-Apr-2026"
                   unseen     → True  → only unread messages
        folders: list of folder names to search (default: ["INBOX", "INBOX.Processed"])
        limit:   max results per folder (default: 20)
        config:  optional config dict

    Returns list of message envelope dicts with added key:
        folder   → the IMAP folder the message lives in
    """
    cfg = _build_cfg(config)
    m, _ = _imap_connect(cfg)
    if m is None:
        raise RuntimeError("IMAP not configured — set WAGGLE_IMAP_HOST")

    if folders is None:
        folders = ["INBOX", "INBOX.Processed"]

    # Build IMAP search criteria list
    criteria = []
    if query.get("unseen"):
        criteria.append("UNSEEN")
    if query.get("from_addr"):
        criteria += ["FROM", f'"{query["from_addr"]}"']
    if query.get("subject"):
        criteria += ["SUBJECT", f'"{query["subject"]}"']
    if query.get("text"):
        criteria += ["TEXT", f'"{query["text"]}"']
    if query.get("since"):
        criteria += ["SINCE", query["since"]]
    if not criteria:
        criteria = ["ALL"]

    results = []
    try:
        for folder in folders:
            try:
                _validate_folder_name(folder)
                status, _ = m.select(_imap_quote_folder(folder), readonly=True)
                if status != "OK":
                    continue

                status, data = m.uid("SEARCH", None, *criteria)
                if status != "OK" or not data[0]:
                    continue

                uids = data[0].split()
                uids = uids[-limit:][::-1]  # most recent first

                for uid in uids:
                    try:
                        _, msg_data = m.uid("FETCH", uid, "(FLAGS RFC822.SIZE BODY[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
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
                            "folder":     folder,
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
                        logger.warning(f"Error fetching envelope for uid {uid} in {folder}: {e}")
                        continue
            except Exception as e:
                logger.warning(f"Error searching folder {folder!r}: {e}")
                continue
    finally:
        try:
            m.logout()
        except Exception:
            pass

    return results


def move_message(uid, dest_folder, src_folder="INBOX", config=None):
    """
    Move a message from src_folder to dest_folder.

    Uses UID COPY + UID STORE + EXPUNGE — stable, immune to concurrent folder changes.
    Use this after sending a reply: move_message("42", "INBOX.Processed")

    Args:
        uid:         IMAP UID (as returned by list_inbox/search_messages/read_message)
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
        # Validate folder names to prevent IMAP injection
        _validate_folder_name(src_folder)
        _validate_folder_name(dest_folder)
        
        status, _ = m.select(_imap_quote_folder(src_folder))
        if status != "OK":
            raise RuntimeError(f"Could not select folder: {src_folder!r}")

        # uid is already a real UID (as returned by list_inbox/search_messages)
        uid_bytes = str(uid).encode() if not isinstance(uid, bytes) else uid

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


def _modify_flags(uid, flags, action, folder, config, operation_name):
    """
    Internal helper for flag modifications.

    Args:
        uid:            Message UID or comma-separated UIDs.
        flags:          List of IMAP flags.
        action:         "+FLAGS" or "-FLAGS".
        folder:         IMAP folder name.
        config:         Config dict.
        operation_name: Function name for error messages.

    Returns:
        True on success.

    Raises:
        ValueError:   Invalid flag, UID, or folder.
        RuntimeError: IMAP not configured or STORE failed.
    """
    _validate_flags(flags)

    # Validate each UID in comma-separated list
    uid_list = []
    for part in str(uid).split(","):
        part = part.strip()
        if not part.isdigit() or int(part) <= 0:
            raise ValueError(f"Invalid UID: {part!r}")
        uid_list.append(part)

    cfg = _build_cfg(config)
    m, _ = _imap_connect(cfg)
    if m is None:
        raise RuntimeError("IMAP not configured — set WAGGLE_IMAP_HOST")

    try:
        _validate_folder_name(folder)
        status, _ = m.select(_imap_quote_folder(folder))
        if status != "OK":
            raise RuntimeError(f"Could not select folder: {folder!r}")

        flag_str = "(%s)" % " ".join(flags)

        for uid_str in uid_list:
            uid_bytes = uid_str.encode()
            status, _ = m.uid("STORE", uid_bytes, action, flag_str)
            if status != "OK":
                raise RuntimeError(f"{operation_name} failed for UID {uid_str}: {status}")

        return True
    finally:
        try:
            m.logout()
        except Exception:
            pass


def set_flags(uid, flags, folder="INBOX", config=None):
    """
    Add IMAP flags to a message.

    Args:
        uid:    Message UID (str, as returned by list_inbox/read_message).
                Comma-separated UIDs for bulk operations (e.g. "42,43,44").
        flags:  List of IMAP flags to add (e.g. [r"\\Seen", r"\\Flagged"]).
        folder: IMAP folder (default: INBOX).
        config: Optional config dict (falls back to env vars).

    Returns:
        True on success. For bulk UIDs, all operations must succeed.

    Raises:
        ValueError:    Unknown flag, invalid UID, or empty flags list.
        RuntimeError:  IMAP not configured or STORE command failed.
                       For bulk operations, stops at the first failure (fail-fast).
    """
    return _modify_flags(uid, flags, "+FLAGS", folder, config, "set_flags")


def clear_flags(uid, flags, folder="INBOX", config=None):
    """
    Remove IMAP flags from a message.

    Args:
        uid:    Message UID (str, as returned by list_inbox/read_message).
                Comma-separated UIDs for bulk operations (e.g. "42,43,44").
        flags:  List of IMAP flags to remove (e.g. [r"\\Seen", r"\\Flagged"]).
        folder: IMAP folder (default: INBOX).
        config: Optional config dict (falls back to env vars).

    Returns:
        True on success. For bulk UIDs, all operations must succeed.

    Raises:
        ValueError:    Unknown flag, invalid UID, or empty flags list.
        RuntimeError:  IMAP not configured or STORE command failed.
                       For bulk operations, stops at the first failure (fail-fast).
    """
    return _modify_flags(uid, flags, "-FLAGS", folder, config, "clear_flags")


def download_attachments(uid, folder="INBOX", dest_dir=".", config=None):
    """
    Download all attachments from a message to dest_dir.

    Security considerations:
    - Filename sanitization prevents path traversal (dots collapsed)
    - Symlink validation prevents redirection attacks
    - Size limits prevent DoS from large attachments
    - Atomic writes prevent partial file corruption
    - Random suffixes prevent collision attacks

    Args:
        uid:      IMAP UID (as returned by list_inbox/search_messages)
        folder:   IMAP folder (default: INBOX)
        dest_dir: Directory to save files (created if needed)
        config:   Optional config dict

    Returns list of saved file paths (strings).
    """
    cfg = _build_cfg(config)
    m, _ = _imap_connect(cfg)
    if m is None:
        raise RuntimeError("IMAP not configured — set WAGGLE_IMAP_HOST")

    # Resolve and validate destination path (prevents symlink attacks)
    dest = Path(dest_dir).resolve(strict=False)
    
    # Check for symlinks in the path chain
    current = dest
    while current != current.parent:
        if current.is_symlink():
            raise ValueError(f"Destination path contains symlink: {current}")
        current = current.parent
    
    dest.mkdir(parents=True, exist_ok=True)
    
    # Validate we can actually write to this directory
    if not os.access(dest, os.W_OK):
        raise PermissionError(f"Cannot write to destination: {dest}")

    saved_paths = []
    total_size = 0

    try:
        _validate_folder_name(folder)
        status, _ = m.select(_imap_quote_folder(folder), readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not select folder: {folder!r}")

        uid_bytes = str(uid).encode() if not isinstance(uid, bytes) else uid
        typ, data = m.uid("FETCH", uid_bytes, "(BODY.PEEK[])")
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

            # Enforce per-attachment size limit
            if len(payload) > MAX_ATTACHMENT_SIZE:
                logger.warning(
                    f"Skipping attachment: size {len(payload)} exceeds limit "
                    f"{MAX_ATTACHMENT_SIZE}"
                )
                continue

            # Enforce total message limit
            if total_size + len(payload) > MAX_TOTAL_ATTACHMENT_SIZE:
                logger.warning(
                    f"Skipping remaining attachments: would exceed total limit "
                    f"{MAX_TOTAL_ATTACHMENT_SIZE}"
                )
                break

            total_size += len(payload)

            if not fn:
                ext = mimetypes.guess_extension(ct) or ".bin"
                fn = f"attachment_{i}{ext}"

            # Sanitize filename: collapse path separators and dots
            # Replace path separators first, then handle dot sequences
            fn = fn.replace('/', '_').replace('\\', '_')
            fn = re.sub(r'\.\.+', '_', fn)  # Collapse .. sequences
            fn = re.sub(r'[^\w\-_\. ]', '_', fn)
            
            # Ensure filename doesn't start with . (hidden files)
            fn = fn.lstrip('.')
            
            # Limit filename length (255 is max on most filesystems)
            if len(fn) > 255:
                stem = Path(fn[:250]).stem
                suffix = Path(fn).suffix[:5]
                fn = f"{stem}{suffix}"
            
            # Ensure we have something left after sanitization
            if not fn:
                fn = f"attachment_{i}.bin"

            out_path = dest / fn

            # Avoid overwrites with random suffix (prevents collision attacks)
            counter = 0
            while out_path.exists() or out_path.is_symlink():
                if counter > 100:  # Prevent infinite loop
                    raise RuntimeError(f"Cannot find unique filename for {fn}")
                stem = Path(fn).stem
                suffix = Path(fn).suffix
                random_suffix = secrets.token_hex(4)
                out_path = dest / f"{stem}_{random_suffix}{suffix}"
                counter += 1

            # Atomic write: temp file then rename
            tmp_fd = None
            tmp_path = None
            try:
                tmp_fd, tmp_path = tempfile.mkstemp(dir=dest, prefix=".waggle_tmp_")
                os.write(tmp_fd, payload)
                os.close(tmp_fd)
                tmp_fd = None
                
                # Final symlink check before rename
                if out_path.is_symlink():
                    raise ValueError(f"Refusing to overwrite symlink: {out_path}")
                
                # Atomic rename
                Path(tmp_path).rename(out_path)
                saved_paths.append(str(out_path))
            except Exception:
                # Cleanup on error
                if tmp_fd is not None:
                    try:
                        os.close(tmp_fd)
                    except:
                        pass
                if tmp_path:
                    try:
                        Path(tmp_path).unlink(missing_ok=True)
                    except:
                        pass
                raise

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
    """Append a send record to the local sent log.
    
    Sanitizes log entry to prevent format corruption from malicious inputs.
    Replaces | and newlines to preserve pipe-delimited format integrity.
    """
    import time
    _SEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    # Sanitize to preserve log format integrity
    to_clean = str(to).lower().replace('|', '').replace('\n', '').replace('\r', '').strip()
    subj_clean = str(subject).lower().replace('|', '').replace('\n', '').replace('\r', '').strip()
    with open(_SEND_LOG, "a") as f:
        f.write(f"{int(time.time())}|{to_clean}|{subj_clean}\n")


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
    Fetch the original message and return (quoted_plain, quoted_html).

    Lookup order:
    1. Maildir (if configured via config["maildir_path"] or WAGGLE_MAILDIR)
    2. IMAP   (if configured via WAGGLE_IMAP_HOST or WAGGLE_HOST)

    If Maildir is configured and the message is found there, IMAP is skipped
    entirely. If Maildir is configured but the message isn't found, falls
    through to IMAP. Returns (None, None) if neither source has the message.

    Outlook-style quoting:
    - plain: -----Original Message----- block (snowballs naturally)
    - html:  left-border blockquote div (snowballs like Outlook)

    Searches all folders for IMAP — works after message is moved to INBOX.Processed.
    Returns (None, None) if neither source is configured or message not found.
    """
    cfg = _build_cfg(config)

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
                return None, None

            folder, uid = _imap_find_uid(m, mid)
            if not uid:
                m.logout()
                return None, None

            # Re-select the folder (find may have left us somewhere else)
            m.select(_imap_quote_folder(folder), readonly=True)
            typ, data = m.uid("FETCH", uid, "(BODY.PEEK[])")
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
    to_hdr   = str(make_header(decode_header(msg.get("To", "")    or "")))
    cc_hdr   = str(make_header(decode_header(msg.get("Cc", "")    or "")))

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
    attr_lines = [
        "-----Original Message-----",
        f"From: {from_hdr}",
        f"Sent: {date_hdr}",
    ]
    if to_hdr:
        attr_lines.append(f"To: {to_hdr}")
    if cc_hdr:
        attr_lines.append(f"Cc: {cc_hdr}")
    attr_lines.append(f"Subject: {subj_hdr}")
    attr_plain = "\n".join(attr_lines)
    if plain_body:
        quoted_plain = f"\n\n{attr_plain}\n\n{plain_body.strip()}"
    else:
        quoted_plain = f"\n\n{attr_plain}"

    # --- HTML quoted block (Outlook-style left-border blockquote) ---
    attr_html_parts = [
        f'<b>From:</b> {from_hdr}<br>',
        f'<b>Sent:</b> {date_hdr}<br>',
    ]
    if to_hdr:
        attr_html_parts.append(f'<b>To:</b> {to_hdr}<br>')
    if cc_hdr:
        attr_html_parts.append(f'<b>Cc:</b> {cc_hdr}<br>')
    attr_html_parts.append(f'<b>Subject:</b> {subj_hdr}')
    attr_html = (
        f'<p style="margin:0 0 8px 0;font-size:12px;color:#777;">'
        + "".join(attr_html_parts)
        + '</p>'
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

    def _table_block(m):
        lines = [l.strip() for l in m.group(0).strip().splitlines() if l.strip()]
        # Separate separator rows (e.g. |---|---|) from content rows
        content_rows = [l for l in lines if not re.match(r"^\|[-|: ]+\|$", l)]
        if not content_rows:
            return m.group(0)
        def parse_cells(line):
            return [c.strip() for c in line.strip().strip("|").split("|")]
        th_cells = parse_cells(content_rows[0])
        th_html = "".join(
            f'<th style="padding:6px 10px;text-align:left;border:1px solid #ccc;">{c}</th>'
            for c in th_cells
        )
        tbody_rows = ""
        for i, row in enumerate(content_rows[1:]):
            bg = "#f5f8ff" if i % 2 == 0 else "#ffffff"
            td_html = "".join(
                f'<td style="padding:6px 10px;border:1px solid #ddd;">{c}</td>'
                for c in parse_cells(row)
            )
            tbody_rows += f'<tr style="background:{bg};">{td_html}</tr>\n'
        return (
            '<table style="border-collapse:collapse;width:100%;margin:12px 0;'
            'font-family:Aptos,Calibri,Arial,sans-serif;font-size:11pt;">\n'
            f'<thead><tr style="background:#0d47a1;color:#fff;">{th_html}</tr></thead>\n'
            f'<tbody>{tbody_rows}</tbody></table>'
        )
    html = re.sub(r"(^\|.+\|[ \t]*\n?)+", _table_block, html, flags=re.MULTILINE)

    def _render_list(lines, base_indent=0):
        """Recursively render a block of list lines into nested HTML ol/ul."""
        html_out = []
        i = 0
        while i < len(lines):
            line = lines[i]
            raw_indent = len(line) - len(line.lstrip())
            indent = raw_indent - base_indent
            stripped = line.lstrip()
            ol_m = re.match(r'^(\d+)\. (.*)', stripped)
            ul_m = re.match(r'^[-*] (.*)', stripped)
            if not ol_m and not ul_m:
                i += 1
                continue
            is_ol = bool(ol_m)
            item_text = ol_m.group(2) if ol_m else ul_m.group(1)
            tag = 'ol' if is_ol else 'ul'
            # Collect children (lines more indented than current)
            children = []
            j = i + 1
            while j < len(lines):
                next_indent = len(lines[j]) - len(lines[j].lstrip())
                if next_indent > raw_indent:
                    children.append(lines[j])
                    j += 1
                else:
                    break
            child_html = _render_list(children, raw_indent + 2) if children else ''
            li_content = item_text + ('\n' + child_html if child_html else '')
            # Collect siblings at same indent + same list type
            siblings = [(tag, li_content)]
            i = j
            while i < len(lines):
                sline = lines[i]
                s_indent = len(sline) - len(sline.lstrip())
                if s_indent != raw_indent:
                    break
                s_stripped = sline.lstrip()
                s_ol = re.match(r'^(\d+)\. (.*)', s_stripped)
                s_ul = re.match(r'^[-*] (.*)', s_stripped)
                if not s_ol and not s_ul:
                    break
                s_is_ol = bool(s_ol)
                if s_is_ol != is_ol:
                    break
                s_text = s_ol.group(2) if s_ol else s_ul.group(1)
                s_children = []
                k = i + 1
                while k < len(lines):
                    nk_indent = len(lines[k]) - len(lines[k].lstrip())
                    if nk_indent > s_indent:
                        s_children.append(lines[k])
                        k += 1
                    else:
                        break
                s_child_html = _render_list(s_children, s_indent + 2) if s_children else ''
                s_li = s_text + ('\n' + s_child_html if s_child_html else '')
                siblings.append((tag, s_li))
                i = k
            margin = 'margin:8px 0' if indent == 0 else 'margin:2px 0'
            lis = ''.join(f'<li style="margin:2px 0;">{t}</li>' for _, t in siblings)
            html_out.append(f'<{tag} style="{margin};padding-left:20px;">{lis}</{tag}>')
        return '\n'.join(html_out)

    def _list_block(m):
        lines = m.group(0).rstrip('\n').split('\n')
        return '\n\n' + _render_list(lines) + '\n\n'
    html = re.sub(r'(^[ \t]*(?:\d+\.|[-*]) .+\n?)+', _list_block, html, flags=re.MULTILINE)

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
        elif re.match(r"<(h[1-6]|ul|ol|blockquote|hr|div|table|pre)[\s>]", p):
            wrapped.append(p)
        else:
            p = p.replace("\n", "<br>\n")
            wrapped.append(f'<p style="{_p_style}">{p}</p>')

    html_out = "\n".join(wrapped)
    return html_out


def _wrap_html_simple(body_html):
    return (
        '<!DOCTYPE html><html><body style="font-family:Aptos,Calibri,Arial,sans-serif;'
        'font-size:12pt;color:#000;max-width:700px;">\n'
        + body_html
        + "\n</body></html>"
    )


def _md_to_html_rich(text):
    import markdown as md_lib
    extensions = ["extra", "codehilite", "tables", "fenced_code"]
    ext_configs = {"codehilite": {"noclasses": True, "guess_lang": False}}
    try:
        html_out = md_lib.markdown(text, extensions=extensions, extension_configs=ext_configs)
    except Exception as e:
        logger.warning(f"Rich markdown rendering failed: {e}, falling back to simple")
    html_out = re.sub(
        r'(<div class="codehilite")\s+(style="([^"]*)")',
        lambda m: f'{m.group(1)} style="{m.group(3).rstrip(";")};padding:10px 14px;border-radius:4px;"',
        html_out,
    )
    return html_out


_DEFAULT_FONT_FAMILY = "Aptos, Calibri, Arial, sans-serif"


def _wrap_html_rich(body_html, font_family=None):
    ff = font_family or _DEFAULT_FONT_FAMILY
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: {ff}; font-size: 16px;
         color: #222; max-width: 680px; margin: 40px auto; padding: 0 24px; }}
  h1, h2, h3 {{ font-family: {ff}; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 32px 0; }}
  a {{ color: #2563eb; }}
  blockquote {{ border-left: 3px solid #ccc; margin: 0; padding: 0 0 0 16px;
                color: #555; font-style: italic; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #f4f4f4; }}
  pre {{ font-family:'SF Mono','Fira Code',Consolas,monospace; font-size:12px; }}
  ol, ul {{ margin: 8px 0 8px 0; padding-left: 24px; }}
  li {{ margin: 4px 0; }}
  ol ol, ul ul, ol ul, ul ol {{ margin: 2px 0; }}
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
    font_family=None,
    config=None,
    save_sent=True,
    sent_folder=None,
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
        font_family:  Optional CSS font-family string for rich HTML emails (e.g.
                      "Georgia, serif"). Defaults to "Aptos, Calibri, Arial, sans-serif".
                      Only used when rich=True.
        config:       Config dict (falls back to env vars).
        save_sent:    If True (default), append sent message to IMAP Sent folder.
                      Silently skips if IMAP is not configured.
        sent_folder:  Explicit IMAP Sent folder name, or None for auto-detect.
                      Auto-detection order: "Sent", "Sent Items", "INBOX.Sent".
    """
    if not isinstance(save_sent, bool):
        raise TypeError(f"save_sent must be bool, got {type(save_sent).__name__}")
    if sent_folder is not None and not isinstance(sent_folder, str):
        raise TypeError(f"sent_folder must be str or None, got {type(sent_folder).__name__}")
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
        html_full = _wrap_html_rich(html_body_rendered, font_family=font_family)
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
            s.ehlo()
            if cfg.get("smtp_starttls", True) and s.has_extn("STARTTLS"):
                s.starttls(context=ctx)
                s.ehlo()
            if cfg["user"] and cfg["password"]:
                s.login(cfg["user"], cfg["password"])
            s.sendmail(envelope_from, envelope_to, msg.as_string())

    # IMAP Sent folder sync — save sent message server-side
    if save_sent and cfg.get("imap_host"):
        try:
            folder = sent_folder or cfg.get("sent_folder") or None
            _imap_append_sent(cfg, msg.as_bytes(), folder=folder)
        except Exception as exc:
            logger.warning(f"Sent folder append failed (SMTP succeeded): {exc}")

    # Log the send so check_recently_sent() can detect duplicates
    _log_sent(to, subject)


def reply_all(msg, body_md, *, from_name=None, attachments=None, rich=False, config=None, force=False):
    """
    Reply-all to a message returned by read_message().

    Automatically:
    - Sets To: to the original sender
    - Sets Cc: to everyone else on the original To + Cc, minus your own address
    - Sets Subject, In-Reply-To, and References for proper threading
    - Strips your own from_addr from the CC list

    This is the default reply mode — use it whenever the original message
    had multiple recipients. Use reply() only when you explicitly want to
    reply to the sender alone.

    Args:
        msg:         Message dict as returned by read_message().
        body_md:     Your reply body in Markdown.
        from_name:   Optional display name override.
        attachments: Optional list of file paths to attach.
        rich:        Enable rich HTML rendering (opt-in).
        config:      Optional config dict.
        force:       Set True to send even if already replied (override guard).

    Example:
        msg = read_message("42")
        reply_all(msg, body_md="Thanks for the note — here's my response.")
    """
    # --- Duplicate reply guard ---
    message_id = msg.get("message_id", "")
    already, when = check_already_replied(message_id)
    if already and not force:
        raise RuntimeError(
            f"Already replied to message {message_id} at {when}. "
            f"Pass force=True to reply_all() if you intentionally want to send again."
        )

    cfg = _build_cfg(config)
    own_addr = cfg["from_addr"].strip().lower()

    # Build CC: everyone on the original thread except the sender (goes to To)
    # and except ourselves
    reply_cc = msg.get("reply_cc", "")
    if reply_cc:
        # Filter out our own address in case it appears
        filtered = [
            addr.strip() for addr in reply_cc.split(",")
            if addr.strip() and addr.strip().lower() != own_addr
        ]
        reply_cc = ", ".join(filtered)

    # Normalize whitespace in references (long chains may have folded CRLF)
    refs = re.sub(r'[\r\n]+\s*', ' ', msg.get("reply_references", "") or "").strip()

    send_email(
        to=msg["from_addr"],
        subject=msg["reply_subject"],
        body_md=body_md,
        cc=reply_cc or None,
        in_reply_to=msg["message_id"],
        references=refs or None,
        from_name=from_name,
        attachments=attachments,
        rich=rich,
        config=config,
    )
    # Log the reply so future calls are blocked
    _mark_replied(message_id)


def reply(msg, body_md, *, from_name=None, attachments=None, rich=False, config=None, force=False):
    """
    Reply directly to the sender of a message — no CC.

    Use this only when you explicitly want a private reply to the sender alone
    (e.g. they asked "just reply to me"). For any message with CC recipients,
    prefer reply_all().

    Args:
        msg:         Message dict as returned by read_message().
        body_md:     Your reply body in Markdown.
        from_name:   Optional display name override.
        attachments: Optional list of file paths to attach.
        rich:        Enable rich HTML rendering (opt-in).
        config:      Optional config dict.

    Example:
        msg = read_message("42")
        reply(msg, body_md="Just between us — here's my answer.")
    """
    # --- Duplicate reply guard ---
    message_id = msg.get("message_id", "")
    already, when = check_already_replied(message_id)
    if already and not force:
        raise RuntimeError(
            f"Already replied to message {message_id} at {when}. "
            f"Pass force=True to reply() if you intentionally want to send again."
        )

    send_email(
        to=msg["from_addr"],
        subject=msg["reply_subject"],
        body_md=body_md,
        in_reply_to=msg["message_id"],
        references=msg["reply_references"],
        from_name=from_name,
        attachments=attachments,
        rich=rich,
        config=config,
    )
    # Log the reply so future calls are blocked
    _mark_replied(message_id)


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
    msg = read_message(args.uid, folder=args.folder, mark_read=getattr(args, 'mark_read', False))
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
    print("THREADING — use these for reply:")
    print(f"  message_id       = {msg['message_id'] or '(none)'}")
    print(f"  reply_references = {msg['reply_references'] or '(none)'}")
    print(f"  reply_subject    = {msg['reply_subject']}")
    if msg.get('reply_cc'):
        print(f"  reply_cc         = {msg['reply_cc']}")
    print()
    print("PYTHON REPLY TEMPLATE (reply_all — recommended):")
    print("  import sys")
    print("  sys.path.insert(0, '/home/jason/.openclaw/workspace/projects/waggle')")
    print("  from waggle import reply_all, reply, move_message")
    print("  # Reply-all (keeps everyone on the thread):")
    print("  reply_all(msg, body_md=\"\"\"YOUR REPLY HERE\"\"\")")
    print("  # Direct reply to sender only:")
    print("  reply(msg, body_md=\"\"\"YOUR REPLY HERE\"\"\")")
    print(f'  move_message("{msg["uid"]}", "INBOX.Processed", "{msg["folder"]}")')
    print("─" * 70)


def _cli_search(args):
    folders = args.folders.split(",") if args.folders else ["INBOX", "INBOX.Processed"]
    query = {}
    if args.from_addr:
        query["from_addr"] = args.from_addr
    if args.subject:
        query["subject"] = args.subject
    if args.text:
        query["text"] = args.text
    if args.since:
        query["since"] = args.since
    if args.unseen:
        query["unseen"] = True

    msgs = search_messages(query, folders=folders, limit=args.limit)
    if not msgs:
        print("(no messages found)")
        return
    print(f"{'UID':<6} {'FOLDER':<20} {'UNREAD':<7} {'FROM':<30} {'SUBJECT':<45} {'DATE'}")
    print("-" * 130)
    for m in msgs:
        unread = "●" if m["unread"] else " "
        frm    = (m["from_name"] or m["from_addr"])[:28]
        subj   = m["subject"][:43]
        date   = m["date"][:20] if m["date"] else ""
        folder = m["folder"][:18]
        print(f"{m['uid']:<6} {folder:<20} {unread:<7} {frm:<30} {subj:<45} {date}")


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
        save_sent=not getattr(args, "no_save_sent", False),
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
    p_read.add_argument("uid", help="IMAP UID (from waggle list or waggle search)")
    p_read.add_argument("--folder", default="INBOX", help="IMAP folder (default: INBOX)")
    p_read.add_argument("--mark-read", action="store_true", default=False,
                        help="Mark message as read (\\Seen) on the server")

    # --- move ---
    p_move = sub.add_parser("move", help="Move a message to another folder")
    p_move.add_argument("uid",  help="IMAP UID (from waggle list or waggle search)")
    p_move.add_argument("dest", help="Destination folder (e.g. INBOX.Processed)")
    p_move.add_argument("--folder", default="INBOX", help="Source folder (default: INBOX)")

    # --- search ---
    p_srch = sub.add_parser("search", help="Search messages across folders (returns folder+UID together)")
    p_srch.add_argument("--from",    dest="from_addr", default=None, help="Filter by sender address or name")
    p_srch.add_argument("--subject", default=None, help="Filter by subject keyword")
    p_srch.add_argument("--text",    default=None, help="Full-text body search")
    p_srch.add_argument("--since",   default=None, help="Only messages since date (e.g. 17-Apr-2026)")
    p_srch.add_argument("--unseen",  action="store_true", default=False, help="Only unread messages")
    p_srch.add_argument("--folders", default=None, help="Comma-separated folder list (default: INBOX,INBOX.Processed)")
    p_srch.add_argument("--limit",   type=int, default=20, help="Max results per folder (default: 20)")

    # --- attach ---
    p_att = sub.add_parser("attach", help="Download attachments from a message")
    p_att.add_argument("uid", help="IMAP UID (from waggle list or waggle search)")
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
    p_send.add_argument("--rich",          action="store_true", default=False)
    p_send.add_argument("--no-save-sent",  action="store_true", default=False,
                        help="Skip saving to IMAP Sent folder")

    args = parser.parse_args()

    dispatch = {
        "list":   _cli_list,
        "read":   _cli_read,
        "move":   _cli_move,
        "attach": _cli_attach,
        "send":   _cli_send,
        "search": _cli_search,
    }

    if args.command in dispatch:
        dispatch[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
