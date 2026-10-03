"""Content-addressed media storage.

``LocalMediaStorage`` writes under ``APT_MEDIA_DIR``; production swaps in an
S3/GCS implementation of the same two methods (public media bucket behind the
CDN, legal documents in a separate KMS-encrypted bucket served only through
short-lived signed URLs).
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from typing import Protocol


class MediaStorage(Protocol):
    async def put(self, data: bytes, filename: str, media_type: str, private: bool = False) -> str: ...

    async def get(self, key: str) -> bytes: ...


class LocalMediaStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid media key")
        return p

    async def put(self, data: bytes, filename: str, media_type: str, private: bool = False) -> str:
        digest = hashlib.sha256(data).hexdigest()
        ext = Path(filename).suffix.lower() or mimetypes.guess_extension(media_type) or ""
        if not ext[1:].isalnum():
            ext = ""
        key = f"{'private' if private else 'public'}/{digest[:2]}/{digest}{ext}"
        path = self._path(key)
        if not path.exists():  # content-addressed: identical uploads dedupe for free
            await asyncio.to_thread(self._write, path, data)
        return key

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)
