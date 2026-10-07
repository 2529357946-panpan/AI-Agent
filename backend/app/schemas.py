#规定了前端传入、模型生成、后端返回的长什么样。每一个类都是一个数据包格式
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

#字段只能从固定值选
Status = Literal["unknown", "todo", "in_progress", "done"]

#BaseModel就是默认结构化数据格式，直接继承就行
#Field就是里面的校验规则

#A.会议纪要结果结构
#一条有来源的文本
class ReferencedText(BaseModel):
    text: str #模型的结论
    source_refs: list[str] = Field(min_length=1) 
    #来源于会议原材料的文本块，例：["c1","c2"]最少一个元素

#一条待办任务
class ActionItem(BaseModel):
    id: str
    task: str
    owner: str = "待确认"
    due_date: date | None = None
    status: Status = "unknown"
    source_refs: list[str] = Field(min_length=1)
    user_provided_fields: list[str] = Field(default_factory=list) #默认给我一个新的空列表
    #最后一条：哪些字段是用户自己补充的

#会议纪要最终结果
class MinutesResult(BaseModel):
    summary: str
    #下面两个都是嵌套
    decisions: list[ReferencedText] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)

#B.周报结果结构
class WeeklyResult(BaseModel):
    completed: list[str] = Field(default_factory=list)
    in_progress: list[str] = Field(default_factory=list)
    risks_and_pending: list[str] = Field(default_factory=list)
    next_week: list[str] = Field(default_factory=list)
    source_meeting_ids: list[str]
    notes: str | None = None

#C.前端->后端的请求结构
#前端创建会议会发一个POST请求，请求体里要求的字段。
#FastAPI接受后，会创建一个MeetingCreate对象。
class MeetingCreate(BaseModel):
    text: str
    title: str | None = None
    meeting_date: date | None = None

    @field_validator("text") #字段级validation，专门检查text字段是否为空
    @classmethod
    def text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("会议材料不能为空")
        return value

#修改待办完成状态的请求包，前端的待办事项的复选框
class ActionToggleRequest(BaseModel):
    done: bool

#minutes是会议纪要的意思，目前只有这一种生成请求，后面可能扩展
class GenerateRequest(BaseModel):
    kind: Literal["minutes"] = "minutes"

#用户在聊天框下的指令在操作哪个会议
class MessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2_000)
    meeting_id: str | None = None


#D.聊天理解/修改的结构

#修改某个待办事项，这是一个安全边界设计
#模型负责理解意图，代码执行修改
class FieldPatch(BaseModel):
    #用户说的话可能关联某一项待办事项的id或者内部序号
    item_id: str | None = None
    item_index: int | None = Field(default=None, ge=1)#ge：大于等于
    #允许更改的字段：负责人、DDL、状态、任务内容
    field: Literal["owner", "due_date", "status", "task"]
    value: str | None = None

#模型对用户意图转译
class ChatInterpretation(BaseModel):
    #理解意图：用户要修改/问询/意图不清、需要用户说明白
    intent: Literal["revision", "question", "clarify"]
    answer: str
    #在上面的修改补丁中选择一个
    patches: list[FieldPatch] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)

#生成周报的范围
class WeeklyRequest(BaseModel):
    meeting_ids: list[str] = Field(min_length=1)
    period_start: date
    period_end: date
    notes: str | None = None

    @model_validator(mode="after") #创建完对象后整体检查，是否业务合规
    def validate_period(self):
        if self.period_end < self.period_start:
            raise ValueError("结束日期不能早于开始日期")
        return self
