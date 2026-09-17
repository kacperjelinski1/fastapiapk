from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    db_host: str
    db_port: int = 5432
    db_name: str
    db_user: str
    db_password: str

    media_root: str = str(PROJECT_ROOT / "storage")
    owner_username: str = "tomasz"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    auth_required: bool = False
    api_secret: str = "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET"
    token_hours: int = 168

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
