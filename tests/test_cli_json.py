"""Tests for --json on list/read/search/move/attach (issue #27).

The issue as filed assumed these subcommands defaulted to JSON with no human
format — that's been stale since April 2026 (they've defaulted to formatted
text for months). The real gap was the opposite: no way to get *structured*
output from these five, unlike `check` which already has --json. This adds
that, following check's exact default-text/--json-opt-in shape.
"""
import json
from unittest.mock import patch

import waggle


class _Args:
    """Lightweight argparse.Namespace stand-in, matching test_check.py's convention."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


_MSG = {
    "uid": "42", "unread": True, "from_name": "Jane", "from_addr": "jane@example.com",
    "subject": "Hi", "date": "Sat, 11 Jul 2026 08:32:00 +0000",
}


class TestListJson:
    def test_json_output_shape(self, capsys):
        with patch("waggle.list_inbox", return_value=[_MSG]):
            waggle._cli_list(_Args(folder="INBOX", limit=20, json=True))
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"folder": "INBOX", "messages": [_MSG]}

    def test_json_output_empty_is_still_valid_json(self, capsys):
        """Unlike the text path's '(no messages...)' string, JSON stays parseable."""
        with patch("waggle.list_inbox", return_value=[]):
            waggle._cli_list(_Args(folder="INBOX", limit=20, json=True))
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"folder": "INBOX", "messages": []}

    def test_text_default_unchanged(self, capsys):
        with patch("waggle.list_inbox", return_value=[]):
            waggle._cli_list(_Args(folder="INBOX", limit=20, json=False))
        assert "(no messages in INBOX)" in capsys.readouterr().out


class TestReadJson:
    def test_json_output_shape(self, capsys):
        full_msg = {**_MSG, "to": "me@example.com", "body_plain": "hello", "attachments": [],
                    "from_raw": "Jane <jane@example.com>", "message_id": "<a@x>",
                    "reply_references": None, "reply_subject": "Re: Hi", "folder": "INBOX"}
        with patch("waggle.read_message", return_value=full_msg):
            waggle._cli_read(_Args(uid="42", folder="INBOX", mark_read=False, json=True))
        payload = json.loads(capsys.readouterr().out)
        assert payload == full_msg


class TestSearchJson:
    def test_json_output_shape(self, capsys):
        with patch("waggle.search_messages", return_value=[_MSG]):
            args = _Args(from_addr=None, subject="Hi", text=None, since=None,
                         unseen=False, folders=None, limit=20, json=True)
            waggle._cli_search(args)
        payload = json.loads(capsys.readouterr().out)
        assert payload["messages"] == [_MSG]
        assert payload["query"] == {"subject": "Hi"}


class TestMoveJson:
    def test_json_output_shape(self, capsys):
        with patch("waggle.move_message"):
            waggle._cli_move(_Args(uid="42", dest="INBOX.Processed", folder="INBOX", json=True))
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"uid": "42", "from": "INBOX", "to": "INBOX.Processed", "status": "moved"}


class TestAttachJson:
    def test_json_output_shape(self, capsys):
        with patch("waggle.download_attachments", return_value=["/tmp/a.pdf", "/tmp/b.jpg"]):
            waggle._cli_attach(_Args(uid="42", folder="INBOX", dest="/tmp", json=True))
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"uid": "42", "dest": "/tmp", "paths": ["/tmp/a.pdf", "/tmp/b.jpg"]}


if __name__ == "__main__":
    import sys
    failures = 0
    for cls_name, cls in sorted(globals().items()):
        if cls_name.startswith("Test") and isinstance(cls, type):
            inst = cls()
            for name in dir(inst):
                if name.startswith("test_"):
                    try:
                        getattr(inst, name)(capsys=None)
                    except TypeError:
                        pass  # needs capsys fixture — run via pytest instead
    print("run via: python3 -m pytest tests/test_cli_json.py -v")
    sys.exit(0)
