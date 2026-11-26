"""Runtime configuration.

Environment variables are the source of truth so the same image runs in
docker-compose and in tests. API keys stay here and are never written to the
database or to model-call logs.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://zhenchuan:zhenchuan@localhost:5432/zhenchuan"
    redis_url: str = "redis://localhost:6379/0"
    inline_jobs: bool = False

    storage_backend: str = "s3"
    local_storage_dir: str = "./.local-data"
    s3_endpoint: str = "http://localhost:9000"
    s3_public_endpoint: str = ""
    s3_bucket: str = "zhenchuan"
    s3_access_key: str = "minio"
    s3_secret_key: str = "minio12345"
    s3_region: str = "us-east-1"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_image_model: str = "dall-e-3"

    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    seedream_model: str = "doubao-seedream-4-5-251128"
    seedance_model: str = "doubao-seedance-2-0-260128"

    kling_api_key: str = ""
    kling_access_key: str = ""
    kling_secret_key: str = ""
    kling_base_url: str = "https://api-singapore.klingai.com"
    kling_video_model: str = "kling-v2-6"

    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com"
    minimax_model: str = "speech-2.6-hd"
    minimax_voice_id: str = "male-qn-qingse"

    elevenlabs_api_key: str = ""
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"

    video_http_api_key: str = ""

    sample_novel_path: str = ""
    cors_origins: str = "*"
    admin_api_key: str = "zc-dev-admin"
    dev_user_api_key: str = "zc-dev-user"
    dev_user_credits: float = 100
    credit_markup: float = 1.2

    def sample_path(self) -> Path:
        if self.sample_novel_path:
            return Path(self.sample_novel_path)
        return Path(__file__).resolve().parents[2] / "samples" / "short_novel.txt"


@lru_cache
def get_settings() -> Settings:
    return Settings()
