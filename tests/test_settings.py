import pytest

from vigil.settings import MissingSetting, PostgresSettings, required, required_int


def test_a_missing_variable_names_itself_in_the_error(monkeypatch):
    monkeypatch.delenv("VIGIL_TEST_ABSENT", raising=False)
    with pytest.raises(MissingSetting, match="VIGIL_TEST_ABSENT"):
        required("VIGIL_TEST_ABSENT")


def test_an_empty_variable_counts_as_missing(monkeypatch):
    # An empty value in .env is a half-finished copy of .env.example, not a choice.
    monkeypatch.setenv("VIGIL_TEST_EMPTY", "   ")
    with pytest.raises(MissingSetting):
        required("VIGIL_TEST_EMPTY")


def test_a_non_numeric_int_setting_fails_with_the_offending_value(monkeypatch):
    monkeypatch.setenv("VIGIL_TEST_PORT", "not-a-port")
    with pytest.raises(MissingSetting, match="not-a-port"):
        required_int("VIGIL_TEST_PORT")


def test_the_password_never_appears_in_a_repr():
    # Settings objects end up in tracebacks and log lines; the password must not.
    s = PostgresSettings(
        host="h", port=5432, user="u", password="hunter2-should-not-leak", database="d"
    )
    assert "hunter2-should-not-leak" not in repr(s)
    assert "<redacted>" in repr(s)


def test_the_dsn_still_carries_the_password_for_connecting():
    s = PostgresSettings(host="h", port=5432, user="u", password="pw", database="d")
    assert s.dsn == "postgresql://u:pw@h:5432/d"
