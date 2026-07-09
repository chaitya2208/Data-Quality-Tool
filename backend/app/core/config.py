from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    PROJECT_NAME: str = "Data Quality Platform"
    API_V1_STR: str = "/api/v1"

    # Snowflake
    SNOWFLAKE_ACCOUNT: str
    SNOWFLAKE_USER: str
    SNOWFLAKE_PASSWORD: Optional[str] = None
    SNOWFLAKE_WAREHOUSE: str
    SNOWFLAKE_DATABASE: str = "PLAYGROUND_DB"
    SNOWFLAKE_SCHEMA: Optional[str] = None
    SNOWFLAKE_ROLE: Optional[str] = None
    SNOWFLAKE_AUTH_METHOD: str = "externalbrowser"  # externalbrowser or password

    # App storage — all app-owned tables (assets/scans/findings/rules/etc.)
    # live in this one schema inside SNOWFLAKE_DATABASE, separate from the
    # schemas holding the source data being scanned.
    SNOWFLAKE_APP_SCHEMA: str = "DQ_APP"

    # Security
    SECRET_KEY: str

    # Environment
    ENVIRONMENT: str = "development"

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
