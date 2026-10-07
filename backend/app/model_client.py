import json
import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import httpx
#这个库跟requests库很像，但是对现代同步/异步HTTP支持更完整

from .config import settings
from .prompts import CHAT_PROMPT, MINUTES_PROMPT, SYSTEM_PROMPT
from .schemas import ActionItem, ChatInterpretation, MinutesResult, ReferencedText
from .services import apply_revision, looks_like_question, looks_like_revision

#模型调用统一异常：异常抽象，上层不需要管具体是什么异常
#包括API key，dpsk超时，HTTP500，返回格式错误，返回空内容，JSON不合法，pydantic校验失败
class ModelError(RuntimeError):
    pass

#模型客户端接口/抽象基类。类似java的interface
#任何modelClient类都需要提供以下两个功能
class ModelClient(ABC):
    @abstractmethod
    def generate_minutes(self, chunks: list[dict]) -> MinutesResult: ...

    @abstractmethod
    def interpret(
        self,
        message: str,
        chunks: list[dict],
        minutes: dict[str, Any] | None,
        history: list[dict[str, str]],
        previous_target: str | None,
    ) -> ChatInterpretation: ...

#dpsk API的网络适配器
class DeepSeekModelClient(ModelClient):
    #真正向deepseek API发送HTTP请求
    def _request(self, messages: list[dict[str, str]]) -> str:
        #1.API key配置
        if not settings.deepseek_api_key:
            raise ModelError("未配置 DEEPSEEK_API_KEY")
        try:
            response = httpx.post(
                #2.URL
                f"{settings.base_url.rstrip('/')}/chat/completions",
                #3.鉴权，API身份认证
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                #4.请求体
                json={ 
                    "model": settings.model_name,
                    "response_format": {"type": "json_object"},
                    "messages": messages,
                    "temperature": 0.1,
                },
                #5.超时
                timeout=settings.model_timeout_seconds,
            )
            #6.HTTP错误检查
            response.raise_for_status()
            #7.读取模型结果
            content = response.json()["choices"][0]["message"]["content"]
            if not content:
                raise ModelError("模型返回空内容")
            return content
        except (
            httpx.TimeoutException,
            httpx.HTTPStatusError,
            httpx.RequestError,
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            raise ModelError(f"模型调用失败：{exc}") from exc
    #确保模型输出合法JSON
    def _complete_json(self, user_content: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        #用户输入+系统提示词传给dpsk
        content = self._request(messages)
        #看JSON格式是否合法
        try:
            json.loads(content)
            return content
        except json.JSONDecodeError:
            # 每次请求最多修复一次，避免异常上游造成无限循环。
            repaired = self._request(
                [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "上次输出不是有效 JSON。请只返回符合原要求的 JSON，不要添加解释。",
                    },
                ]
            )
            try:
                json.loads(repaired)
            except json.JSONDecodeError as exc:
                raise ModelError("模型连续两次返回了非 JSON 内容") from exc
            return repaired
    #会议纪要
    def generate_minutes(self, chunks: list[dict]) -> MinutesResult:
        chunk_text = "\n".join(f"[{c['id']}] {c['text']}" for c in chunks)
        try:
            return MinutesResult.model_validate_json(
                self._complete_json(MINUTES_PROMPT.format(chunks=chunk_text))
            )
        except ValueError as exc:
            raise ModelError(f"模型返回不符合结构要求：{exc}") from exc

    def interpret(
        self,
        message: str, #用户消息
        chunks: list[dict], #原文
        minutes: dict[str, Any] | None, #纪要
        history: list[dict[str, str]], #聊天历史
        previous_target: str | None, #上一轮修改的是什么待办事项
    ) -> ChatInterpretation:
        chunk_text = "\n".join(f"[{c['id']}] {c['text']}" for c in chunks)   #历史记忆保存：
        history_text = "\n".join(f"{item['role']}: {item['content']}" for item in history[-6:]) or "无"
        #把传入的参数全都传入chat_prompt
        prompt = CHAT_PROMPT.format(
            minutes=json.dumps(minutes, ensure_ascii=False, default=str) if minutes else "尚未生成纪要",
            chunks=chunk_text,
            history=history_text + (f"\n上次修订目标: {previous_target}" if previous_target else ""),
            message=message,
        )
        #自然语言转化为结构化转译
        try:
            return ChatInterpretation.model_validate_json(self._complete_json(prompt))
        except ValueError as exc:
            raise ModelError(f"对话结果不符合结构要求：{exc}") from exc


class FakeModelClient(ModelClient):
    """离线演示客户端。结果会由 API 明确标记为 fake，不冒充真实模型。"""
    #用确定性规则+正则化提取关键字，来完成模型理解的功能，在外部连接挂掉的情况下完成测试
    task_words = ("负责", "完成", "跟进", "整理", "提交", "准备", "更新")

    def generate_minutes(self, chunks: list[dict]) -> MinutesResult:
        sentences: list[tuple[str, str]] = []
        for chunk in chunks:
            for sentence in re.split(r"[\n。；;]+", chunk["text"]):
                if sentence.strip():
                    sentences.append((sentence.strip(), chunk["id"]))

        decisions = [
            ReferencedText(text=text, source_refs=[ref])
            for text, ref in sentences
            if any(word in text for word in ("决定", "确定", "决议"))
            and "可能" not in text
        ]
        actions: list[ActionItem] = []
        for text, ref in sentences:
            if not any(word in text for word in self.task_words):
                continue
            owner_match = re.search(r"(?:负责人[：:]?|由)\s*([\u4e00-\u9fa5A-Za-z]{2,8})", text)
            date_match = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?", text)
            due_date = None
            if date_match:
                try:
                    due_date = datetime(
                        int(date_match.group(1)),
                        int(date_match.group(2)),
                        int(date_match.group(3)),
                    ).date()
                except ValueError:
                    due_date = None
            actions.append(
                ActionItem(
                    id=f"a{len(actions) + 1}",
                    task=text,
                    owner=owner_match.group(1) if owner_match else "待确认",
                    due_date=due_date,
                    source_refs=[ref],
                )
            )

        refs = list(dict.fromkeys([ref for _, ref in sentences]))
        questions = []
        if any(action.owner == "待确认" for action in actions):
            questions.append("部分待办负责人待确认")
        if any(action.due_date is None for action in actions):
            questions.append("部分待办截止日期待确认")
        summary_text = "；".join(text for text, _ in sentences[:3])[:300]
        return MinutesResult(
            summary=summary_text or "未提取到有效内容",
            decisions=decisions,
            action_items=actions,
            open_questions=questions,
            source_refs=refs,
        )


    def interpret(
        self,
        message: str,
        chunks: list[dict],
        minutes: dict[str, Any] | None,
        history: list[dict[str, str]],
        previous_target: str | None,
    ) -> ChatInterpretation:
        if minutes and not looks_like_question(message):
            try:
                _, changes, target_id = apply_revision(minutes, message, previous_target)
                change_text = "、".join(f"{item['field']}→{item['to']}" for item in changes)
                return ChatInterpretation(
                    intent="revision",
                    answer=f"已理解修改：{change_text}。目标：{target_id}",
                    patches=[],
                )
            except Exception:
                pass
        refs = [chunk["id"] for chunk in chunks[:3]]
        if looks_like_question(message) or not looks_like_revision(message):
            excerpt = " ".join(chunk["text"] for chunk in chunks)[:400]
            answer = f"根据当前材料：{excerpt}" if excerpt else "当前没有可供回答的会议材料。"
            if looks_like_revision(message) and looks_like_question(message) is False:
                answer = "请补充要改第几项、改哪个字段。也可以直接问会议内容。"
                return ChatInterpretation(intent="clarify", answer=answer)
            return ChatInterpretation(intent="question", answer=answer, source_refs=refs)
        return ChatInterpretation(
            intent="clarify",
            answer="请补充要改哪一项待办、改负责人、截止日期还是状态；也可以直接询问会议内容。",
        )


def get_model_client() -> ModelClient:
    return FakeModelClient() if settings.use_fake_model else DeepSeekModelClient()
