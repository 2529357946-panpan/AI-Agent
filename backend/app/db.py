#定义数据库表，表之间的关系；
#创建数据库连接engine，创建数据库会话session，提供init,health check,dependency
import json #dict和json互相转换
import uuid #全局唯一ID
from datetime import date, datetime, timezone
from typing import Any #dict[str,Any]，key是str,value可以是任何类型

#ORM：对象关系映射，表、行、列对应类、对象、属性，PK是id，FK是对象之间关系
#也就是python跟关系型数据库之间的翻译
#SQLalchemy是负责表操作、事务、连接池、对象->SQL，真正存数据的还是SQLite
#create_engine创建数据库连接引擎，event监听数据库事件，text写原始SQL

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text,create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
#第一个是所有ORM类的父类，第二个是告诉sqlalchemy这个属性要映射到数据库
#mapped_column定义数据库列，relationship定义ORM对象间关系，sessionmaker是创建数据库session工厂

from .config import settings


def new_id() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc) #数据库内统一用utc时间

#所有数据库模型的共同父类，继承了Base，SQLalchemy就知道这个类是ORM。
#Base.metadata可以知道当前项目注册了哪些表
class Base(DeclarativeBase):
    pass


class Meeting(Base):
    __tablename__ = "meetings"
    #Mapped[str]：SQLalchemy的定义方式，里面是python的类型，外面是代表ORM
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(240))
    meeting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    source_type: Mapped[str] = mapped_column(String(20), default="text")
    raw_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="ready")
    chunks: Mapped[list["SourceChunk"]] = relationship(
        cascade="all, delete-orphan", order_by="SourceChunk.ordinal"
    ) #relationship代表一对多，一个meeting对多个chunk，cascade就是删meeting也要删对应的chunk
    sessions: Mapped[list["Session"]] = relationship(cascade="all, delete-orphan")
    versions: Mapped[list["ResultVersion"]] = relationship(
        cascade="all, delete-orphan", order_by="ResultVersion.created_at"
    )


class SourceChunk(Base): #会议原文切片
    __tablename__ = "source_chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)#外键，数据库给它建立索引
    ordinal: Mapped[int] = mapped_column(Integer) #原文第几段
    text: Mapped[str] = mapped_column(Text)
    start_char: Mapped[int] = mapped_column(Integer) #每个chunk对应原文的字符范围
    end_char: Mapped[int] = mapped_column(Integer)


class Session(Base): #这是业务会话，用户和agent聊天会话
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    meeting_id: Mapped[str | None] = mapped_column(
        ForeignKey("meetings.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    messages: Mapped[list["Message"]] = relationship(
        cascade="all, delete-orphan", order_by="Message.created_at"
    )#一个session对应多个message


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    result_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("result_versions.id"), nullable=True
    )#某条消息可能关联到某一个会议纪要版本，可以追溯哪一轮聊天导致了哪个版本


class ResultVersion(Base):
    __tablename__ = "result_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    meeting_id: Mapped[str | None] = mapped_column(
        ForeignKey("meetings.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(20)) #minutes,weekly
    version_no: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text)
    #payload是会议纪要的JSON版本，里面有summary,decisions,action_items等key
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("result_versions.id"), nullable=True
    )#基于哪一个上一个版本
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)
    #json格式是数据库存储格式，dict是方便python使用的格式


class WeeklySelection(Base): #基于哪些列选择生成周报
    __tablename__ = "weekly_selections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    result_version_id: Mapped[str] = mapped_column(
        ForeignKey("result_versions.id"), unique=True
    ) #一个resultVersion最多对应一个weeklySession
    meeting_ids_json: Mapped[str] = mapped_column(Text)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)


is_sqlite = settings.database_url.startswith("sqlite") #检查当前是不是SQLite
connect_args = {"check_same_thread": False, "timeout": 15} if is_sqlite else {}
#存储一些连接配置；允许FastAPI在处理请求时跨线程工作
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
#连接管理总入口，后面检查连接是否还活着

if is_sqlite:
    #每当engine新建一个SQLite连接时，自动执行以下配置（事件监听）
    @event.listens_for(engine, "connect") 
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        #dbapi_connection：实际上python SQLite驱动建立的数据库连接对象
        cursor = dbapi_connection.cursor()
        #已经创立连接，还需要一个cursor（实际操作入口）来实际发送SQL命令、获取结果
        #PRAGMA设置：SQLite运行参数设置，很多是针对数据库连接生效的
        cursor.execute("PRAGMA foreign_keys=ON") #开启FK约束，防止脏关联
        cursor.execute("PRAGMA journal_mode=WAL") #写入操作先写到一个日志文件里，不立刻修改主数据库文件
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close() #cuesoe生命周期资源管理
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
#通过sessionmaker创建的数据库session都用这个engine，commit后不直接过期，后面还能查

def init_db() -> None:
    Base.metadata.create_all(engine) 
    #sqlalchemy根据所有继承了Base的ORM类，把不存在的数据表创建出来
    #后端启动的时候就会调用init_db


def check_db() -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def get_db():
    db = SessionLocal() #一次具体数据库对话
    try:
        yield db
    finally:
        db.close()
