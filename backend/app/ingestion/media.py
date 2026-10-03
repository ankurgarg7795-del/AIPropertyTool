"""Media normalisation: classify uploads, sample video keyframes, transcribe audio.

Claude reads images and PDFs natively, so those pass through untouched.
Video and audio are reduced to things Claude can read: JPEG keyframes and a
text transcript. Both external steps are optional (ffmpeg / faster-whisper);
when unavailable the pipeline records a warning and continues.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

MediaKind = Literal["image", "pdf", "video", "audio", "text", "unsupported"]
CLAUDE_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@dataclass
class MediaItem:
    filename: str
    media_type: str
    data: bytes
    kind: MediaKind
    derived_from: str | None = None  # e.g. keyframe of "walkthrough.mp4"
    label: str | None = None


@dataclass
class NormalisedMedia:
    images: list[MediaItem] = field(default_factory=list)
    pdfs: list[MediaItem] = field(default_factory=list)
    transcripts: list[tuple[str, str]] = field(default_factory=list)  # (source filename, text)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def classify(filename: str, content_type: str | None) -> tuple[MediaKind, str]:
    mt = (content_type or "").split(";")[0].strip().lower()
    if not mt or mt == "application/octet-stream":
        mt = mimetypes.guess_type(filename)[0] or ""
    if mt in CLAUDE_IMAGE_TYPES:
        return "image", mt
    if mt.startswith("image/"):  # heic etc. need conversion; flag as unsupported for the MVP
        return "unsupported", mt
    if mt == "application/pdf":
        return "pdf", mt
    if mt.startswith("video/"):
        return "video", mt
    if mt.startswith("audio/"):
        return "audio", mt
    if mt.startswith("text/") or filename.endswith((".txt", ".md")):
        return "text", mt or "text/plain"
    return "unsupported", mt


async def _run(*cmd: str) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode or 0, out or err


async def video_keyframes(item: MediaItem, n: int) -> list[MediaItem]:
    """Sample ``n`` evenly spaced frames (scene-change aware sampling is a later optimisation)."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg not installed")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / ("in" + Path(item.filename).suffix)
        src.write_bytes(item.data)
        code, out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=nw=1:nk=1", str(src))
        if code != 0:
            raise RuntimeError(f"ffprobe failed: {out[:200]!r}")
        duration = float(out.strip() or 0)
        frames: list[MediaItem] = []
        for i in range(n):
            ts = duration * (i + 0.5) / n
            dst = Path(tmp) / f"f{i}.jpg"
            code, _ = await _run("ffmpeg", "-v", "error", "-ss", f"{ts:.2f}", "-i", str(src),
                                 "-frames:v", "1", "-vf", "scale='min(1568,iw)':-2", "-q:v", "3", str(dst))
            if code == 0 and dst.exists():
                frames.append(MediaItem(filename=f"{item.filename}#t={ts:.1f}s", media_type="image/jpeg",
                                        data=dst.read_bytes(), kind="image", derived_from=item.filename,
                                        label=f"video keyframe at {ts:.1f}s of {item.filename}"))
        return frames


async def video_audio_track(item: MediaItem) -> MediaItem | None:
    if not shutil.which("ffmpeg"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / ("in" + Path(item.filename).suffix)
        dst = Path(tmp) / "audio.wav"
        src.write_bytes(item.data)
        code, _ = await _run("ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(dst))
        if code != 0 or not dst.exists() or dst.stat().st_size < 1000:
            return None
        return MediaItem(filename=f"{item.filename}.wav", media_type="audio/wav", data=dst.read_bytes(),
                         kind="audio", derived_from=item.filename)


_whisper = None


async def transcribe(item: MediaItem) -> str:
    """Speech-to-text via faster-whisper (multilingual; handles Hindi/Hinglish).

    Production deployments swap this for a hosted STT (e.g. a GPU worker pool)
    behind the same signature.
    """
    global _whisper
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise RuntimeError("faster-whisper not installed") from e

    def _do() -> str:
        global _whisper
        if _whisper is None:
            _whisper = WhisperModel("small", compute_type="int8")
        with tempfile.NamedTemporaryFile(suffix=Path(item.filename).suffix or ".wav") as f:
            f.write(item.data)
            f.flush()
            segments, _info = _whisper.transcribe(f.name, vad_filter=True)
            return " ".join(s.text.strip() for s in segments)

    return await asyncio.to_thread(_do)


async def normalise(items: list[MediaItem], keyframes: int = 6, transcript_hint: str | None = None) -> NormalisedMedia:
    out = NormalisedMedia()
    if transcript_hint:
        out.transcripts.append(("client-transcript", transcript_hint))
    for item in items:
        if item.kind == "image":
            out.images.append(item)
        elif item.kind == "pdf":
            out.pdfs.append(item)
        elif item.kind == "text":
            out.notes.append(item.data.decode("utf-8", errors="replace"))
        elif item.kind == "video":
            try:
                out.images.extend(await video_keyframes(item, keyframes))
            except RuntimeError as e:
                out.warnings.append(f"{item.filename}: keyframes skipped ({e})")
            audio = await video_audio_track(item)
            if audio:
                try:
                    out.transcripts.append((item.filename, await transcribe(audio)))
                except RuntimeError as e:
                    out.warnings.append(f"{item.filename}: narration not transcribed ({e})")
        elif item.kind == "audio":
            try:
                out.transcripts.append((item.filename, await transcribe(item)))
            except RuntimeError as e:
                out.warnings.append(f"{item.filename}: not transcribed ({e}); send `transcript` field instead")
        else:
            out.warnings.append(f"{item.filename}: unsupported type {item.media_type}")
    return out
