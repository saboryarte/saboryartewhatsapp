from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    verify_token: str
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    app_secret: str = ""
    anthropic_api_key: str = ""

    # Archivo SQLite con el estado de las conversaciones y los ids de mensajes ya procesados.
    state_db_path: str = "data/conversaciones.db"

    log_level: str = "INFO"


settings = Settings()
