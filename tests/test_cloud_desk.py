"""The desk without the Mac (2026-11-03): GitHub Actions runs it; the Mac hands its database over through Gmail,
sealed so only the cloud can open it, then goes idle. Nothing here touches the real Gmail."""
from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from datetime import datetime
from email import policy

import pytest
from cryptography.exceptions import InvalidTag

from engine import cloud_desk as cd
from engine import gmail_autopilot as gm
from tests.test_gmail_autopilot import FakeIMAP

KEY = "x" * 24 + "cloud-desk-test-key"
ME = "jordan@example.test"


class Courier:
    """SMTP that delivers to the account's own Sent folder, which the fake IMAP then serves."""

    def __init__(self):
        self.store: dict[int, bytes] = {}

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        self.store[len(self.store) + 1] = msg.as_bytes(policy=policy.SMTP)


@pytest.fixture()
def courier(monkeypatch):
    c = Courier()
    gm.set_imap_factory(lambda: FakeIMAP(c.store))
    monkeypatch.setenv("SMTP_USER", ME)
    monkeypatch.setenv("SMTP_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setenv("DESK_KEY", KEY)
    monkeypatch.setattr(gm, "credentials", lambda: (ME, "abcdefghijklmnop"))
    yield c
    gm.set_imap_factory(None)


def _root(tmp_path, rows=3):
    root = tmp_path / "mac"
    (root / "config").mkdir(parents=True)
    con = sqlite3.connect(root / "wholesaling.db")
    con.execute("create table t (x)")
    con.executemany("insert into t values (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()
    (root / "config" / "settings.local.yaml").write_text("operator:\n  name: Jordan Operator\n")
    (root / "config" / "token.json").write_text('{"token": "t"}')
    return root


def test_only_the_cloud_can_open_what_the_mac_seals():
    sealed = cd.seal(b"the whole desk", cd.public_hex(KEY))
    assert cd.unseal(sealed, KEY) == b"the whole desk"
    with pytest.raises(InvalidTag):
        cd.unseal(sealed, "another-key-entirely-123")
    assert cd.unlock(cd.lock(b"state", KEY), KEY) == b"state"
    with pytest.raises(InvalidTag):
        cd.unlock(cd.lock(b"state", KEY), "another-key-entirely-123")


def test_a_bundle_carries_the_database_and_config_and_nothing_else(tmp_path):
    root = _root(tmp_path, rows=5)
    data = cd.make_bundle(root)
    dest = tmp_path / "cloud"
    assert sorted(cd.apply_bundle(data, dest)) == ["config/settings.local.yaml", "config/token.json", "wholesaling.db"]
    assert sqlite3.connect(dest / "wholesaling.db").execute("select count(*) from t").fetchone()[0] == 5
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:xz") as tar:
        for name in ("../escape.txt", ".env", "config/token.json"):
            info = tarfile.TarInfo(name)
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
    assert cd.apply_bundle(evil.getvalue(), dest) == ["config/token.json"]
    assert not (tmp_path / "escape.txt").exists() and not (dest / ".env").exists()


def test_gmail_carries_a_bundle_in_parts_and_checks_it(courier, monkeypatch):
    monkeypatch.setattr(cd, "PART_BYTES", 1000)
    blob = bytes(range(256)) * 13
    n = cd.send_bundle(ME, "pw", blob, "db", now=datetime(2026, 11, 3, 15, 0), smtp_factory=courier)
    assert n == 4 and len(courier.store) == 4
    assert cd.latest_bundle(ME, "pw", "db") == ("20261103T150000", blob)
    assert cd.latest_bundle(ME, "pw", "config") is None
    del courier.store[3]                                         # a part never arrived: that set is skipped
    assert cd.latest_bundle(ME, "pw", "db") is None


def test_the_mac_hands_off_once_the_cloud_is_ready_then_stays_idle(courier, tmp_path):
    root = _root(tmp_path)
    assert cd.mac_handoff(root=root, smtp_factory=courier)["state"] == "local"      # cloud not set up yet
    cd.post_ready(ME, "pw", KEY, smtp_factory=courier)
    assert cd.ready_key(ME, "pw") == cd.public_hex(KEY)
    h = cd.mac_handoff(root=root, smtp_factory=courier, now=datetime(2026, 11, 3, 16, 0))
    assert h["state"] == "handed_off" and (root / "data" / "runtime_cloud.json").exists()
    stamp, sealed = cd.latest_bundle(ME, "pw", "db")
    assert "wholesaling.db" in cd.apply_bundle(cd.unseal(sealed, KEY), tmp_path / "check")
    assert cd.mac_handoff(root=root, smtp_factory=courier) == {"state": "idle"}
    import os
    t = root / "config" / "token.json"
    os.utime(t, (t.stat().st_mtime + 60, t.stat().st_mtime + 60))           # a new calendar sign-in on the Mac
    assert cd.mac_handoff(root=root, smtp_factory=courier, now=datetime(2026, 11, 4, 9, 0))["sent"] == "config"
    assert cd.latest_bundle(ME, "pw", "config")[0] == "20261104T090000"


def test_the_cloud_waits_for_the_handoff_then_runs_from_its_own_encrypted_state(courier, tmp_path, monkeypatch):
    import db.database as database
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    monkeypatch.setenv("WHOLESALING_DATABASE_URL", f"sqlite:///{cloud / 'wholesaling.db'}")
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_SessionFactory", None)
    monkeypatch.setattr(cd, "_reload", lambda root: None)
    ran = []
    monkeypatch.setattr(cd, "_work", lambda task, **kw: ran.append(task) or {"sent": 0})
    assert cd.cloud_run(root=cloud, smtp_factory=courier) == {"state": "waiting_for_handoff", "ready_posted": True}
    assert cd.cloud_run(root=cloud, smtp_factory=courier) == {"state": "waiting_for_handoff"}       # asks once
    mac = _root(tmp_path)
    cd.mac_handoff(root=mac, smtp_factory=courier)
    r = cd.cloud_run(root=cloud, smtp_factory=courier)
    assert r["state"] == "ok" and r["restored"] and ran == ["run"]
    assert (cloud / "data" / "cloud" / "state.enc").exists()
    assert b"Jordan Operator" not in (cloud / "data" / "cloud" / "state.enc").read_bytes()     # encrypted at rest
    # The next run on a fresh runner: only the cached state.enc, no bundle needed.
    for p in (cloud / "wholesaling.db", cloud / "config" / "settings.local.yaml"):
        p.unlink()
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_SessionFactory", None)
    monkeypatch.setattr(cd, "latest_bundle", lambda user, pw, kind, **kw: None)
    r = cd.cloud_run(root=cloud, smtp_factory=courier)
    assert r["state"] == "ok" and "restored" not in r and (cloud / "config" / "settings.local.yaml").exists()
    assert sqlite3.connect(cloud / "wholesaling.db").execute("select count(*) from t").fetchone()[0] == 3


def test_without_its_secrets_the_cloud_desk_does_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("DESK_KEY", raising=False)
    assert cd.cloud_run(root=tmp_path) == {"state": "not_configured"}
