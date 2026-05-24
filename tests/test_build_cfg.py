import json

import waggle


def test_build_cfg_reads_optional_config_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "waggle.json"
    cfg_path.write_text(json.dumps({
        "host": "smtp.example.com",
        "port": 2465,
        "user": "sam@example.com",
        "password": "secret",
        "from_addr": "sam@example.com",
        "from_name": "Sam",
        "imap_host": "imap.example.com",
        "imap_port": 2993,
        "imap_tls": False,
        "tls": False,
        "smtp_starttls": True,
        "sent_folder": "Sent Items",
    }))

    monkeypatch.delenv("WAGGLE_HOST", raising=False)
    monkeypatch.delenv("WAGGLE_PORT", raising=False)
    monkeypatch.delenv("WAGGLE_USER", raising=False)
    monkeypatch.delenv("WAGGLE_PASS", raising=False)
    monkeypatch.delenv("WAGGLE_FROM", raising=False)
    monkeypatch.delenv("WAGGLE_NAME", raising=False)
    monkeypatch.delenv("WAGGLE_IMAP_HOST", raising=False)
    monkeypatch.delenv("WAGGLE_IMAP_PORT", raising=False)
    monkeypatch.delenv("WAGGLE_IMAP_TLS", raising=False)
    monkeypatch.delenv("WAGGLE_TLS", raising=False)
    monkeypatch.delenv("WAGGLE_SMTP_STARTTLS", raising=False)
    monkeypatch.delenv("WAGGLE_SENT_FOLDER", raising=False)

    cfg = waggle._build_cfg({"config_path": str(cfg_path)})

    assert cfg["host"] == "smtp.example.com"
    assert cfg["port"] == 2465
    assert cfg["user"] == "sam@example.com"
    assert cfg["password"] == "secret"
    assert cfg["from_addr"] == "sam@example.com"
    assert cfg["from_name"] == "Sam"
    assert cfg["imap_host"] == "imap.example.com"
    assert cfg["imap_port"] == 2993
    assert cfg["imap_tls"] is False
    assert cfg["tls"] is False
    assert cfg["smtp_starttls"] is True
    assert cfg["sent_folder"] == "Sent Items"


def test_build_cfg_env_overrides_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "waggle.json"
    cfg_path.write_text(json.dumps({
        "host": "smtp.example.com",
        "user": "sam@example.com",
        "password": "secret",
    }))

    monkeypatch.setenv("WAGGLE_CONFIG", str(cfg_path))
    monkeypatch.setenv("WAGGLE_HOST", "smtp.override.example.com")
    monkeypatch.setenv("WAGGLE_USER", "override@example.com")
    monkeypatch.delenv("WAGGLE_PASS", raising=False)

    cfg = waggle._build_cfg()

    assert cfg["host"] == "smtp.override.example.com"
    assert cfg["user"] == "override@example.com"
    assert cfg["password"] == "secret"
