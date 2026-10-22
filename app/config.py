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

    # Traspaso a una persona: correo que recibe la ficha y servidor SMTP para enviarlo.
    handoff_email_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # Minutos (en horario de atención) sin respuesta del equipo antes de enviar una alerta.
    handoff_alert_minutes: int = 60
    # Horas desde el último mensaje del equipo tras las cuales el bot vuelve a responder solo.
    handoff_idle_hours: int = 12

    log_level: str = "INFO"


settings = Settings()
