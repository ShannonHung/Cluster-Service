"""
tests/unit/test_dry_run_mode.py

T8: the dry-run scaffolding — the setting, the production start-up refusal, and
the response marker.

Deliberately behaviour-free. Enabling DRY_RUN_MODE today changes nothing except
the marker and a warning log: the deploy-service client and the Kubernetes
client are still real, and the stubs arrive in T9 / T10. Keeping the scaffolding
in its own ticket means the guard and the marker are in place and tested
*before* anything starts depending on them.

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings, get_settings
from app.domain.models import ApiResponse


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """get_settings is lru_cache'd, so a test that changes the environment must
    clear it on both sides or it leaks into unrelated tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── the setting ───────────────────────────────────────────────────────────────


def test_dry_run_is_off_by_default():
    """The safe default. A deployment that says nothing must not be in
    dry-run — silently no-op'ing production is the failure this guards."""
    assert get_settings().DRY_RUN_MODE is False


def test_dry_run_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()
    assert get_settings().DRY_RUN_MODE is True


def test_dry_run_accepts_falsey_spellings(monkeypatch):
    for value in ("false", "False", "0"):
        monkeypatch.setenv("DRY_RUN_MODE", value)
        get_settings.cache_clear()
        assert get_settings().DRY_RUN_MODE is False, value


# ── production start-up refusal ───────────────────────────────────────────────


def test_prod_plus_dry_run_is_refused():
    """A hard failure, not a warning.

    In production a dry-run instance would answer 200 to every drain and cordon
    while doing nothing — worse than an outage, because nothing alerts. The
    process must not reach a serving state, so this raises before the FastAPI
    instance is built rather than logging and carrying on.
    """
    from app.main import _guard_dry_run

    settings = Settings(APP_ENV="prod", DRY_RUN_MODE=True)
    with pytest.raises(RuntimeError, match="refused when APP_ENV=prod"):
        _guard_dry_run(settings)


def test_prod_without_dry_run_starts_normally():
    """The guard must not block an ordinary production start."""
    from app.main import _guard_dry_run

    _guard_dry_run(Settings(APP_ENV="prod", DRY_RUN_MODE=False))


@pytest.mark.parametrize("env", ["dev", "test"])
def test_dry_run_is_allowed_outside_prod(env):
    from app.main import _guard_dry_run

    _guard_dry_run(Settings(APP_ENV=env, DRY_RUN_MODE=True))


def test_dry_run_start_warns_loudly(caplog):
    """The operator has to be able to tell from the log that this instance is
    not doing what it appears to be doing."""
    from app.main import _guard_dry_run

    with caplog.at_level("WARNING"):
        _guard_dry_run(Settings(APP_ENV="dev", DRY_RUN_MODE=True))

    assert "DRY-RUN MODE ACTIVE" in caplog.text


def test_the_warning_admits_behaviour_is_not_stubbed_yet(caplog):
    """T8 ships the scaffolding only — the Kubernetes and deploy-service
    clients are still real. A banner implying otherwise would be actively
    dangerous, since an operator could believe a drain was safe to run."""
    from app.main import _guard_dry_run

    with caplog.at_level("WARNING"):
        _guard_dry_run(Settings(APP_ENV="dev", DRY_RUN_MODE=True))

    assert "not stubbed yet" in caplog.text


def test_no_warning_when_dry_run_is_off(caplog):
    """A banner on every ordinary start would train operators to ignore it."""
    from app.main import _guard_dry_run

    with caplog.at_level("WARNING"):
        _guard_dry_run(Settings(APP_ENV="dev", DRY_RUN_MODE=False))

    assert "DRY-RUN" not in caplog.text


# ── the response marker ───────────────────────────────────────────────────────


def test_marker_is_false_by_default():
    """Existing clients must see no behavioural change."""
    assert ApiResponse[dict](data={}).dry_run is False


def test_marker_follows_the_setting(monkeypatch):
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()
    assert ApiResponse[dict](data={}).dry_run is True


def test_marker_is_resolved_per_instance_not_at_import(monkeypatch):
    """A default_factory is evaluated at construction. Binding the value at
    import instead would make the marker wrong for any app built after a
    settings change — including every test that toggles the flag."""
    assert ApiResponse[dict](data={}).dry_run is False

    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()

    assert ApiResponse[dict](data={}).dry_run is True


def test_marker_can_still_be_set_explicitly():
    """The factory supplies a default, not an override — a caller that passes
    the field keeps control of it."""
    assert ApiResponse[dict](data={}, dry_run=True).dry_run is True
    assert ApiResponse[dict](data={}, dry_run=False).dry_run is False


def test_marker_is_serialised_in_the_envelope():
    """It has to reach the wire; a field the client never sees is no marker."""
    assert ApiResponse[dict](data={}, request_id="r1").model_dump() == {
        "data": {},
        "request_id": "r1",
        "dry_run": False,
    }


def test_existing_envelope_fields_are_unchanged():
    """The marker is additive. Renaming or dropping data / request_id would
    break every existing client."""
    payload = ApiResponse[dict](data={"k": "v"}, request_id="abc").model_dump()
    assert payload["data"] == {"k": "v"}
    assert payload["request_id"] == "abc"
