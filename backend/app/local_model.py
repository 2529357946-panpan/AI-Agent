import hashlib
import math
import re
from abc import ABC, abstractmethod

import httpx

from .config import settings
from .schemas import InputInspection


class LocalModelError(RuntimeError):
    pass


class EmbeddingError(RuntimeError):
    pass


PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
INJECTION_WORDS = ("忽略之前", "忽略以上", "system prompt", "系统提示词", "绕过规则")


def deterministic_inspection(query: str) -> InputInspection:
    normalized = " ".join(query.strip().split())
    sensitive = bool(PHONE_RE.search(normalized) or EMAIL_RE.search(normalized))
    injection = any(word.lower() in normalized.lower() for word in INJECTION_WORDS)
    reasons = []
    if sensitive:
        reasons.append("检测到手机号或邮箱格式")
    if injection:
        reasons.append("检测到疑似提示词注入表达")
    return InputInspection(
        valid=bool(normalized),
        contains_sensitive_info=sensitive,
        suspected_prompt_injection=injection,
        normalized_query=normalized,
        reason="；".join(reasons),
    )


class LocalModelClient(ABC):
    @abstractmethod
    def inspect_input(self, query: str) -> InputInspection: ...

    @abstractmethod
    def generate_hypothetical(self, query: str) -> str: ...


class FakeLocalModelClient(LocalModelClient):
    """网络无关的确定性实现，结果只用于离线演示和测试。"""

    def inspect_input(self, query: str) -> InputInspection:
        return deterministic_inspection(query)

    def generate_hypothetical(self, query: str) -> str:
        normalized = self.inspect_input(query).normalized_query
        return f"与问题“{normalized}”相关的历史会议记录或演示制度会说明处理步骤、负责人和确认要求。"


class OllamaLocalModelClient(LocalModelClient):
    def _generate(self, prompt: str, *, json_mode: bool = False) -> str:
        if not settings.ollama_model:
            raise LocalModelError("未配置 OLLAMA_MODEL")
        try:
            response = httpx.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0, "seed": 0},
                    **({"format": "json"} if json_mode else {}),
                },
                timeout=settings.model_timeout_seconds,
            )
            response.raise_for_status()
            content = response.json()["response"].strip()
            if not content:
                raise LocalModelError("Ollama 返回空内容")
            return content
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise LocalModelError(f"Ollama 调用失败：{exc}") from exc

    def inspect_input(self, query: str) -> InputInspection:
        rules = deterministic_inspection(query)
        prompt = (
            "检查下面查询，仅输出 JSON，字段为 valid、contains_sensitive_info、"
            "suspected_prompt_injection、normalized_query、reason。该判断只是风险信号。\n"
            f"查询：{query}"
        )
        try:
            model_result = InputInspection.model_validate_json(self._generate(prompt, json_mode=True))
        except (ValueError, LocalModelError):
            return rules
        return InputInspection(
            valid=rules.valid and model_result.valid,
            contains_sensitive_info=rules.contains_sensitive_info or model_result.contains_sensitive_info,
            suspected_prompt_injection=rules.suspected_prompt_injection or model_result.suspected_prompt_injection,
            normalized_query=model_result.normalized_query.strip() or rules.normalized_query,
            reason="；".join(filter(None, [rules.reason, model_result.reason])),
        )

    def generate_hypothetical(self, query: str) -> str:
        return self._generate(
            "为向量检索生成不超过80个汉字的假设性相关文档。只扩展主题、流程字段和同义表达，"
            "不要直接回答问题，不要猜测具体人名、机构、日期、密码、负责人或结论。\n"
            f"查询：{query}"
        )


class EmbeddingClient(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]: ...


class FakeEmbeddingClient(EmbeddingClient):
    """基于字符 n-gram 哈希的固定维向量；可复现且能保留有限词面相似度。"""

    def __init__(self, dimensions: int = 64):
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        normalized = re.sub(r"\s+", "", text.lower())
        grams = list(normalized) + [normalized[i : i + 2] for i in range(max(0, len(normalized) - 1))]
        vector = [0.0] * self.dimensions
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "big") % self.dimensions
            vector[index] += -1.0 if digest[2] & 1 else 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


class OllamaEmbeddingClient(EmbeddingClient):
    def embed(self, text: str) -> list[float]:
        if not settings.ollama_embed_model:
            raise EmbeddingError("未配置 OLLAMA_EMBED_MODEL")
        try:
            response = httpx.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/embed",
                json={"model": settings.ollama_embed_model, "input": text},
                timeout=settings.model_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            vector = payload.get("embeddings", [None])[0]
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("Ollama 没有返回 embedding")
            return [float(value) for value in vector]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"Ollama embedding 调用失败：{exc}") from exc


def get_local_model_client() -> LocalModelClient:
    return OllamaLocalModelClient() if settings.ollama_enabled else FakeLocalModelClient()


def get_embedding_client() -> EmbeddingClient:
    return OllamaEmbeddingClient() if settings.ollama_enabled else FakeEmbeddingClient()
