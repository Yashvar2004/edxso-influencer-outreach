"""Environment-backed application configuration."""

from __future__ import annotations

import os
import ipaddress
import math
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from dotenv import load_dotenv

from outreach.database import normalize_business_email


class ConfigurationError(ValueError):
    """Raised when an application setting is malformed."""


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false).")


def _resolve_database_path(value: str) -> Path:
    raw_path = Path(value.strip()).expanduser()
    if not str(raw_path):
        raise ConfigurationError("DATABASE_PATH must not be empty.")
    return raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path


def _optional_value(source: dict[str, str], name: str) -> str | None:
    value = source.get(name, "").strip()
    return value or None


def _validate_smtp_host(value: str) -> str:
    """Validate a configured hostname or IP literal without resolving it."""
    host = value.strip()
    if not host or any(char.isspace() or ord(char) < 33 for char in host):
        raise ConfigurationError("SMTP_HOST must be a hostname or IP address without whitespace.")
    if any(marker in host for marker in ("://", "/", "@", "?", "#")):
        raise ConfigurationError("SMTP_HOST must not contain a URL, credentials, path, or port.")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    try:
        ascii_host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ConfigurationError("SMTP_HOST is not a valid hostname or IP address.") from exc
    labels = ascii_host.split(".")
    label_pattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    if len(ascii_host) > 253 or not all(label_pattern.fullmatch(label) for label in labels):
        raise ConfigurationError("SMTP_HOST is not a valid hostname or IP address.")
    if len(labels) > 1 and (len(labels[-1]) < 2 or not re.fullmatch(r"[a-z]{2,}|xn--[a-z0-9-]+", labels[-1])):
        raise ConfigurationError("SMTP_HOST has an invalid top-level domain.")
    return ascii_host


def _parse_smtp_port(value: str | None) -> int:
    if value is None:
        return 587
    try:
        port = int(value)
    except ValueError as exc:
        raise ConfigurationError("SMTP_PORT must be an integer between 1 and 65535.") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("SMTP_PORT must be an integer between 1 and 65535.")
    return port


def _parse_smtp_timeout(value: str | None) -> float:
    if value is None:
        return 20.0
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ConfigurationError("SMTP_TIMEOUT_SECONDS must be between 1 and 120.") from exc
    if not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise ConfigurationError("SMTP_TIMEOUT_SECONDS must be between 1 and 120.")
    return timeout


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime settings with safe local defaults."""

    database_path: Path
    demo_mode: bool = True
    llm_provider: str = "groq"
    llm_model: str = ""
    llm_api_key: str | None = dataclass_field(default=None, repr=False)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_username: str | None = dataclass_field(default=None, repr=False)
    smtp_password: str | None = dataclass_field(default=None, repr=False)
    smtp_from_email: str | None = None
    smtp_timeout_seconds: float = 20.0

    @property
    def smtp_configured(self) -> bool:
        """True when all required authenticated SMTP settings are available."""
        return all((self.smtp_host, self.smtp_username, self.smtp_password, self.smtp_from_email))

    @classmethod
    def from_env(
        cls,
        *,
        env_file: str | Path | None = None,
        environ: dict[str, str] | None = None,
    ) -> "Settings":
        """Load settings from environment variables and optionally a dotenv file.

        Existing process environment values take precedence over values in the
        dotenv file. Pass ``environ`` to make configuration deterministic in tests.
        """
        if environ is None:
            if env_file is not None:
                load_dotenv(dotenv_path=Path(env_file), override=False)
            source = os.environ
        else:
            source = environ
            if env_file is not None:
                # Explicit mappings intentionally remain isolated from process env.
                from dotenv import dotenv_values

                file_values = dotenv_values(Path(env_file))
                source = {**{k: v for k, v in file_values.items() if v is not None}, **environ}

        database_value = source.get("DATABASE_PATH", "data/outreach.sqlite3")
        if not database_value.strip():
            raise ConfigurationError("DATABASE_PATH must not be empty.")

        demo_value = source.get("DEMO_MODE", "true")
        llm_provider = source.get("LLM_PROVIDER", "groq").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", llm_provider):
            raise ConfigurationError("LLM_PROVIDER must be a provider identifier.")
        provider_key_name = f"{llm_provider.upper().replace('-', '_')}_API_KEY"
        smtp_host_value = _optional_value(source, "SMTP_HOST")
        smtp_username = _optional_value(source, "SMTP_USERNAME")
        smtp_password = _optional_value(source, "SMTP_PASSWORD")
        smtp_from_raw = _optional_value(source, "SMTP_FROM_EMAIL")
        smtp_security = source.get("SMTP_SECURITY", "starttls").strip().lower()
        if smtp_security not in {"starttls", "ssl"}:
            raise ConfigurationError("SMTP_SECURITY must be 'starttls' or 'ssl'.")
        if bool(smtp_username) != bool(smtp_password):
            raise ConfigurationError("SMTP_USERNAME and SMTP_PASSWORD must be provided together.")
        if any((smtp_host_value, smtp_username, smtp_password, smtp_from_raw)) and not all(
            (smtp_host_value, smtp_username, smtp_password, smtp_from_raw)
        ):
            raise ConfigurationError(
                "SMTP configuration requires SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM_EMAIL together."
            )
        smtp_host = _validate_smtp_host(smtp_host_value) if smtp_host_value else None
        if smtp_from_raw:
            try:
                smtp_from_email = normalize_business_email(smtp_from_raw)
            except ValueError as exc:
                raise ConfigurationError("SMTP_FROM_EMAIL must be a valid email address.") from exc
        else:
            smtp_from_email = None
        return cls(
            database_path=_resolve_database_path(database_value),
            demo_mode=_parse_bool(demo_value, name="DEMO_MODE"),
            llm_provider=llm_provider,
            llm_model=source.get("LLM_MODEL", "").strip(),
            llm_api_key=source.get(provider_key_name),
            smtp_host=smtp_host,
            smtp_port=_parse_smtp_port(_optional_value(source, "SMTP_PORT")),
            smtp_security=smtp_security,
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            smtp_from_email=smtp_from_email,
            smtp_timeout_seconds=_parse_smtp_timeout(_optional_value(source, "SMTP_TIMEOUT_SECONDS")),
        )
