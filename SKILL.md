---
name: waggle
description: >
  Send multipart email (plain text + HTML) from Markdown. Use waggle for all
  outbound email — letters to the herd, notifications, reports. Pairs with
  himalaya (reading) to form a complete email workflow. Zero extra dependencies;
  talks directly to SMTP.
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

# waggle

Send beautifully formatted email from Markdown. Plain-text body for AI agents,
HTML body for humans — generated from the same source, sent in one call.

**Pairs with himalaya:** waggle sends, himalaya reads. They don't overlap.

## Usage (always on)

**When sending any outbound email, use waggle — not raw SMTP, not ad-hoc scripts.**

```bash
waggle --to recipient@example.com \
       --subject "Hello" \
       --body "# Hi\n\nThis is **markdown**."
```

## Replying to emails

When replying, pass `--in-reply-to` with the original Message-ID. waggle handles everything else automatically:

1. Fetches the original message from IMAP (searches all folders — works even after you've moved it to INBOX.Processed)
2. Formats an attributed quoted block with smart trimming (no snowballing reply chains)
3. Appends it to your reply body

```bash
waggle --to sender@example.com \
       --subject "Re: Topic" \
       --body "Your reply here in **markdown**." \
       --in-reply-to "<original-message-id@example.com>" \
       --references "<original-message-id@example.com>"
```

**To find the Message-ID:** `himalaya message read <id> --account sam` — look for the `Message-ID:` header.

Requires `WAGGLE_IMAP_HOST` configured (see below). Falls back gracefully — sends without quote if IMAP is unavailable.

## Python API

```python
from waggle import send_email

send_email(
    to="recipient@example.com",
    subject="Hello",
    body_md="# Hi\n\nThis is **markdown**.",
    cc="other@example.com",       # optional
    in_reply_to="<msg-id>",       # optional — triggers auto-quote + threading
    references="<msg-id>",        # optional — threading
)
```

## Configuration

| Env var | Required | Default | Description |
|---------|----------|---------|-------------|
| `WAGGLE_HOST` | ✅ | — | SMTP server hostname |
| `WAGGLE_PORT` | No | `465` | SMTP port |
| `WAGGLE_USER` | ✅ | — | SMTP username |
| `WAGGLE_PASS` | ✅ | — | SMTP password |
| `WAGGLE_FROM` | No | `WAGGLE_USER` | From address |
| `WAGGLE_NAME` | No | — | Display name in From header |
| `WAGGLE_TLS`  | No | `true` | `false` for STARTTLS instead of SSL |
| `WAGGLE_IMAP_HOST` | No | `WAGGLE_HOST` | IMAP server for auto-quoting replies |
| `WAGGLE_IMAP_PORT` | No | `993` | IMAP port |
| `WAGGLE_IMAP_TLS`  | No | `true` | IMAP SSL |

## Setup

```bash
pip install waggle-mail
```

Add to `~/.openclaw/openclaw.json`:

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

## Notes

- Plain-text body is raw Markdown — AI agents reading with himalaya parse it natively.
- HTML body uses `markdown` + `pygments` (inline styles, survives Gmail CSS stripping).
  Falls back to a lightweight built-in renderer if those packages aren't installed.
- Install rich rendering: `pip install waggle-mail[rich]`
