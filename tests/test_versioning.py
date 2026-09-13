import pytest

from operations.versioning import SCHEMA_VERSION, UnsupportedSchemaVersion, check_schema_version


def test_matching_major_is_accepted() -> None:
    check_schema_version(SCHEMA_VERSION)


def test_unknown_minor_is_accepted() -> None:
    check_schema_version("0.99")


def test_unknown_major_is_rejected() -> None:
    with pytest.raises(UnsupportedSchemaVersion):
        check_schema_version("1.0")


def test_error_message_names_received_and_supported_major() -> None:
    with pytest.raises(UnsupportedSchemaVersion) as excinfo:
        check_schema_version("2.0", supported="0.1")

    message = str(excinfo.value)
    assert "2.0" in message
    assert "0" in message
