"""Encrypted database snapshot: carry wholesaling.db from the cloud session to the operator's laptop as one file.

    python manage.py db-sync --pack                 # VACUUM INTO -> xz -> AES-256-GCM -> data/snapshots/
    python manage.py db-sync --unpack [PASSPHRASE]  # the reverse, into wholesaling.db (never over real data
                                                    # without --force, which backs the local copy up first)
    python manage.py db-sync --status               # what the snapshot holds vs the local database

The snapshot is encrypted with AES-256-GCM (authenticated: a wrong passphrase or a single changed byte fails before
anything is written) under a key derived from a random 32-character passphrase by PBKDF2-HMAC-SHA256 (600,000
iterations, random salt). The passphrase is kept in .env as DB_SYNC_KEY (git-ignored, chmod 600), never in the
snapshot or the manifest. Whoever holds the passphrase and the snapshot can read every name in the database, so
share it only over a private channel; if it leaks, re-pack with --new-key. data/snapshots/ is git-ignored while the repository is public:
the file travels as a private download (commit it only to a private repository, deliberately, with git add -f).

File layout: header (magic, PBKDF2 iterations, salt, nonce; authenticated as associated data) + ciphertext, split
into parts under 45 MB (GitHub's 50 MB warning) when large. manifest.json carries sizes, SHA-256 sums and table
counts only.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import lzma
import os
import secrets
import shutil
import sqlite3
import struct
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import (
    DEFAULT_DB_PATH,
    ENV_FILE,
    ROOT,
    database_url,
    load_dotenv,
    write_env,
)

SNAPSHOT_DIR = ROOT / "data" / "snapshots"
SNAPSHOT_NAME = "wholesaling.db.xz.enc"
MANIFEST = "manifest.json"
KEY_VAR = "DB_SYNC_KEY"
MAGIC = b"NJWSDB\x00\x01"
SALT_BYTES, NONCE_BYTES = 16, 12
HEADER = struct.Struct(f">8sI{SALT_BYTES}s{NONCE_BYTES}s")
ITERATIONS = 600_000                     # OWASP's PBKDF2-HMAC-SHA256 figure
CHUNK_BYTES = 45_000_000                 # GitHub warns at 50 MB per file and refuses 100 MB
XZ_PRESET = 9 | lzma.PRESET_EXTREME
# No look-alike characters (0/O, 1/l/I) so the passphrase can be typed from a phone screen: 57^32 ~ 2^186.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
PASSPHRASE_LEN = 32
COUNT_TABLES = ("properties", "leads", "valuations", "contacts", "cash_buyers", "jv_partners", "interactions")
_READ = 4 * 1024 * 1024


class SnapshotError(RuntimeError):
    pass


@dataclass
class PackResult:
    files: list[Path]
    db_bytes: int
    packed_bytes: int
    seconds: float
    new_key: bool


def new_passphrase() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(PASSPHRASE_LEN))


def derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32)


def default_db_path() -> Path | None:
    """The SQLite file the app uses (None when WHOLESALING_DATABASE_URL points at a server database)."""
    url = database_url()
    if not url.startswith("sqlite:///"):
        return None
    path = url[len("sqlite:///"):]
    return Path(path) if path else DEFAULT_DB_PATH


def stored_passphrase(env_path: Path = ENV_FILE) -> str | None:
    load_dotenv(env_path)
    value = (os.environ.get(KEY_VAR) or "").strip()
    return value or None


def save_passphrase(passphrase: str, env_path: Path = ENV_FILE) -> None:
    write_env({KEY_VAR: passphrase}, env_path)
    os.environ[KEY_VAR] = passphrase


def snapshot_files(directory: Path = SNAPSHOT_DIR) -> list[Path]:
    """The single snapshot file, or its parts in order."""
    single = directory / SNAPSHOT_NAME
    if single.is_file():
        return [single]
    return sorted(directory.glob(SNAPSHOT_NAME + ".part*"))


def read_manifest(directory: Path = SNAPSHOT_DIR) -> dict[str, Any] | None:
    path = directory / MANIFEST
    return json.loads(path.read_text()) if path.is_file() else None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_READ), b""):
            h.update(block)
    return h.hexdigest()


def _table_counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in COUNT_TABLES if t in have}
    finally:
        con.close()


def has_data(path: Path) -> bool:
    """True when the file is a database with at least one property in it (an empty schema doesn't count)."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        return _table_counts(path).get("properties", 0) > 0
    except sqlite3.DatabaseError:
        return True                       # unreadable, not empty: never treat it as safe to replace


def needs_restore(db_path: Path | None = None, directory: Path = SNAPSHOT_DIR) -> bool:
    db_path = db_path or default_db_path()
    return db_path is not None and bool(snapshot_files(directory)) and not has_data(db_path)


# -- pack ------------------------------------------------------------------------------------------
def pack(db_path: Path | None = None, directory: Path = SNAPSHOT_DIR, *, passphrase: str | None = None,
         new_key: bool = False, env_path: Path = ENV_FILE, iterations: int = ITERATIONS,
         chunk_bytes: int = CHUNK_BYTES, preset: int = XZ_PRESET) -> PackResult:
    """Consistent copy (VACUUM INTO, safe while the cockpit is running) -> xz -> AES-256-GCM -> parts + manifest."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    started = datetime.now()
    db_path = db_path or default_db_path()
    if db_path is None or not db_path.is_file():
        raise SnapshotError(f"no SQLite database at {db_path}")
    made_key = False
    if passphrase is None:
        passphrase = None if new_key else stored_passphrase(env_path)
        if passphrase is None:
            passphrase, made_key = new_passphrase(), True
            save_passphrase(passphrase, env_path)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / ".vacuum.tmp.db"
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(str(db_path), timeout=120)
    try:
        con.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        con.close()
    try:
        db_bytes = tmp.stat().st_size
        db_sha = _sha256_file(tmp)
        counts = _table_counts(tmp)
        comp = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=preset)
        chunks: list[bytes] = []
        with tmp.open("rb") as fh:
            for block in iter(lambda: fh.read(_READ), b""):
                chunks.append(comp.compress(block))
        chunks.append(comp.flush())
    finally:
        tmp.unlink(missing_ok=True)
    salt, nonce = secrets.token_bytes(SALT_BYTES), secrets.token_bytes(NONCE_BYTES)
    header = HEADER.pack(MAGIC, iterations, salt, nonce)
    blob = header + AESGCM(derive_key(passphrase, salt, iterations)).encrypt(nonce, b"".join(chunks), header)
    del chunks
    for old in snapshot_files(directory):           # a smaller snapshot must not leave stale parts behind
        old.unlink()
    if len(blob) <= chunk_bytes:
        pieces = [(directory / SNAPSHOT_NAME, blob)]
    else:
        pieces = [(directory / f"{SNAPSHOT_NAME}.part{i // chunk_bytes + 1:02d}", blob[i:i + chunk_bytes])
                  for i in range(0, len(blob), chunk_bytes)]
    for path, data in pieces:
        path.write_bytes(data)
    manifest = {
        "format": "NJWSDB1", "created_at": started.isoformat(timespec="seconds"),
        "cipher": "AES-256-GCM", "kdf": "PBKDF2-HMAC-SHA256", "iterations": iterations,
        "compression": "xz", "db_bytes": db_bytes, "db_sha256": db_sha, "packed_bytes": len(blob),
        "files": [{"name": p.name, "bytes": len(d), "sha256": hashlib.sha256(d).hexdigest()} for p, d in pieces],
        "tables": counts,
    }
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    return PackResult([p for p, _ in pieces], db_bytes, len(blob), (datetime.now() - started).total_seconds(),
                      made_key)


# -- unpack ----------------------------------------------------------------------------------------
def _read_blob(directory: Path) -> bytes:
    files = snapshot_files(directory)
    if not files:
        raise SnapshotError(f"no snapshot in {directory} (run `git pull` first)")
    manifest = read_manifest(directory)
    if manifest:
        expected = {f["name"]: f for f in manifest.get("files", [])}
        if sorted(expected) != [p.name for p in files]:
            raise SnapshotError(f"snapshot parts don't match manifest.json: have {[p.name for p in files]}, "
                                f"expected {sorted(expected)} (re-run `git pull`)")
        for p in files:
            if p.stat().st_size != expected[p.name]["bytes"] or _sha256_file(p) != expected[p.name]["sha256"]:
                raise SnapshotError(f"{p.name} is incomplete or changed (re-run `git pull`)")
    return b"".join(p.read_bytes() for p in files)


def unpack(passphrase: str, db_path: Path | None = None, directory: Path = SNAPSHOT_DIR, *,
           force: bool = False, backup_dir: Path | None = None, env_path: Path | None = ENV_FILE) -> dict[str, Any]:
    """Decrypt + decompress the snapshot into db_path. Writes a temp file first: the database is replaced only after
    the GCM tag, the SHA-256 in the manifest and SQLite's quick_check all pass. Saves the passphrase to .env."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    started = datetime.now()
    db_path = db_path or default_db_path()
    if db_path is None:
        raise SnapshotError("WHOLESALING_DATABASE_URL is not a SQLite file: restore into it with its own tools")
    if has_data(db_path) and not force:
        raise SnapshotError(f"{db_path.name} already has data: pass --force to replace it (it is backed up first)")
    blob = _read_blob(directory)
    if len(blob) < HEADER.size or not blob.startswith(MAGIC):
        raise SnapshotError("not a db-sync snapshot (bad header)")
    _, iterations, salt, nonce = HEADER.unpack(blob[:HEADER.size])
    header = blob[:HEADER.size]
    try:
        compressed = AESGCM(derive_key(passphrase.strip(), salt, iterations)).decrypt(nonce, blob[HEADER.size:], header)
    except InvalidTag:
        raise SnapshotError("wrong passphrase, or the snapshot was damaged: nothing was written") from None
    del blob
    tmp = db_path.with_name(db_path.name + ".restoring")
    tmp.unlink(missing_ok=True)
    h = hashlib.sha256()
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    try:
        with tmp.open("wb") as out:
            for i in range(0, len(compressed), _READ):
                data = dec.decompress(compressed[i:i + _READ])
                h.update(data)
                out.write(data)
        manifest = read_manifest(directory) or {}
        if manifest.get("db_sha256") and h.hexdigest() != manifest["db_sha256"]:
            raise SnapshotError("restored database doesn't match the manifest checksum: nothing was replaced")
        con = sqlite3.connect(str(tmp))
        try:
            check = con.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            con.close()
        if check != "ok":
            raise SnapshotError(f"restored database failed SQLite's quick_check ({check}): nothing was replaced")
        backup = None
        if db_path.exists():
            backup_dir = backup_dir or db_path.parent / "data" / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            backup = backup_dir / f"{db_path.stem}_{stamp}_before_unpack.db"
            shutil.move(str(db_path), backup)
            for side in ("-wal", "-shm"):
                extra = db_path.with_name(db_path.name + side)
                if extra.exists():
                    shutil.move(str(extra), backup.with_name(backup.name + side))
        os.replace(tmp, db_path)
    finally:
        tmp.unlink(missing_ok=True)
    if env_path is not None and stored_passphrase(env_path) != passphrase.strip():
        save_passphrase(passphrase.strip(), env_path)
    return {"db": str(db_path), "bytes": db_path.stat().st_size, "backup": str(backup) if backup else None,
            "tables": _table_counts(db_path), "created_at": manifest.get("created_at"),
            "seconds": round((datetime.now() - started).total_seconds(), 1)}


def ask_passphrase(prompt: str = "Database passphrase (DB_SYNC_KEY): ") -> str | None:
    """Prompt once on an interactive terminal; None when there is nobody to ask (background launch, CI)."""
    if not sys.stdin or not sys.stdin.isatty():
        return None
    try:
        return getpass.getpass(prompt).strip() or None
    except (EOFError, KeyboardInterrupt):
        return None


def ensure_database(db_path: Path | None = None, directory: Path = SNAPSHOT_DIR, *, interactive: bool = True,
                    env_path: Path = ENV_FILE) -> dict[str, Any] | None:
    """Laptop start-up: no database (or an empty one) and a snapshot on disk -> unpack it with DB_SYNC_KEY from .env,
    else ask once. Returns the unpack summary, None when nothing had to be done. Raises SnapshotError when a
    restore is needed but can't happen (so the caller doesn't start on an empty database by mistake)."""
    db_path = db_path or default_db_path()
    if not needs_restore(db_path, directory):
        return None
    passphrase = stored_passphrase(env_path) or (ask_passphrase() if interactive else None)
    if not passphrase:
        where = directory.relative_to(ROOT) if directory.is_relative_to(ROOT) else directory
        raise SnapshotError(f"{db_path.name} is missing and a snapshot is waiting in {where}: run "
                            f"`python manage.py db-sync --unpack` (or put {KEY_VAR}=... in .env)")
    return unpack(passphrase, db_path, directory, force=True, env_path=env_path)
