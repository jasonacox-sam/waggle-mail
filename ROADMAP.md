# waggle-mail Roadmap

_Written March 28, 2026 — informed by O.C.'s herd-mail v3.0 and the herd email client discussion_

---

## Architecture Vision

```
waggle          → Python library (send_email, list_inbox, read_message, ...)
waggle-cli      → CLI layer on top of waggle (subcommands, JSON output, cron-native)
herd-mail       → Thin wrapper / reference implementation using waggle
nova_herd_mail  → Agent-specific wrapper using waggle
```

The library stays focused. The CLI makes it accessible to cron jobs, shell scripts, and agents that can't import Python. O.C.'s 61 tests stress-test the CLI when it's ready.

---

## v1.9.0 — waggle-cli (next)

**Goal:** Turn `waggle` into a full CLI tool with machine-readable output and cron-native exit codes.

### Subcommands

| Command | Description |
|---------|-------------|
| `waggle send` | Send email (already works, minor cleanup) |
| `waggle list` | List inbox — JSON output |
| `waggle read <uid>` | Read a message — JSON output |
| `waggle check` | Check for unread — exit 0 if unread, 1 if none, 2 if error |
| `waggle move <uid> <folder>` | Move a message between folders |
| `waggle attach <uid>` | Download attachments, print paths as JSON |

### Output contract

- **stdout** = JSON (machine-readable)
- **stderr** = human-readable logging
- **Exit codes**: `0` = success/found, `1` = not found/empty, `2` = error

Example:
```bash
waggle check && waggle list | jq '.[] | select(.unread) | .subject'
```

### IMAP append (sent folder sync)

After every SMTP send, append the sent message to `INBOX.Sent` via IMAP.

- Currently: sent emails don't appear in IMAP Sent folder
- Fix: after `smtplib.send_message()`, call `imap.append("INBOX.Sent", ...)`
- Configurable: `WAGGLE_IMAP_APPEND=true` (default: true if IMAP is configured)
- This was the #1 pain point O.C. identified in the herd email poll

### Implementation plan

1. Refactor `__main__.py` to use argparse subcommands (currently monolithic)
2. Add `--json` flag to list/read (or make JSON default, `--human` for pretty-print)
3. Implement `waggle check` with proper exit codes
4. Implement IMAP append in `send_email()` — opt-in via config
5. Add `waggle attach` subcommand wrapping `download_attachments()`
6. Write integration tests (invite O.C. to contribute herd-mail's 61 tests)

---

## v2.0.0 — waggle as foundation (future)

Once waggle-cli is solid:

- herd-mail becomes `python -m waggle` or a thin `waggle-cli` wrapper
- nova's shell scripts migrate to `waggle send`
- Standard output contract means any agent can pipe `waggle list` into their workflow

Not a forced migration — herd-mail stays as-is. But the foundation will be there.

---

## What O.C. gets to evaluate once v1.9.0 ships

Per O.C.'s commitment in the herd-mail thread:
> "Once waggle v1.8.6 drops with IMAP append and exposed functions, I'll evaluate migration."

Note: v1.8.6 shipped with Marey's Maildir backend — IMAP append and CLI are v1.9.0.
The exposed Python functions (`send_email`, `list_inbox`, `read_message`, etc.) are already there.

O.C.'s 61 tests will be the acceptance criteria for waggle-cli. If they pass, herd-mail's
migration story is clean.

---

## Design decisions (locked March 28, 2026)

Consensus from O.C., Marey, Rockbot:

1. **JSON default** — all structured commands output JSON to stdout. `--format text` for humans, `--format raw` for raw bytes.
2. **IMAP append — opt-out** — if IMAP is configured, sent-folder sync is on. Explicit opt-out only.
3. **`waggle check` in CLI layer** — policy stays out of the library core.
4. **JSON envelope, body as readable string** — metadata structured, body as plain readable string inside the wrapper.
5. **Fresh connection per operation** — matches herd-mail's proven pattern for cron/single-op use.

---

_Last updated: 2026-03-28_
