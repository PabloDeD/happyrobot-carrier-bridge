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
    tms_host: str = Field(..., description="Legacy TMS host.")
    tms_port: int = Field(..., description="Legacy TMS port.")
    tms_token: str = Field(..., description="TMS bearer token (secret, never logged).")
    tms_timeout: float = Field(8.0, description="Timeout per socket attempt (s).")
    tms_retries: int = Field(3, description="Retries on reads when faults are injected.")

    # FMCSA (public REST)
    fmcsa_api_key: str = Field("", description="FMCSA QCMobile API webKey.")
    fmcsa_base_url: str = Field(
        "https://mobile.fmcsa.dot.gov/qc/services",
        description="FMCSA QCMobile API base URL.",
    )
    fmcsa_timeout: float = Field(10.0, description="FMCSA call timeout (s).")
    fmcsa_mode: str = Field(
        "live",
        description="live = real QCMobile (requires a US IP) · mock = deterministic verdict "
                    "for local dev (QCMobile geo-blocks outside the US).",
    )

    # Bridge's own security: every operational route requires X-API-Key == bridge_api_key.
    bridge_api_key: str = Field(
        "", description="API key the platform's webhooks must send."
    )

    # Negotiation engine (dials; the real ceiling comes from the TMS)
    neg_alpha: float = Field(0.08, description="Concession amplitude relative to the posted rate.")
    neg_max_rounds: int = Field(3, description="Max counteroffers (brief: 3).")
    neg_step: int = Field(25, description="Offer rounding ($) so it sounds natural.")
    neg_ttl_seconds: float = Field(
        1800.0, description="Negotiation-state TTL (s): prevents a reused call_id from inheriting "
                            "stale state (anti-ratchet with an old agreed_rate)."
    )

    # OTP (identity check via the platform's native SMS)
    otp_on_file_phone: str = Field(
        "", description="Carrier's 'on-file' number (E.164, e.g. +1...) the OTP is sent to. "
                        "For the demo = the test mobile; used by the Send SMS node."
    )
    otp_fixed_code: str = Field(
        "482915", description="TEST code always accepted (a SIMULATED caller can't receive a real "
                              "SMS → this lets the adversarial suite pass the gate). Removed in prod."
    )
    otp_ttl_seconds: float = Field(600.0, description="Code validity (s).")
    otp_max_attempts: int = Field(3, description="Attempts before invalidation.")

    # Meta
    app_version: str = Field("1.0.0", description="Bridge version.")
    log_level: str = Field("INFO", description="Logging level.")


@lru_cache
def get_settings() -> Settings:
    """Cached settings — read once per process."""
    return Settings()
