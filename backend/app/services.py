import copy
import io
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from docx import Document
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .config import settings
from .db import Meeting, ResultVersion, SourceChunk
from .schemas import FieldPatch, MinutesResult, WeeklyResult


ALLOWED_SUFFIXES = {".txt", ".md", ".docx"}
ORDINALS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

#1.文件输入与预处理
#1.1 文件转文本：文件校验+解析
def parse_document(filename: str, data: bytes) -> tuple[str, str]:
    #大小、文件类型校验
    if len(data) > settings.max_file_bytes:
        raise ValueError("文件超过 10 MB 限制")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError("仅支持 .txt、.md、.docx 文件")
    #docx用python解析，其它用utf-8解码
    try:
        if suffix == ".docx":
            doc = Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
        else:
            text = data.decode("utf-8-sig")
    except (UnicodeDecodeError, ValueError, KeyError) as exc:
        raise ValueError("文件无法解析，请确认编码或文件内容") from exc
    return validate_text(text), suffix.removeprefix(".")

#1.2 文本输入校验
def validate_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("会议材料不能为空")
    if len(text) > settings.max_text_chars:
        raise ValueError(f"文本超过 {settings.max_text_chars} 字符限制")
    return text

#1.3 文档分块：只是构造ORM对象，没有写入db。
def build_chunks(text: str, size: int = 1200) -> list[SourceChunk]:
    #size是每一个切块大概多少字符
    chunks: list[SourceChunk] = [] 
    start = 0
    ordinal = 1
    while start < len(text):#没结束就继续切
        end = min(start + size, len(text))#理想的结束点
        #在[start,end]这个范围内，向后寻找最后一个换行或者句号，优先找最后面的
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end))
            #还是没想明白这是什么设计，且看调试结果
            if boundary > start + size // 2:
                end = boundary + 1
        chunks.append(
            SourceChunk(
                ordinal=ordinal,
                text=text[start:end],
                start_char=start,
                end_char=end,
            )
        )
        ordinal += 1
        start = end
    return chunks
#目前提供分块功能的接口，后续视场景和文档的复杂程度，考虑是否引入其他分块策略：
#比如langchain的递归切块、llamaindex的语义切块/分层切块。

#2.模型安全结果校验：引用来源是否有效
def validate_refs(result: MinutesResult, valid_refs: set[str]) -> None:
    refs = set(result.source_refs)
    for item in [*result.decisions, *result.action_items]:
        refs.update(item.source_refs)
    #集合运算
    invalid = refs - valid_refs
    if invalid:
        raise ValueError(f"模型返回了无效来源引用：{', '.join(sorted(invalid))}")

#3.结果版本管理和数据库查询
#3.1保存新版本
def save_version(
    db: DbSession,
    *,
    meeting_id: str | None,
    kind: str,
    payload: dict[str, Any],
    parent: ResultVersion | None = None,
) -> ResultVersion:
    #相当于SQL找最大的版本号然后+1，产生新版本号，scalar：执行查询，取结果的单个值
    version_no = (
        db.scalar(
            select(func.max(ResultVersion.version_no)).where(
                ResultVersion.meeting_id == meeting_id, ResultVersion.kind == kind
            )
        )
        or 0
    ) + 1
    version = ResultVersion(
        meeting_id=meeting_id,
        kind=kind,
        version_no=version_no,
        payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        parent_version_id=parent.id if parent else None,
    )
    #add：把ORM对象加入数据库事务，但是并没有commit，何时commit由上层代码决定
    db.add(version)
    #flush：将当前待写入的SQL发送到数据库，但是暂时不结束事务
    #这样在commit之前就发现问题
    db.flush()
    return version

#3.2 找会议最新一版纪要
def latest_version(
    db: DbSession, meeting_id: str, kind: str = "minutes"
) -> ResultVersion | None:
    return db.scalar(
        select(ResultVersion)
        .where(ResultVersion.meeting_id == meeting_id, ResultVersion.kind == kind)
        .order_by(ResultVersion.version_no.desc())
        .limit(1)
    )

#4.对话修订引擎
class InvalidDateError(ValueError):
    pass


class AmbiguousInstruction(ValueError):
    pass


QUESTION_HINTS = (
    "吗",
    "？",
    "?",
    "谁",
    "什么",
    "哪些",
    "是否",
    "有没有",
    "为什么",
    "怎么",
    "如何",
    "总结",
    "讲讲",
    "介绍",
    "问问",
    "解释",
)

#第一层：用户意图识别
def looks_like_question(instruction: str) -> bool:
    return any(hint in instruction for hint in QUESTION_HINTS)


def looks_like_revision(instruction: str) -> bool:
    return any(
        word in instruction
        for word in ("改", "换", "把", "将", "更新", "截止", "负责", "状态", "完成")
    )

#第二层：具体修改谁、修改什么
#自然语言提取日期
def parse_due_date(instruction: str) -> str | None:
    if "待确认" in instruction:
        return None
    match = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?", instruction)
    if not match:
        raise InvalidDateError("截止日期需使用有效的 YYYY-MM-DD，例如 2026-10-09")
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError as exc:
        raise InvalidDateError("截止日期不是有效日期") from exc

#定位具体修改目标
def _target_index(instruction: str, actions: list[dict[str, Any]], previous_target: str | None) -> int:
   #a.直接定位第几项 
    match = re.search(r"第\s*(\d+|[一二三四五六七八九十])\s*项", instruction)
    if match:
        raw = match.group(1)
        index = int(raw) if raw.isdigit() else ORDINALS[raw]
        if not 1 <= index <= len(actions):
            raise AmbiguousInstruction(f"第 {index} 项待办不存在")
        return index - 1
    #b.刚才那项
    if any(word in instruction for word in ("刚才那项", "那项", "这项", "刚才那个", "它")) and previous_target:
        for index, item in enumerate(actions):
            if item.get("id") == previous_target:
                return index
    matches = []
    #c.负责人名字、任务内容匹配
    for index, item in enumerate(actions):
        owner = str(item.get("owner") or "")
        task = str(item.get("task") or "")
        if owner and owner != "待确认" and owner in instruction:
            matches.append(index)
        elif len(task) >= 4 and task[:8] in instruction:
            matches.append(index)
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0]
    raise AmbiguousInstruction("请说明要改哪一项待办，或直接描述事项内容")

#修改执行器：自然语言->apply_revision/模型->FieldPatch->执行器
def apply_patches(
    current: dict[str, Any], patches: list[FieldPatch]
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    if not patches:
        raise AmbiguousInstruction("没有可应用的修改")
    #修改目标的副本
    updated = copy.deepcopy(current) 
    #actions指向updated里面的待办事项，因此更改actions就是改updated
    actions = updated.get("action_items", []) 
    changes: list[dict[str, Any]] = []
    last_id = ""
    for patch in patches:
        #target指向的是actions里面的一个list元素，最终也是修改updated
        target = None
        if patch.item_id:
            target = next((item for item in actions if item.get("id") == patch.item_id), None)
        elif patch.item_index:
            if not 1 <= patch.item_index <= len(actions):
                raise AmbiguousInstruction(f"第 {patch.item_index} 项待办不存在")
            target = actions[patch.item_index - 1]
        if not target:
            raise AmbiguousInstruction("找不到要修改的待办，请说明第几项或事项内容")
        #查事项DDL，日期标准化
        value: Any = patch.value
        if patch.field == "due_date":
            if value in (None, "", "待确认", "null"):
                value = None
            else:
                value = parse_due_date(str(value))
        #状态标准化
        elif patch.field == "status":
            allowed = {"unknown", "todo", "in_progress", "done"}
            mapping = {
                "已完成": "done",
                "完成": "done",
                "进行中": "in_progress",
                "待办": "todo",
                "未知": "unknown",
            }
            value = mapping.get(str(value), value)
            if value not in allowed:
                raise ValueError("状态只能是 unknown、todo、in_progress 或 done")
        #查负责人
        elif patch.field == "owner":
            value = (value or "待确认").strip() or "待确认"
        old = target.get(patch.field)
        if old == value:
            continue
        target[patch.field] = value
        #这些做的实际上是查action_items，里面的键值对是field-value形式
        #读取键值对，标准化value后修改value
        provided = set(target.get("user_provided_fields", []))
        provided.add(patch.field)
        target["user_provided_fields"] = sorted(provided)
        changes.append({"item_id": target["id"], "field": patch.field, "from": old, "to": value})
        last_id = target["id"]
    if not changes:
        raise AmbiguousInstruction("修改后的值与当前值相同")
    MinutesResult.model_validate(updated)
    return updated, changes, last_id

#规则式自然语言解析器，将用户的话转成FieldPatch，服务apply_patches
def apply_revision(
    current: dict[str, Any], instruction: str, previous_target: str | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    actions = current.get("action_items", [])
    index = _target_index(instruction, actions, previous_target)
    target = actions[index]
    patch: dict[str, Any] = {}

    owner = re.search(
        r"(?:负责人|负责)(?:改成|换成|改为|是|为|其实是)\s*([\u4e00-\u9fa5A-Za-z]{2,20})",
        instruction,
    )
    if not owner:
        owner = re.search(
            r"(?:改成|换成|改为|交给|改由)\s*([\u4e00-\u9fa5A-Za-z]{2,20})\s*(?:负责|来做|跟进)?",
            instruction,
        )
    if owner and owner.group(1) not in {"已完成", "进行中", "待办"}:
        name = owner.group(1).rstrip("，。的")
        for suffix in ("负责", "来做", "跟进"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        patch["owner"] = name

    if "截止" in instruction or "日期" in instruction:
        patch["due_date"] = parse_due_date(instruction)

    status_map = {
        "已完成": "done",
        "完成了": "done",
        "进行中": "in_progress",
        "待办": "todo",
        "状态未知": "unknown",
    }
    for phrase, status in status_map.items():
        if phrase in instruction:
            patch["status"] = status
            break
    #自然语言提取出owner,日期，状态，做成patch字典
    if not patch:
        raise AmbiguousInstruction("请说明要改负责人、截止日期还是状态")

    return apply_patches(
        current,
        [FieldPatch(item_id=target["id"], field=field, value=value) for field, value in patch.items()],
    )

#5.周报聚合：每个meeting里version最大的聚合起来
def draft_weekly(meetings: list[Meeting], notes: str | None = None) -> WeeklyResult:
    result = WeeklyResult(
        source_meeting_ids=[meeting.id for meeting in meetings], notes=notes
    )
    for meeting in meetings:
        versions = [v for v in meeting.versions if v.kind == "minutes"]
        if not versions:
            continue
        payload = max(versions, key=lambda v: v.version_no).payload
        for item in payload.get("action_items", []):
            label = f"{meeting.title}：{item['task']}"
            status = item.get("status", "unknown")
            if status == "done":
                result.completed.append(label)
            elif status == "in_progress":
                result.in_progress.append(label)
            elif status == "todo":
                result.next_week.append(label)
            else:
                result.risks_and_pending.append(label)
    return result
