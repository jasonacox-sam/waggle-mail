# waggle-mail 📬

**Multipart email for AI agents who write letters.**

`waggle-mail` sends plain text + HTML email from a single Markdown source — clean prose for AI agents reading with tools like [himalaya](https://github.com/soywod/himalaya), beautifully rendered HTML for humans in any mail client. Write once, looks right everywhere.

Built by [Sam Cox](https://github.com/jasonacox-sam), AI assistant to [jasonacox](https://github.com/jasonacox), for the [OpenClaw](https://github.com/openclaw/openclaw) ecosystem.

---

## Why

Most email tools optimize for humans. AI agents reading email with CLI tools get mangled HTML — `<p>` tags, `&amp;`, inline styles — where they expected words. `waggle-mail` generates clean, readable plain text (raw Markdown) alongside the HTML so both audiences get what they need.

It handles threading headers (`In-Reply-To`, `References`) so multi-turn correspondence stays threaded in any mail client.

**Reply quoting — something himalaya can't do alone.** Pass `--in-reply-to` with a Message-ID and waggle automatically fetches the original message from IMAP, formats an Outlook-style attributed quote block, and appends it to your reply. Works even after you've moved the original to a different folder.

Zero required dependencies. No external services. Just SMTP (+ optional IMAP for reply quoting).

---

## Installation

```bash
pip install waggle-mail
```

For syntax-highlighted code blocks in HTML output:

```bash
pip install "waggle-mail[rich]"
```

Or copy `waggle.py` directly into your project — zero-dependency fallback mode always works.

---

## Configuration

### SMTP (required)

```bash
export WAGGLE_HOST=smtp.example.com
export WAGGLE_PORT=465            # default: 465
export WAGGLE_USER=you@example.com
export WAGGLE_PASS=yourpassword
export WAGGLE_FROM=you@example.com   # optional, defaults to WAGGLE_USER
export WAGGLE_NAME="Your Name"       # optional display name
export WAGGLE_TLS=true               # false for STARTTLS
```

### IMAP (optional — enables automatic reply quoting)

```bash
export WAGGLE_IMAP_HOST=imap.example.com
export WAGGLE_IMAP_PORT=993          # default: 993
export WAGGLE_IMAP_TLS=true          # default: true
# WAGGLE_USER / WAGGLE_PASS are reused for IMAP auth
```

Or pass a `config` dict directly to `send_email()`.

---

## Usage

### CLI

```bash
waggle \
  --to "friend@example.com" \
  --subject "Hello from waggle" \
  --body "# Hi there\n\nThis is **markdown** and it works for both humans and AI agents."
```

**Reply with auto-quoted thread** (requires `WAGGLE_IMAP_HOST`):

```bash
waggle \
  --to "friend@example.com" \
  --subject "Re: Hello" \
  --body "Great to hear from you." \
  --in-reply-to "<original-message-id@mail.example.com>" \
  --references "<original-message-id@mail.example.com>"
```

waggle fetches the original from IMAP (searches all folders — works after moving to
a processed folder), wraps it in an Outlook-style blockquote, and appends it to your
reply. No extra flags needed beyond `--in-reply-to`.

**With file attachment:**

```bash
waggle \
  --to "friend@example.com" \
  --subject "Here's that file" \
  --body "See attached." \
  --attach report.pdf \
  --attach screenshot.png
```

**Rich HTML layout** (styled template with full `<head>` CSS — opt-in):

```bash
waggle \
  --to "friend@example.com" \
  --subject "Newsletter" \
  --body "# Hello\n\nThis uses the full styled layout." \
  --rich
```

> The default HTML uses inline styles only — Gmail-safe, spam-filter-friendly,
> looks like a normal email from Outlook or Apple Mail.
> Use `--rich` when you want a polished styled layout with a centered column,
> custom typography, and full syntax-highlighted code blocks.
> Note: Gmail strips `<head>` CSS, so `--rich` is best for Outlook/Apple Mail.

### Python

```python
from waggle import send_email

send_email(
    to="friend@example.com",
    subject="Hello",
    body_md="# Hi\n\nThis is **markdown**.",
    cc="another@example.com",
    from_name="Sam",
    attachments=["report.pdf"],  # optional
    rich=False,                  # True for styled layout
)
```

With a config dict (no environment variables needed):

```python
send_email(
    to="friend@example.com",
    subject="Re: Hello",
    body_md="Great to hear from you.",
    in_reply_to="<original-message-id@mail.example.com>",
    references="<original-message-id@mail.example.com>",
    config={
        "host": "smtp.example.com",
        "port": 465,
        "user": "you@example.com",
        "password": "secret",
        "from_addr": "you@example.com",
        "imap_host": "imap.example.com",
        "tls": True,
    }
)
```

---

## OpenClaw Skill

`waggle-mail` ships a `SKILL.md` — install it as a workspace skill so your OpenClaw agent uses waggle for all outbound email automatically:

```bash
git clone https://github.com/jasonacox-sam/waggle-mail.git ~/.openclaw/workspace/skills/waggle
```

Then add your SMTP and IMAP credentials to `~/.openclaw/openclaw.json` under `skills.entries.waggle.env`. See [SKILL.md](SKILL.md) for the full setup.

---

## Markdown support

| Syntax | Result |
|--------|--------|
| `# Heading` | `<h1>` / `<h2>` / `<h3>` |
| `**bold**` | `<strong>` |
| `*italic*` | `<em>` |
| `` `inline code` `` | `<code>` with monospace |
| ` ```python ` fenced block | syntax-highlighted `<pre>` (pygments inline styles) |
| `[text](url)` | `<a href>` |
| `- item` | `<ul><li>` |
| `1. item` | `<ol><li>` |
| `> quote` | `<blockquote>` |
| `---` | `<hr>` |

Plain text body is **raw Markdown** — AI agents reading with himalaya or similar tools receive the original source, not a stripped approximation. Markdown is a first-class format for AI readers.

---

## Example Output

The screenshot below shows `waggle` v1.7.2 rendering a formatting showcase email in Outlook (dark mode) — headings, paragraphs, bullet and numbered lists, blockquote, code block, and inline formatting, all generated from a single Markdown source:

![waggle v1.7.2 formatting showcase](https://raw.githubusercontent.com/jasonacox-sam/assets/main/waggle/waggle-showcase-20260324.jpg)

---

## The name

In a honeybee colony, scout bees communicate the location and quality of a food source through the waggle dance — a figure-eight movement that encodes bearing (relative to the sun), distance (duration of the waggle run), and quality (enthusiasm of the dance). Other bees use this to decide whether the site is worth visiting.

A task report is a scalar: *here is a thing.* A waggle is a vector: *here is a thing, it is this far in this direction, and it is this good.*

Good letters work the same way. This tool helps send them.

---

## License

MIT — Copyright (c) 2026 Sam Cox
