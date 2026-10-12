import hashlib
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class CacheError(RuntimeError):
    pass


def make_cache_key(operation: str, model_input: Any, model: str, prompt_version: str) -> str:
    canonical = json.dumps(
        {
            "operation": operation,
            "model_input": model_input,
            "model": model,
            "prompt_version": prompt_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CacheClient(ABC):
    @abstractmethod
    def get(self, cache_key: str) -> dict | None: ...

    @abstractmethod
    def set(
        self,
        cache_key: str,
        *,
        operation: str,
        model: str,
        prompt_version: str,
        result: dict,
    ) -> None: ...


class NoopCacheClient(CacheClient):
    def get(self, cache_key: str) -> dict | None:
        return None

    def set(self, cache_key: str, **kwargs) -> None:
        return None


class InMemoryCacheClient(CacheClient):
    def __init__(self):
        self._values: dict[str, dict] = {}
        self._lock = Lock()

    def get(self, cache_key: str) -> dict | None:
        with self._lock:
            item = self._values.get(cache_key)
            if not item:
                return None
            if item["expires_at"] < datetime.now(timezone.utc):
                self._values.pop(cache_key, None)
                return None
            item["hit_count"] += 1
            return json.loads(json.dumps(item["result"], ensure_ascii=False))

    def set(self, cache_key: str, **kwargs) -> None:
        with self._lock:
            self._values[cache_key] = {
                "result": kwargs["result"],
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=settings.cache_ttl_seconds),
                "hit_count": 0,
            }


class SupabaseCacheClient(CacheClient):
    def __init__(self, base_url: str | None = None, key: str | None = None):
        self.base_url = (base_url or settings.supabase_url).rstrip("/")
        self.key = key or settings.supabase_key
        if not self.base_url or not self.key:
            raise CacheError("Supabase cache 未配置 URL 或 Key")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def get(self, cache_key: str) -> dict | None:
        try:
            response = httpx.get(
                f"{self.base_url}/rest/v1/result_cache",
                headers=self.headers,
                params={
                    "cache_key": f"eq.{cache_key}",
                    "expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}",
                    "select": "result_json,hit_count",
                    "limit": "1",
                },
                timeout=8,
            )
            response.raise_for_status()
            rows = response.json()
            if not rows:
                return None
            httpx.patch(
                f"{self.base_url}/rest/v1/result_cache",
                headers=self.headers,
                params={"cache_key": f"eq.{cache_key}"},
                json={"hit_count": int(rows[0].get("hit_count", 0)) + 1},
                timeout=8,
            )
            return rows[0]["result_json"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise CacheError(f"Supabase cache lookup 失败：{exc}") from exc

    def set(self, cache_key: str, **kwargs) -> None:
        payload = {
            "cache_key": cache_key,
            "operation": kwargs["operation"],
            "model": kwargs["model"],
            "prompt_version": kwargs["prompt_version"],
            "result_json": kwargs["result"],
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=settings.cache_ttl_seconds)
            ).isoformat(),
        }
        try:
            response = httpx.post(
                f"{self.base_url}/rest/v1/result_cache",
                headers={**self.headers, "Prefer": "resolution=merge-duplicates"},
                params={"on_conflict": "cache_key"},
                json=payload,
                timeout=8,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CacheError(f"Supabase cache write 失败：{exc}") from exc


def get_cache_client() -> CacheClient:
    if not settings.supabase_enabled:
        return NoopCacheClient()
    try:
        return SupabaseCacheClient()
    except CacheError as exc:
        logger.warning("supabase_error cache_init=%s", exc)
        return NoopCacheClient()
