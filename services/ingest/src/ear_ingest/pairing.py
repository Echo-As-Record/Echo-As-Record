"""Receiver identity, short-lived invitations, and device-scoped credentials."""

import hmac
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

DEFAULT_HOME = "~/Library/Application Support/EarIngest/pairing"


class Pairing:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.database = directory / "devices.sqlite3"
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS receiver(id INTEGER PRIMARY KEY, uuid TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS invites(secret TEXT PRIMARY KEY, expires REAL NOT NULL,
                    device TEXT);
                CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, token TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO receiver VALUES(1,?)", (str(uuid.uuid4()),))
            self.id = db.execute("SELECT uuid FROM receiver WHERE id=1").fetchone()[0]

    def connect(self):
        return sqlite3.connect(self.database, timeout=10)

    def certificate(self):
        """Serialize first-time certificate creation with the same database lock."""
        cert, key = self.directory / "receiver.pem", self.directory / "receiver.key"
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not cert.exists():
                private = ec.generate_private_key(ec.SECP256R1())
                name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.id)])
                now = datetime.now(UTC)
                leaf = (
                    x509.CertificateBuilder()
                    .subject_name(name)
                    .issuer_name(name)
                    .public_key(private.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(days=1))
                    .not_valid_after(now + timedelta(days=3650))
                    .sign(private, hashes.SHA256())
                )
                for path, data in [
                    (
                        key,
                        private.private_bytes(
                            serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption(),
                        ),
                    ),
                    (cert, leaf.public_bytes(serialization.Encoding.PEM)),
                ]:
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(data)
                    tmp.chmod(0o600)
                    os.replace(tmp, path)
            pin = (
                x509.load_pem_x509_certificate(cert.read_bytes()).fingerprint(hashes.SHA256()).hex()
            )
        return cert, key, pin

    def invite(self, relay: str):
        relay = relay.replace("{receiver}", self.id)
        if relay:
            u = urlsplit(relay)
            if (
                (
                    u.scheme != "https"
                    and not (
                        u.scheme == "http"
                        and relay.rstrip("/")
                        == os.environ.get("EAR_INGEST_HTTP_RELAY", "").rstrip("/")
                    )
                )
                or not u.hostname
                or u.username
                or u.password
                or u.path not in ("", "/")
                or u.query
                or u.fragment
            ):
                raise ValueError("Use HTTPS or explicitly configure EAR_INGEST_HTTP_RELAY")
        secret = secrets.token_hex(32)
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM invites WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO invites VALUES(?,?,NULL)", (secret, time.time() + 300))
        return "earrecorder://pair?" + urlencode(
            dict(receiver=self.id, pin=self.certificate()[2], code=secret, relay=relay)
        )

    def claim(self, code: str, device: str):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT device FROM invites WHERE secret=? AND expires>=?", (code, time.time())
            ).fetchone()
            if row is None or row[0] not in (None, device):
                return None
            db.execute("UPDATE invites SET device=? WHERE secret=?", (device, code))
            # A lost reply can be replayed by the same device during the invitation lifetime.
            token = hmac.digest(code.encode(), ("ear-pair-v1:" + device).encode(), "sha256").hex()
            if row[0] is None:
                db.execute("INSERT OR REPLACE INTO devices VALUES(?,?)", (device, token))
            return token if self.token(db, device) == token else None

    @staticmethod
    def token(db, device):
        row = db.execute("SELECT token FROM devices WHERE id=?", (device,)).fetchone()
        return row[0] if row else None

    def credential(self, device):
        with closing(self.connect()) as db:
            return self.token(db, device)
