"""
Central bridge config (12-factor: everything via environment / .env).

Secrets (TMS_TOKEN, FMCSA_API_KEY, BRIDGE_API_KEY) live only here and are never logged.
`.env` is gitignored; inject via environment variables in production.
"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Legacy TMS (TCP socket)
    tms_host: str = Field(..., description="Host del TMS legacy.")
    tms_port: int = Field(..., description="Puerto del TMS legacy.")
    tms_token: str = Field(..., description="Bearer token del TMS (secreto, nunca se loguea).")
    tms_timeout: float = Field(8.0, description="Timeout por intento de socket (s).")
    tms_retries: int = Field(3, description="Reintentos en lecturas ante faults inyectados.")

    # FMCSA (public REST)
    fmcsa_api_key: str = Field("", description="webKey de la FMCSA QCMobile API.")
    fmcsa_base_url: str = Field(
        "https://mobile.fmcsa.dot.gov/qc/services",
        description="Base de la FMCSA QCMobile API.",
    )
    fmcsa_timeout: float = Field(10.0, description="Timeout de la llamada a FMCSA (s).")
    fmcsa_mode: str = Field(
        "live",
        description="live = QCMobile real (requiere IP US) · mock = verdict determinista "
                    "para dev local (QCMobile geo-bloquea fuera de EE.UU.).",
    )

    # Bridge's own security: every operational route requires X-API-Key == bridge_api_key.
    bridge_api_key: str = Field(
        "", description="API key que deben enviar los webhooks de la plataforma."
    )

    # Negotiation engine (dials; the real ceiling comes from the TMS)
    neg_alpha: float = Field(0.08, description="Amplitud de concesión sobre el posted.")
    neg_max_rounds: int = Field(3, description="Máx. de contraofertas (brief: 3).")
    neg_step: int = Field(25, description="Redondeo de oferta ($) para que suene natural.")
    neg_ttl_seconds: float = Field(
        1800.0, description="TTL del estado de negociación (s): evita que un call_id reusado herede "
                            "estado rancio (anti-ratchet con agreed_rate viejo)."
    )

    # OTP (identity check via the platform's native SMS)
    otp_on_file_phone: str = Field(
        "", description="Número 'registrado' del carrier (E.164, p.ej. +1...) al que va el OTP. "
                        "Para la demo = el móvil de prueba; lo usa el nodo Send SMS."
    )
    otp_fixed_code: str = Field(
        "482915", description="Código de TEST aceptado siempre (un caller SIMULADO no puede recibir "
                              "un SMS real → así el suite adversarial pasa el gate). En prod se quita."
    )
    otp_ttl_seconds: float = Field(600.0, description="Validez del código (s).")
    otp_max_attempts: int = Field(3, description="Intentos antes de invalidar.")

    # Meta
    app_version: str = Field("1.0.0", description="Versión del Bridge.")
    log_level: str = Field("INFO", description="Nivel de logging.")


@lru_cache
def get_settings() -> Settings:
    """Cached settings — read once per process."""
    return Settings()
