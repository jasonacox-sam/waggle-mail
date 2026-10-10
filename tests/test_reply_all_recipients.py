"""Regression tests for reply-all recipient extraction."""

from email.message import EmailMessage

import waggle


def test_reply_cc_keeps_long_complex_cc_list_on_python_312():
    """A complex address must not make the whole Cc list disappear."""
    msg = EmailMessage()
    msg["From"] = "Kevin Example <kevin@example.com>"
    msg["To"] = "Marey <marey@example.com>"
    msg["Cc"] = (
        "OC <oc@example.com>, "
        "rockbot@example.com <rockbot@example.com>, "
        "Jordan Example <jordan@example.com>, "
        "nova@example.com <nova@example.com>, "
        "Jules <jules@example.com>, "
        "Sam <sam@example.com>"
    )
    msg["Subject"] = "Re: Memorial thread"
    msg["Message-ID"] = "<memorial@example.com>"
    msg.set_content("A broad thread with a complex recipient list.")

    parsed = waggle._parse_message(msg.as_bytes())

    assert parsed["reply_cc"].split(", ") == [
        "marey@example.com",
        "oc@example.com",
        "rockbot@example.com",
        "jordan@example.com",
        "nova@example.com",
        "jules@example.com",
        "sam@example.com",
    ]


def test_reply_cc_excludes_sender_and_deduplicates():
    msg = EmailMessage()
    msg["From"] = "Sender <sender@example.com>"
    msg["To"] = "Sam <sam@example.com>, Sender <sender@example.com>"
    msg["Cc"] = "Sam Again <sam@example.com>, Other <other@example.com>"
    msg["Subject"] = "Thread"
    msg.set_content("Body")

    parsed = waggle._parse_message(msg.as_bytes())

    assert parsed["reply_cc"] == "sam@example.com, other@example.com"
