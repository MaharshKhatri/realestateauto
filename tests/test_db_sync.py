"""db-sync: the encrypted snapshot round-trips, never leaks a name in plaintext, refuses a wrong passphrase or a
damaged part without touching the database, splits into parts and never overwrites real data unasked."""
import json
import sqlite3

import pytest

from db import snapshot as sn

NAME = "QUINN Q SAMPLE"
FAST = 1_000          # PBKDF2 iterations for tests (the real default is 600,000)


def make_db(path, rows=200, name=NAME):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE properties (id INTEGER PRIMARY KEY, owner_name TEXT, street_address TEXT)")
    con.executemany("INSERT INTO properties (owner_name, street_address) VALUES (?, ?)",
                    [(name if i == 7 else f"OWNER {i}", f"{i} Sample St, Newark") for i in range(rows)])
    con.commit()
    con.close()
    return path


def rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT owner_name, street_address FROM properties ORDER BY id").fetchall()
    finally:
        con.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(sn.KEY_VAR, raising=False)
    return tmp_path / ".env"


def test_round_trip_generates_key_and_hides_names(tmp_path, env):
    src = make_db(tmp_path / "src.db")
    snap = tmp_path / "snapshots"
    r = sn.pack(src, snap, env_path=env, iterations=FAST)
    assert r.new_key and len(r.files) == 1 and r.files[0].name == sn.SNAPSHOT_NAME
    key = sn.stored_passphrase(env)
    assert key and len(key) == sn.PASSPHRASE_LEN and set(key) <= set(sn.ALPHABET)
    assert f"{sn.KEY_VAR}={key}" in env.read_text() and (env.stat().st_mode & 0o777) == 0o600
    blob = r.files[0].read_bytes()
    assert blob.startswith(sn.MAGIC) and NAME.encode() not in blob and b"Sample St" not in blob
    manifest = json.loads((snap / sn.MANIFEST).read_text())
    assert manifest["cipher"] == "AES-256-GCM" and manifest["tables"] == {"properties": 200}
    assert key not in json.dumps(manifest) and NAME not in json.dumps(manifest)

    out = tmp_path / "laptop" / "wholesaling.db"
    out.parent.mkdir()
    res = sn.unpack(key, out, snap, env_path=tmp_path / "laptop.env")
    assert rows(out) == rows(src) and res["tables"] == {"properties": 200} and res["backup"] is None
    assert sn.stored_passphrase(tmp_path / "laptop.env") == key       # the laptop never asks again
    assert not list(out.parent.glob("*.restoring"))


def test_wrong_passphrase_and_damaged_part_write_nothing(tmp_path, env):
    src = make_db(tmp_path / "src.db")
    snap = tmp_path / "snapshots"
    sn.pack(src, snap, passphrase="right-passphrase", env_path=env, iterations=FAST)
    out = tmp_path / "wholesaling.db"
    with pytest.raises(sn.SnapshotError, match="wrong passphrase"):
        sn.unpack("wrong-passphrase", out, snap, env_path=None)
    assert not out.exists()

    f = snap / sn.SNAPSHOT_NAME
    data = bytearray(f.read_bytes())
    data[-5] ^= 0x01
    f.write_bytes(bytes(data))
    with pytest.raises(sn.SnapshotError, match="incomplete or changed"):        # manifest checksum
        sn.unpack("right-passphrase", out, snap, env_path=None)
    (snap / sn.MANIFEST).unlink()
    with pytest.raises(sn.SnapshotError, match="wrong passphrase, or the snapshot was damaged"):   # GCM tag
        sn.unpack("right-passphrase", out, snap, env_path=None)
    assert not out.exists()


def test_large_snapshot_splits_into_parts_and_repack_removes_stale_ones(tmp_path, env):
    import os
    src = tmp_path / "src.db"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE properties (id INTEGER PRIMARY KEY, blob BLOB)")
    con.executemany("INSERT INTO properties (blob) VALUES (?)", [(os.urandom(2000),) for _ in range(100)])
    con.commit()
    con.close()
    snap = tmp_path / "snapshots"
    r = sn.pack(src, snap, passphrase="k", env_path=env, iterations=FAST, chunk_bytes=50_000, preset=1)
    assert len(r.files) >= 4 and all(p.stat().st_size <= 50_000 for p in r.files)
    assert [p.name for p in sn.snapshot_files(snap)] == [f"{sn.SNAPSHOT_NAME}.part{i:02d}" for i in range(1, len(r.files) + 1)]
    out = tmp_path / "out.db"
    sn.unpack("k", out, snap, env_path=None)
    assert sqlite3.connect(out).execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 100

    r.files[1].unlink()                                            # a pull that stopped half-way
    with pytest.raises(sn.SnapshotError, match="don't match manifest"):
        sn.unpack("k", tmp_path / "out2.db", snap, env_path=None)

    small = make_db(tmp_path / "small.db", rows=5)
    r2 = sn.pack(small, snap, passphrase="k", env_path=env, iterations=FAST)
    assert [p.name for p in snap.iterdir() if p.name.startswith(sn.SNAPSHOT_NAME)] == [sn.SNAPSHOT_NAME]
    assert len(r2.files) == 1


def test_never_overwrites_data_without_force_and_backs_up(tmp_path, env):
    src = make_db(tmp_path / "src.db")
    snap = tmp_path / "snapshots"
    sn.pack(src, snap, passphrase="k", env_path=env, iterations=FAST)
    local = make_db(tmp_path / "wholesaling.db", rows=3, name="LOCAL ONLY")
    with pytest.raises(sn.SnapshotError, match="already has data"):
        sn.unpack("k", local, snap, env_path=None)
    assert len(rows(local)) == 3
    res = sn.unpack("k", local, snap, force=True, backup_dir=tmp_path / "backups", env_path=None)
    assert len(rows(local)) == 200 and res["backup"] and len(rows(res["backup"])) == 3


def test_ensure_database_restores_missing_or_empty_db_with_stored_key(tmp_path, env):
    src = make_db(tmp_path / "src.db")
    snap = tmp_path / "snapshots"
    sn.pack(src, snap, env_path=env, iterations=FAST)               # key saved in env
    db = tmp_path / "wholesaling.db"
    sqlite3.connect(db).execute("CREATE TABLE properties (id INTEGER PRIMARY KEY)").connection.commit()
    assert sn.needs_restore(db, snap)                              # an empty schema is not data
    res = sn.ensure_database(db, snap, interactive=False, env_path=env)
    assert res and len(rows(db)) == 200
    assert sn.ensure_database(db, snap, interactive=False, env_path=env) is None   # already there: no-op
    assert not sn.needs_restore(tmp_path / "other.db", tmp_path / "no-snapshots")


def test_ensure_database_without_key_refuses_instead_of_starting_empty(tmp_path, env, monkeypatch):
    src = make_db(tmp_path / "src.db")
    snap = tmp_path / "snapshots"
    sn.pack(src, snap, passphrase="k", env_path=tmp_path / "packer.env", iterations=FAST)
    monkeypatch.delenv(sn.KEY_VAR, raising=False)
    with pytest.raises(sn.SnapshotError, match="db-sync --unpack"):
        sn.ensure_database(tmp_path / "wholesaling.db", snap, interactive=False, env_path=env)
    assert not (tmp_path / "wholesaling.db").exists()


def test_launchers_unlock_the_snapshot_before_starting():
    from scripts import install_desktop_shortcut as sc
    sh, bat = sc.unix_script(), sc.windows_script()
    for text in (sh, bat):
        assert "db-sync --unpack --if-missing" in text
        assert text.index("db-sync --unpack") < text.index("manage.py server")
