"""
Media storage: snapshots, clips and sounds.

Two backends with the same four methods. Local disk is the default and the
right choice for a single VPS with a persistent volume; S3 (or any
S3-compatible service) is for hosts whose disks are ephemeral, and it lets
the browser fetch clips straight from the bucket instead of through this
server.

Keys are generated here, never taken from a request, so a client cannot
steer a write (or a read) outside its own media.
"""
from __future__ import annotations

import mimetypes
import secrets
import time
from pathlib import Path

from flask import abort, redirect, send_file


def new_key(user_id: int, kind: str, ext: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"u{user_id}/{kind}/{stamp}_{secrets.token_hex(6)}{ext}"


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        # Belt and braces: keys are ours, but never serve outside the root.
        p.relative_to(self.root)
        return p

    def save(self, key: str, fileobj) -> int:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        with open(tmp, "wb") as out:
            while chunk := fileobj.read(1024 * 1024):
                out.write(chunk)
        tmp.replace(p)
        return p.stat().st_size

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def serve(self, key: str, mimetype: str | None = None):
        p = self._path(key)
        if not p.exists():
            abort(404)
        # conditional=True gives byte-range support, which browsers need to
        # seek within a video rather than only play it straight through.
        return send_file(p, mimetype=mimetype or mimetypes.guess_type(p.name)[0],
                         conditional=True, max_age=3600)

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except Exception:
            pass


class S3Storage:
    def __init__(self, bucket: str, endpoint_url: str | None = None, region: str | None = None):
        try:
            import boto3
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("STORAGE=s3 needs boto3: pip install boto3") from e
        if not bucket:
            raise RuntimeError("STORAGE=s3 needs S3_BUCKET")
        self.bucket = bucket
        # Credentials come from the standard AWS_ACCESS_KEY_ID /
        # AWS_SECRET_ACCESS_KEY environment variables.
        self.s3 = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)

    def save(self, key: str, fileobj) -> int:
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        self.s3.upload_fileobj(fileobj, self.bucket, key, ExtraArgs={"ContentType": ctype})
        return int(self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"])

    def read_bytes(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def serve(self, key: str, mimetype: str | None = None):
        # Short-lived signed URL: the bucket stays private, and the browser
        # streams/seeks directly from S3 rather than through this server.
        url = self.s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=600)
        return redirect(url, code=302)

    def delete(self, key: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass


def make_storage(cfg):
    if cfg.STORAGE == "s3":
        return S3Storage(cfg.S3_BUCKET, cfg.S3_ENDPOINT_URL, cfg.S3_REGION)
    return LocalStorage(cfg.MEDIA_DIR)
