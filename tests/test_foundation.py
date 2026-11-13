from pathlib import Path

import pytest

from outreach.config import ConfigurationError, Settings
from outreach.database import (
    DatabaseError,
    add_influencer,
    count_influencers,
    get_influencer,
    initialize_database,
)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "nested" / "test.sqlite3"
    initialize_database(path)
    return path


def test_database_persists_influencer_across_connections(database_path: Path) -> None:
    record_id = add_influencer(
        database_path,
        name="Actual Creator",
        platform="youtube",
        profile_url="https://www.youtube.com/@actualcreator",
        follower_count=12000,
        content_themes=["programming"],
    )

    saved = get_influencer(database_path, record_id)

    assert saved is not None
    assert saved["name"] == "Actual Creator"
    assert saved["platform"] == "YOUTUBE"
    assert saved["follower_count"] == 12000
    assert saved["business_email"] == "Not Found"
    assert count_influencers(database_path) == 1


def test_duplicate_platform_profile_is_rejected(database_path: Path) -> None:
    values = {
        "name": "Creator",
        "platform": "youtube",
        "profile_url": "https://youtube.com/@creator",
    }
    add_influencer(database_path, **values)

    with pytest.raises(DatabaseError, match="already stored"):
        add_influencer(database_path, **values)


def test_different_platform_profile_can_share_url(database_path: Path) -> None:
    profile_url = "https://example.com/creator"
    add_influencer(database_path, name="Creator A", platform="SITE_A", profile_url=profile_url)
    add_influencer(database_path, name="Creator B", platform="SITE_B", profile_url=profile_url)
    assert count_influencers(database_path) == 2


def test_profile_url_and_metric_validation(database_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute HTTP"):
        add_influencer(database_path, name="Creator", platform="X", profile_url="not-a-url")
    with pytest.raises(ValueError, match="follower_count"):
        add_influencer(database_path, name="Creator", platform="X", profile_url="https://x.com/a", follower_count=-1)
    with pytest.raises(ValueError, match="engagement_rate"):
        add_influencer(database_path, name="Creator", platform="X", profile_url="https://x.com/b", engagement_rate=101)


def test_configuration_defaults_and_overrides(tmp_path: Path) -> None:
    settings = Settings.from_env(environ={})
    assert settings.database_path.name == "outreach.sqlite3"
    assert settings.demo_mode is True
    assert settings.smtp_port == 587
    assert settings.smtp_security == "starttls"
    assert settings.smtp_timeout_seconds == 20.0
    assert settings.smtp_configured is False

    override = Settings.from_env(
        environ={"DATABASE_PATH": str(tmp_path / "custom.db"), "DEMO_MODE": "no"}
    )
    assert override.database_path == tmp_path / "custom.db"
    assert override.demo_mode is False


def test_configuration_rejects_malformed_values() -> None:
    with pytest.raises(ConfigurationError, match="must be a boolean"):
        Settings.from_env(environ={"DEMO_MODE": "sometimes"})
    with pytest.raises(ConfigurationError, match="must not be empty"):
        Settings.from_env(environ={"DATABASE_PATH": "  "})


def test_dotenv_file_is_loaded_with_environment_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_PATH=from-file.db\nDEMO_MODE=false\n", encoding="utf-8")
    settings = Settings.from_env(env_file=env_file, environ={"DEMO_MODE": "true"})
    assert settings.database_path.name == "from-file.db"
    assert settings.demo_mode is True


def test_complete_smtp_configuration_is_validated_and_secrets_are_redacted() -> None:
    settings = Settings.from_env(environ={
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "465",
        "SMTP_SECURITY": "ssl",
        "SMTP_USERNAME": "operator@example.com",
        "SMTP_PASSWORD": "smtp-test-secret",
        "SMTP_FROM_EMAIL": "outreach@example.com",
        "SMTP_TIMEOUT_SECONDS": "12.5",
    })
    assert settings.smtp_configured is True
    assert settings.smtp_host == "smtp.example.com"
    assert settings.smtp_port == 465
    assert settings.smtp_security == "ssl"
    assert settings.smtp_from_email == "outreach@example.com"
    assert settings.smtp_timeout_seconds == 12.5
    assert "smtp-test-secret" not in repr(settings)
    assert "operator@example.com" not in repr(settings)


@pytest.mark.parametrize("environment", [
    {"SMTP_HOST": "https://smtp.example.com", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p", "SMTP_FROM_EMAIL": "o@example.com"},
    {"SMTP_HOST": "smtp.example.com"},
    {"SMTP_USERNAME": "u"},
    {"SMTP_PASSWORD": "p"},
    {"SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p", "SMTP_FROM_EMAIL": "invalid"},
])
def test_smtp_configuration_rejects_invalid_or_incomplete_values(environment: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        Settings.from_env(environ=environment)


@pytest.mark.parametrize("key,value", [
    ("SMTP_PORT", "0"), ("SMTP_PORT", "65536"), ("SMTP_PORT", "abc"),
    ("SMTP_SECURITY", "plain"), ("SMTP_TIMEOUT_SECONDS", "0"),
    ("SMTP_TIMEOUT_SECONDS", "NaN"), ("SMTP_TIMEOUT_SECONDS", "121"),
])
def test_smtp_configuration_rejects_invalid_transport_values(key: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        Settings.from_env(environ={key: value})
