from enum import StrEnum
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Environment(StrEnum):
    LOCAL = "local"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    environment: Environment = Environment.LOCAL
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr
    migration_database_url: SecretStr | None = None
    worker_database_url: SecretStr | None = None
    redis_url: SecretStr
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    dependency_timeout_seconds: float = Field(default=2, gt=0, le=30)
    tenant_auth_public_origin: str = ""
    secret_encryption_keys: SecretStr = SecretStr("")
    secret_encryption_active_key: str = ""
    otp_hmac_key: SecretStr = SecretStr("")
    stream_token_key: SecretStr = SecretStr("")
    tenant_llm_models: str = (
        "gemini-3.8-flash,gemini-3.5-flash-lite,"
        "google/gemini-3.8-flash,google/gemini-3.5-flash-lite"
    )
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_tls_mode: Literal["starttls", "tls", "plain"] = "starttls"
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_sender: str = ""
    # How long a platform administrator stays signed in. A workspace opened from the
    # platform console stays open for the same period.
    platform_session_minutes: int = Field(default=30, ge=5, le=720)
    # Traces and metrics are exported only when an OTLP/HTTP collector is configured.
    otel_exporter_otlp_endpoint: str = ""
    otel_exporter_otlp_headers: SecretStr = SecretStr("")
    # A clinic's own verified provider connection is used when it has one; otherwise
    # the platform account configured by the provider keys below.
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    telephony_public_base_url: str = ""
    stt_provider: Literal["deepgram", "azure"] = "deepgram"
    tts_provider: Literal["elevenlabs", "azure"] = "elevenlabs"

    # Deepgram STT
    deepgram_api_key: SecretStr = SecretStr("")
    # "multi" follows a caller who mixes languages; set one language code to fix it.
    deepgram_model: str = "nova-3"
    deepgram_language: str = "multi"

    # ElevenLabs TTS
    elevenlabs_api_key: SecretStr = SecretStr("")
    elevenlabs_model_id: str = "eleven_turbo_v2_5"
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"

    # Azure Speech Services (STT & TTS)
    azure_speech_key: SecretStr = SecretStr("")
    azure_speech_region: str = "eastus"
    azure_speech_language: str = "en-US"
    azure_speech_voice: str = "en-US-JennyNeural"
    azure_hindi_voice: str = "hi-IN-SwaraNeural"

    # Knowledge documents; the API and the worker share this storage.
    knowledge_storage_backend: Literal["local", "s3"] = "local"
    knowledge_local_path: str = ".local/knowledge-objects"
    knowledge_s3_bucket: str = ""
    knowledge_s3_region: str = ""
    knowledge_s3_endpoint: str = ""

    llm_provider: Literal["gemini", "openrouter"] = "gemini"
    gemini_api_key: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    # Empty uses the selected provider's default model.
    llm_model: str = ""
    safety_semantic_enabled: bool = True

    @model_validator(mode="after")
    def validate_knowledge(self) -> Self:
        if self.knowledge_storage_backend == "s3" and not self.knowledge_s3_bucket:
            raise ValueError("S3 knowledge storage requires KNOWLEDGE_S3_BUCKET")
        if self.deployed and self.knowledge_storage_backend != "s3":
            raise ValueError("Production knowledge storage requires S3")
        return self

    @model_validator(mode="after")
    def validate_speech(self) -> Self:
        if self.deepgram_language == "multi" and not self.deepgram_model.startswith("nova-3"):
            raise ValueError("DEEPGRAM_LANGUAGE=multi requires a nova-3 model")
        return self

    @model_validator(mode="after")
    def validate_telephony(self) -> Self:
        """Calls are answered once a public HTTPS address for webhooks is configured."""
        if self.deployed or self.telephony_public_base_url:
            url = urlsplit(self.telephony_public_base_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.path not in {"", "/"}
                or url.query
                or url.fragment
            ):
                raise ValueError("Configure TELEPHONY_PUBLIC_BASE_URL as a public HTTPS origin")
        self.telephony_public_base_url = self.telephony_public_base_url.rstrip("/")
        stream_key = self.stream_token_key.get_secret_value()
        if (self.deployed or stream_key) and len(stream_key) < 43:
            raise ValueError(
                "Configure an independent random STREAM_TOKEN_KEY of at least 43 characters"
            )
        if not self.llm_models:
            raise ValueError("Configure at least one TENANT_LLM_MODELS entry")
        return self

    @property
    def telephony_ready(self) -> bool:
        return bool(self.telephony_public_base_url)

    @property
    def platform_twilio(self) -> bool:
        """Whether the platform's own Twilio account is configured."""
        return bool(self.twilio_account_sid and self.twilio_auth_token.get_secret_value())

    @property
    def platform_llm_key(self) -> SecretStr:
        """The platform account key for the selected provider; empty when unset."""
        return self.openrouter_api_key if self.llm_provider == "openrouter" else self.gemini_api_key

    @property
    def llm_models(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.tenant_llm_models.split(",") if item.strip())

    @classmethod
    def from_environment(cls, env_file: str | None = ".env", **overrides: Any) -> Self:
        # BaseSettings loads required fields at runtime; Pyright sees the model's field signature.
        return cls(_env_file=env_file, **overrides)  # pyright: ignore[reportCallIssue]

    @model_validator(mode="after")
    def validate_accounts_and_mail(self) -> Self:
        """Sign-in, invitations and stored secrets are always part of the product.

        Their keys are mandatory once deployed. Locally a missing value only makes the
        feature that needs it unavailable, so the API can still start.
        """
        if self.deployed or self.secret_encryption_keys.get_secret_value():
            from app.security.encryption import SecretCipher

            SecretCipher(self.secret_encryption_keys, self.secret_encryption_active_key)
        code_key = self.otp_hmac_key.get_secret_value()
        if (self.deployed or code_key) and len(code_key) < 43:
            raise ValueError(
                "Configure an independent random OTP_HMAC_KEY of at least 43 characters"
            )
        if self.deployed or self.tenant_auth_public_origin:
            url = urlsplit(self.tenant_auth_public_origin)
            if (
                url.scheme not in ({"https"} if self.deployed else {"https", "http"})
                or not url.hostname
                or url.username
                or url.password
                or url.path not in {"", "/"}
                or url.query
                or url.fragment
            ):
                raise ValueError("Configure TENANT_AUTH_PUBLIC_ORIGIN as a trusted frontend origin")
        if self.mail_enabled:
            if (
                "@" not in self.smtp_sender
                or any(c in self.smtp_host + self.smtp_sender + self.smtp_username for c in "\r\n")
                or bool(self.smtp_username) != bool(self.smtp_password.get_secret_value())
            ):
                raise ValueError("Configure SMTP sender and matching username/password")
            if self.smtp_tls_mode == "plain" and self.deployed:
                raise ValueError("SMTP requires TLS outside local/development")
        return self

    @property
    def deployed(self) -> bool:
        return self.environment in {Environment.STAGING, Environment.PRODUCTION}

    @property
    def mail_enabled(self) -> bool:
        """Mail is delivered whenever an SMTP server is configured."""
        return bool(self.smtp_host.strip())

    @property
    def account_mail_ready(self) -> bool:
        """Invitations and resets need SMTP, the keyring, the code key and the link origin."""
        return bool(
            self.mail_enabled
            and self.secret_encryption_keys.get_secret_value()
            and len(self.otp_hmac_key.get_secret_value()) >= 43
            and self.tenant_auth_public_origin
        )

    @property
    def migration_url(self) -> str:
        return (self.migration_database_url or self.database_url).get_secret_value()

    @field_validator("migration_database_url", "worker_database_url")
    @classmethod
    def validate_migration_url(cls, value: SecretStr | None) -> SecretStr | None:
        return cls.validate_database_url(value) if value and value.get_secret_value() else None

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from None
        if url.drivername != "postgresql+asyncpg" or not url.host or not url.database:
            raise ValueError("DATABASE_URL must use postgresql+asyncpg with host and database")
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = urlsplit(value.get_secret_value())
            valid = url.scheme in {"redis", "rediss"} and bool(url.hostname)
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("REDIS_URL must be a valid redis:// or rediss:// URL")
        return value

    @field_validator("otel_exporter_otlp_endpoint")
    @classmethod
    def validate_otlp_endpoint(cls, value: str) -> str:
        if not value:
            return value  # Telemetry export is off.
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query:
            raise ValueError("OTLP endpoint must be an HTTP(S) base URL without credentials/query")
        return value.rstrip("/")
