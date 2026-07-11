"""Regression tests for RFC 5322 reply threading (waggle-mail).

Guards against the failure where a reply carries In-Reply-To but an orphaned
References chain. Gmail and most modern clients thread on References, so a
missing or partial chain silently splits a conversation. These fixtures make
sure the orphaning can't sneak back in.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import waggle


def test_in_reply_to_appended_when_references_empty():
    # Caller passed --in-reply-to but no --references at all.
    assert waggle._build_references("<a@x>", None) == "<a@x>"


def test_in_reply_to_appended_to_existing_chain():
    assert waggle._build_references("<c@x>", "<a@x> <b@x>") == "<a@x> <b@x> <c@x>"


def test_no_duplicate_when_parent_already_in_chain():
    # Caller already appended the parent — don't double it.
    assert waggle._build_references("<c@x>", "<a@x> <b@x> <c@x>") == "<a@x> <b@x> <c@x>"


def test_references_preserved_without_in_reply_to():
    assert waggle._build_references(None, "<a@x> <b@x>") == "<a@x> <b@x>"


def test_duplicates_in_parent_chain_collapse_in_order():
    # A messy multi-hop parent chain with a repeated id collapses to first-seen
    # order — the chain stays valid without reordering.
    assert waggle._build_references("<c@x>", "<a@x> <b@x> <a@x>") == "<a@x> <b@x> <c@x>"


def test_empty_when_nothing_provided():
    assert waggle._build_references(None, None) == ""


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else print("\nall threading tests passed") or 0)
