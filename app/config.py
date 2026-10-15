from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    verify_token: str
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    app_secret: str = ""
    anthropic_api_key: str = ""

    log_level: str = "INFO"


settings = Settings()
