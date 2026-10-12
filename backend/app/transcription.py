from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import settings


ALLOWED_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".mp4", ".webm", ".ogg"}


class TranscriptionError(RuntimeError):
    """Stable application error for all upstream transcription failures."""


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    mode: str
    language: str | None = None


class TranscriptionClient(ABC):
    @abstractmethod
    def transcribe(self, filename: str, data: bytes, content_type: str | None = None) -> TranscriptionResult: ...


def validate_audio(filename: str, data: bytes) -> str:
    if not data:
        raise ValueError("音频文件不能为空")
    if len(data) > settings.max_audio_bytes:
        raise ValueError(f"音频文件超过 {settings.max_audio_bytes // 1024 // 1024} MB 限制")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_AUDIO_SUFFIXES:
        raise ValueError("仅支持 .mp3、.wav、.m4a、.mp4、.webm、.ogg 音频")
    return suffix


class FakeTranscriptionClient(TranscriptionClient):
    """Offline adapter. UTF-8 fixture bytes become the transcript; binary audio gets an explicit demo transcript."""

    def transcribe(self, filename: str, data: bytes, content_type: str | None = None) -> TranscriptionResult:
        validate_audio(filename, data)
        try:
            decoded = data.decode("utf-8-sig").strip()
        except UnicodeDecodeError:
            decoded = ""
        text = decoded or "离线模拟转写：请配置真实语音转写服务以识别该音频内容。"
        return TranscriptionResult(text=text, mode="fake", language="zh")


class HttpTranscriptionClient(TranscriptionClient):
    """OpenAI-compatible speech-to-text adapter configured only through environment variables."""

    def transcribe(self, filename: str, data: bytes, content_type: str | None = None) -> TranscriptionResult:
        validate_audio(filename, data)
        if not settings.stt_api_key or not settings.stt_base_url or not settings.stt_model:
            raise TranscriptionError("真实转写模式需要配置 STT_API_KEY、STT_BASE_URL 和 STT_MODEL")
        try:
            response = httpx.post(
                f"{settings.stt_base_url.rstrip('/')}/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.stt_api_key}"},
                data={"model": settings.stt_model},
                files={"file": (Path(filename).name, data, content_type or "application/octet-stream")},
                timeout=settings.stt_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            text = str(payload.get("text") or "").strip()
            if not text:
                raise TranscriptionError("转写服务返回了空文本")
            return TranscriptionResult(text=text, mode="remote", language=payload.get("language"))
        except TranscriptionError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise TranscriptionError(f"语音转写失败：{exc}") from exc


def get_transcription_client() -> TranscriptionClient:
    return FakeTranscriptionClient() if settings.use_fake_transcription else HttpTranscriptionClient()
