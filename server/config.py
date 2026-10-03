"""
Server configuration, read from environment variables.

Everything deploy-specific comes from the environment rather than a file so
the same image runs on a laptop, a VPS, or a PaaS without edits. Defaults are
chosen for local development; production should at least set SECRET_KEY.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, **overrides):
        data_dir = Path(os.environ.get("DATA_DIR", "data"))

        self.DATA_DIR = data_dir
        # SQLite is fine for a handful of users; point this at Postgres
        # (postgresql+psycopg://...) for anything bigger.
        self.DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{data_dir / 'server.db'}")
        # Some hosts hand out "postgres://", which SQLAlchemy 2 rejects.
        if self.DATABASE_URL.startswith("postgres://"):
            self.DATABASE_URL = "postgresql+psycopg://" + self.DATABASE_URL[len("postgres://"):]

        # "local" keeps media on disk under MEDIA_DIR; "s3" uses any
        # S3-compatible bucket (AWS, Cloudflare R2, Backblaze B2, MinIO).
        self.STORAGE = os.environ.get("STORAGE", "local")
        self.MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", str(data_dir / "media")))
        self.S3_BUCKET = os.environ.get("S3_BUCKET", "")
        self.S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL") or None
        self.S3_REGION = os.environ.get("S3_REGION") or None

        self.SECRET_KEY = os.environ.get("SECRET_KEY", "")
        self.ALLOW_SIGNUP = _bool("ALLOW_SIGNUP", True)
        # Behind a reverse proxy / PaaS router, trust its X-Forwarded-* headers
        # so rate limiting sees real client IPs and redirects keep https.
        self.TRUST_PROXY = _bool("TRUST_PROXY", False)
        # Session cookies only over https. Off by default so plain
        # http://localhost works; turn on for any internet deployment.
        self.SECURE_COOKIES = _bool("SECURE_COOKIES", False)
        # Events (and their clips) older than this are deleted. 0 keeps forever.
        self.RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
        self.MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))

        for k, v in overrides.items():
            setattr(self, k, v)

        if not self.SECRET_KEY:
            self.SECRET_KEY = _dev_secret(self.DATA_DIR)


def _dev_secret(data_dir: Path) -> str:
    """Persisted random key so local sessions survive a restart. On a real
    deployment set SECRET_KEY instead -- container disks are often wiped."""
    p = data_dir / "secret_key"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        return p.read_text().strip()
    key = secrets.token_urlsafe(48)
    p.write_text(key)
    print("[server] SECRET_KEY not set; generated one in "
          f"{p}. Set SECRET_KEY explicitly in production.", file=sys.stderr)
    return key
