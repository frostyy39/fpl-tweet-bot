import pytest

from fpl_bot.x_config import XPostingConfig
from fpl_bot.x_errors import XConfigurationError


def test_access_token_alone_does_not_enable_posting() -> None:
    config = XPostingConfig.from_environment({"X_USER_ACCESS_TOKEN": "unit-test-token-placeholder"})

    assert config.posting_enabled is False
    with pytest.raises(XConfigurationError, match="posting is disabled"):
        config.require_posting_guards()


def test_environment_configuration_parses_explicit_test_write_guards() -> None:
    config = XPostingConfig.from_environment(
        {
            "X_ENVIRONMENT": "test",
            "X_POSTING_ENABLED": "true",
            "X_EXPECTED_USER_ID": "123456789",
            "X_USER_ACCESS_TOKEN": "unit-test-token-placeholder",
        }
    )

    assert config.posting_enabled is True
    assert config.environment == "test"
    assert config.expected_user_id == "123456789"


def test_invalid_posting_enabled_value_is_rejected() -> None:
    with pytest.raises(XConfigurationError, match="either 'true' or 'false'"):
        XPostingConfig.from_environment({"X_POSTING_ENABLED": "yes"})


def test_production_mode_is_not_available() -> None:
    config = XPostingConfig(
        environment="production",
        posting_enabled=True,
        expected_user_id="123456789",
        user_access_token="unit-test-token-placeholder",
    )

    with pytest.raises(XConfigurationError, match="no production mode"):
        config.require_posting_guards()


def test_access_token_is_redacted_from_configuration_representation() -> None:
    config = XPostingConfig(user_access_token="unit-test-token-placeholder")

    assert "unit-test-token-placeholder" not in repr(config)
