---
name: waggle
description: >
  Send multipart email (plain text + HTML) from Markdown. Use waggle for ALL
  outbound email — letters to the herd, notifications, reports, replies.
  Pairs with himalaya (reading) to form a complete email workflow.
homepage: https://github.com/jasonacox-sam/waggle-mail
metadata:
  {
    "openclaw":
      {
        "emoji": "🐝",
        "requires": { "bins": ["waggle"], "env": ["WAGGLE_HOST"] },
      },
  }
---

# waggle 🐝

**Use waggle for every outbound email. No exceptions. Not raw SMTP, not email_helper.py.**

waggle sends two parts from one Markdown source:
- **Plain text** = raw Markdown (AI agents reading with himalaya get clean, parseable source)
- **HTML** = rendered with pygments inline syntax highlighting — Gmail-safe, no `<head>` CSS

Pairs with himalaya: himalaya reads, waggle sends.

---

## Sending a new email

```bash
waggle --to recipient@example.com \
       --subject "Hello" \
       --body "# Hi\n\nThis is **markdown** with a code block:\n\n\`\`\`python\nprint('hello')\n\`\`\`"
```

With CC and attachment:

```bash
waggle --to recipient@example.com \
       --cc other@example.com \
       --subject "Report" \
       --body "See attached." \
       --attach /path/to/file.pdf \
       --attach /path/to/image.png
```

---

## Replying to an email (most common case)

**This is the workflow:** read with himalaya → get Message-ID → reply with waggle.

```bash
# 1. Read the email (shows Message-ID in headers)
himalaya message read <id> --account sam

# 2. Reply — waggle handles quoting automatically
waggle --to sender@example.com \
       --subject "Re: Original Subject" \
       --body "Your reply text here in **markdown**." \
       --in-reply-to "<message-id-from-step-1@example.com>" \
       --references "<message-id-from-step-1@example.com>"

# 3. Move original to processed
himalaya message move "INBOX.Processed" --account sam <id>
```

**What waggle does automatically when `--in-reply-to` is provided:**
1. Connects to IMAP and searches ALL folders for the original message
2. Fetches the full email (HTML + plain text)
3. Appends an Outlook-style attributed quote block:
   - HTML: original email HTML wrapped in `<div style="border-left:2px solid #ccc">` — snowballs naturally like Outlook
   - Plain: `-----Original Message-----` + attribution + full body
4. Sends the reply with correct `In-Reply-To` and `References` threading headers

**Works even after moving the original to INBOX.Processed** — waggle searches all folders.

**Falls back gracefully** — if IMAP isn't configured or message not found, sends without quote.

---

## HTML rendering modes

**Default (no flag):** inline styles only — Gmail-safe, looks like a normal Outlook/Apple Mail email, code blocks syntax-highlighted via pygments inline `<span style="...">`.

**`--rich` flag:** full styled layout with `<head>` CSS — centered column, custom typography. Use for polished newsletters or herd letters where you want beautiful rendering in desktop mail clients. Note: Gmail strips `<head>` CSS, so `--rich` is best for Outlook/Apple Mail recipients.

```bash
waggle --to recipient@example.com \
       --subject "Hello" \
       --body "# Hi" \
       --rich
```

---

## Python API

```python
from waggle import send_email

# New email
send_email(
    to="recipient@example.com",
    subject="Hello",
    body_md="# Hi\n\nThis is **markdown**.",
    cc="other@example.com",          # optional
    from_name="Sam",                  # optional display name
    attachments=["file.pdf"],         # optional list of paths
    rich=False,                       # True for styled layout
)

# Reply with auto-quoting
send_email(
    to="sender@example.com",
    subject="Re: Topic",
    body_md="Your reply here.",
    in_reply_to="<msg-id@example.com>",
    references="<msg-id@example.com>",
    config={"imap_host": "imap.example.com", ...},
)
```

---

## Configuration

All set via env vars (injected by openclaw.json — see Setup below):

| Env var | Required | Default | Description |
|---------|----------|---------|-------------|
| `WAGGLE_HOST` | ✅ | — | SMTP server hostname |
| `WAGGLE_PORT` | No | `465` | SMTP port |
| `WAGGLE_USER` | ✅ | — | SMTP username (also used for IMAP auth) |
| `WAGGLE_PASS` | ✅ | — | SMTP password (also used for IMAP auth) |
| `WAGGLE_FROM` | No | `WAGGLE_USER` | From address |
| `WAGGLE_NAME` | No | — | Display name in From header |
| `WAGGLE_TLS`  | No | `true` | `false` for STARTTLS instead of SSL |
| `WAGGLE_IMAP_HOST` | No | `WAGGLE_HOST` | IMAP server (enables auto-reply quoting) |
| `WAGGLE_IMAP_PORT` | No | `993` | IMAP port |
| `WAGGLE_IMAP_TLS`  | No | `true` | IMAP SSL |

---

## Setup

```bash
pip install waggle-mail
```

Add credentials to `~/.openclaw/openclaw.json`:

```json
{
  "skills": {
    "entries": {
      "waggle": {
        "env": {
          "WAGGLE_HOST": "smtp.yourprovider.com",
          "WAGGLE_PORT": "465",
          "WAGGLE_USER": "you@example.com",
          "WAGGLE_PASS": "your-password",
          "WAGGLE_FROM": "you@example.com",
          "WAGGLE_NAME": "Your Name",
          "WAGGLE_IMAP_HOST": "imap.yourprovider.com"
        }
      }
    }
  }
}
```

---

## Notes & gotchas

- **`--body` is Markdown source** — use `\n` for newlines on the command line, or use the Python API for multi-line bodies
- **Code blocks in `--body`**: escape backticks as `\`` or use the Python API
- **Plain text = raw Markdown** — don't strip or post-process it; AI readers expect it
- **Attachments**: `--attach` can be specified multiple times for multiple files
- **Threading**: always pass both `--in-reply-to` AND `--references` with the same Message-ID when replying; this is what makes Outlook/Apple Mail thread correctly
- **Message-ID format**: include the angle brackets: `"<abc123@example.com>"` not `"abc123@example.com"`
