#整个项目的配置中心，后面的直接import settings就可以

from pathlib import Path #python官方库，引入专门处理路径的path

from pydantic_settings import BaseSettings, SettingsConfigDict
#应用配置管理，这个库解决环境变量读取、转换、生成配置对象
#BaseSettings就是把环境变量的字符串，转化成配置需要的布尔、浮点等类型

ROOT_DIR = Path(__file__).resolve().parents[2]
#这个下划线file就是指当前文件自己的路径，规避不同的操作系统问题
#生成Path对象，resolve转换成绝对路径，parents就是一级级往上寻找的路径
#到了第二级就是根目录。

#继承pydantic的配置读取能力
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env", 
        env_file_encoding="utf-8", 
        extra="ignore" #如果有Settings不认识的配置就忽略它
    )

    #这里是默认配置，如果环境变量没写新的就用这个
    database_url: str = f"sqlite:///{(ROOT_DIR / 'office_agent.db').as_posix()}"
    deepseek_api_key: str = ""
    model_name: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    model_timeout_seconds: float = 45
    use_fake_model: bool = True
    max_file_bytes: int = 10 * 1024 * 1024
    max_text_chars: int = 50_000

    #cross-origin resource sharing,跨源资源共享，允许这两个origin向后端请求信息
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    #日志级别：记录严重于info的信息
    log_level: str = "INFO"

    @property #这个装饰器让这个方法可以通过settings.成员变量访问，而不需要加括号
    def cors_origin_list(self) -> list[str]:   #把origins转化为干净的list
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
