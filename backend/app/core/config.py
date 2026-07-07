from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    PROJECT_NAME: str = "Data Quality Platform"
    API_V1_STR: str = "/api/v1"

    # Database
    DATABASE_URL: str

    # Snowflake
    SNOWFLAKE_ACCOUNT: str
    SNOWFLAKE_USER: str
    SNOWFLAKE_PASSWORD: Optional[str] = None
    SNOWFLAKE_WAREHOUSE: str
    SNOWFLAKE_DATABASE: Optional[str] = None
    SNOWFLAKE_SCHEMA: Optional[str] = None
    SNOWFLAKE_ROLE: Optional[str] = None
    SNOWFLAKE_AUTH_METHOD: str = "externalbrowser"  # externalbrowser or password

    # Security
    SECRET_KEY: str

    # Environment
    ENVIRONMENT: str = "development"

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
