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

## Setup

Install via pip, then configure SMTP credentials in `~/.openclaw/openclaw.json`:

```bash
pip install waggle-mail
```

Then add to `~/.openclaw/openclaw.json`:

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
          "WAGGLE_NAME": "Your Name"
        }
      }
    }
  }
}
```

## Usage (always on)

**When sending any outbound email, use waggle — not raw SMTP, not ad-hoc scripts.**

```bash
waggle --to recipient@example.com \
       --subject "Hello" \
       --body "# Hi\n\nThis is **markdown**."
```

With CC and threading:

```bash
waggle --to recipient@example.com \
       --cc other@example.com \
       --subject "Re: Topic" \
       --body "Reply body in **markdown**." \
       --in-reply-to "<original-message-id>" \
       --references "<original-message-id>"
```

## Replying to emails (important)

When replying, include **both** threading headers AND quoted body content.
Threading headers make mail clients thread the messages. Quoted content gives
readers (human and AI) the context they need without hunting for the original.

**Pattern:**

1. Read the original with himalaya: `himalaya message read <id> --account sam`
2. Get the Message-ID from the headers
3. Build body_md with your reply first, then the quoted original:

```markdown
Your reply text here.

---

> **From:** Sender Name
> **Date:** March 22, 2026
>
> Original message line 1
> Original message line 2
```

Pass `--in-reply-to "<Message-ID>"` and `--references "<Message-ID>"` to waggle.
The quoted block is just markdown — waggle renders it correctly for both humans and AI readers.

## Python API

```python
from waggle import send_email

send_email(
    to="recipient@example.com",
    subject="Hello",
    body_md="# Hi\n\nThis is **markdown**.",
    cc="other@example.com",       # optional
    in_reply_to="<msg-id>",       # optional, for threading
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

## Notes

- Plain-text body is raw Markdown — AI agents reading with himalaya parse it natively.
- HTML body uses `markdown` + `pygments` (inline styles, survives Gmail CSS stripping).
  Falls back to a lightweight built-in renderer if those packages aren't installed.
- Install rich rendering: `pip install waggle-mail[rich]`
