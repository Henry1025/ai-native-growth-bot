from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Moonshot / Kimi API
    moonshot_api_key: str = Field(..., env="MOONSHOT_API_KEY")
    moonshot_base_url: str = "https://api.moonshot.cn/v1"

    # Model names
    screening_model: str = "kimi-k2.5"  # Kimi K2.5，判断力更强，确保不错过高价值文章
    analysis_model: str = "kimi-k2.5"               # Kimi K2.5，最强，适合深度分析

    # Feishu / Lark
    feishu_app_id: str = Field(..., env="FEISHU_APP_ID")
    feishu_app_secret: str = Field(..., env="FEISHU_APP_SECRET")
    feishu_doc_token: str | None = Field(default=None, env="FEISHU_DOC_TOKEN")  # 固定日报文档的 docx token
    feishu_sheet_token: str | None = Field(default=None, env="FEISHU_SHEET_TOKEN")  # 固定初筛表格的 sheets token

    # Pipeline settings
    article_max_chars: int = 3000        # 单篇内容最大字符数
    recent_hours: int = 24               # 只取最近 N 小时的文章
    screening_batch_size: int = 20       # 每次送给 LLM 初筛的文章数

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
