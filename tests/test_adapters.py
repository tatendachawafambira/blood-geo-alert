"""Gateway adapters.

The idempotency tests here are the direct evidence for acceptance requirement
7 - replay the same outbox row, no duplicate alert is created. Without them
that property exists only in a scratch script nobody runs.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from bga.adapters import mock_sms
from bga.adapters.africastalking import SEND_MESSAGE_URL, AfricasTalkingAdapter
from bga.adapters.base import (
    GatewayAdapter,
    SubmitResult,
    get_adapter,
    primary_gateway_name,
    secondary_gateway_name,
)
from bga.adapters.mock_sms import MockSmsAdapter

TO = "+263771234567"
BODY = "Blood is needed near you. Tap to reply: https://example/d/abc"


@pytest.fixture()
def adapter() -> MockSmsAdapter:
    """A fresh adapter per test, so one test's issued ids cannot leak."""
    return MockSmsAdapter()


@pytest.fixture(autouse=True)
def no_injected_failures(monkeypatch):
    """Default every test to a working gateway; opt in to failure explicitly."""
    monkeypatch.setattr(mock_sms, "failure_rate", 0.0)


# --- the registry ----------------------------------------------------------


def test_both_adapters_satisfy_the_protocol():
    assert isinstance(get_adapter("mock"), GatewayAdapter)
    assert isinstance(get_adapter("africastalking"), GatewayAdapter)


def test_get_adapter_returns_the_same_instance_each_time():
    """The mock's idempotency table lives on the instance. A new object per
    call would reset it and silently break ADR-010."""
    assert get_adapter("mock") is get_adapter("mock")


def test_unknown_gateway_raises_rather_than_falling_back():
    """A misconfigured gateway name must not present as messages that appear
    to send and never arrive.

    The name here must be one that will never become a real adapter. This test
    previously used "twilio", which stopped testing anything the day a Twilio
    adapter was added - the assertion passed for the wrong reason right up
    until it failed.
    """
    with pytest.raises(ValueError, match="unknown gateway"):
        get_adapter("carrier-pigeon")


def test_gateway_names_come_from_settings():
    from bga.config import settings

    assert primary_gateway_name() == settings.primary_gateway
    assert secondary_gateway_name() == settings.secondary_gateway


def test_settings_reject_a_failure_rate_outside_zero_to_one():
    from pydantic import ValidationError

    from bga.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            database_url_owner="postgresql+psycopg://u@h/d",
            database_url_app="postgresql+psycopg://u@h/d",
            database_url_audit="postgresql+psycopg://u@h/d",
            field_key_b64="x",
            blind_pepper_b64="x",
            mock_failure_rate=1.5,
        )


# --- idempotency (ADR-010) -------------------------------------------------


def test_resubmitting_the_same_key_returns_the_same_message_id(adapter):
    first = adapter.submit(TO, BODY, "alert-abc")
    second = adapter.submit(TO, BODY, "alert-abc")

    assert first.accepted and second.accepted
    assert first.provider_message_id == second.provider_message_id


def test_different_keys_get_different_message_ids(adapter):
    a = adapter.submit(TO, BODY, "alert-abc")
    b = adapter.submit(TO, BODY, "alert-xyz")

    assert a.provider_message_id != b.provider_message_id


def test_message_id_has_the_documented_shape(adapter):
    message_id = adapter.submit(TO, BODY, "alert-abc").provider_message_id

    assert message_id.startswith("mock-")
    assert len(message_id) == len("mock-") + 36  # a uuid4


def test_idempotency_beats_the_failure_roll(adapter, monkeypatch):
    """An accepted key keeps its id even when every submission is failing.

    If the failure roll came first, a retry storm could turn an already
    accepted message into a failure and the worker would send it twice.
    """
    original = adapter.submit(TO, BODY, "alert-abc").provider_message_id
    monkeypatch.setattr(mock_sms, "failure_rate", 1.0)

    replayed = adapter.submit(TO, BODY, "alert-abc")

    assert replayed.accepted is True
    assert replayed.provider_message_id == original


def test_a_failed_submission_records_no_message_id(adapter, monkeypatch):
    """A retry must get a fresh roll, not inherit a phantom id."""
    monkeypatch.setattr(mock_sms, "failure_rate", 1.0)
    assert adapter.submit(TO, BODY, "never-accepted").provider_message_id is None

    monkeypatch.setattr(mock_sms, "failure_rate", 0.0)
    retried = adapter.submit(TO, BODY, "never-accepted")
    assert retried.accepted and retried.provider_message_id is not None


def test_concurrent_submissions_of_one_key_mint_one_id(adapter):
    """Competing consumers: two workers must not both get an id for one alert."""
    for trial in range(50):
        key = f"race-{trial}"
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = {r.provider_message_id for r in pool.map(
                lambda _: adapter.submit(TO, BODY, key), range(8)
            )}
        assert len(ids) == 1, f"trial {trial} minted {len(ids)} ids for one key"


# --- failure injection -----------------------------------------------------


def test_failure_rate_zero_never_fails(adapter):
    assert all(adapter.submit(TO, BODY, f"k{i}").accepted for i in range(100))


def test_failures_are_marked_retriable(adapter, monkeypatch):
    monkeypatch.setattr(mock_sms, "failure_rate", 1.0)
    result = adapter.submit(TO, BODY, "k")

    assert result.accepted is False
    assert result.retriable is True
    assert result.error


def test_failure_rate_is_roughly_honoured(adapter, monkeypatch):
    monkeypatch.setattr(mock_sms, "failure_rate", 0.5)
    failures = sum(not adapter.submit(TO, BODY, f"k{i}").accepted for i in range(2000))

    assert 850 < failures < 1150, f"{failures}/2000 is not close to half"


# --- receipts --------------------------------------------------------------


def test_mock_accepts_any_webhook(adapter):
    assert adapter.verify_webhook({}, b"") is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [("Delivered", "DELIVERED"), ("success", "DELIVERED"), ("opened", "OPENED")],
)
def test_parse_receipt_maps_provider_status_to_alert_state(adapter, status, expected):
    assert adapter.parse_receipt({"id": "mock-1", "status": status}) == ("mock-1", expected)


def test_parse_receipt_accepts_either_id_field(adapter):
    assert adapter.parse_receipt(
        {"provider_message_id": "mock-2", "status": "delivered"}
    ) == ("mock-2", "DELIVERED")


def test_parse_receipt_rejects_a_receipt_with_no_message_id(adapter):
    with pytest.raises(ValueError, match="no provider message id"):
        adapter.parse_receipt({"status": "delivered"})


def test_parse_receipt_rejects_an_unrecognised_status(adapter):
    with pytest.raises(ValueError, match="unrecognised receipt status"):
        adapter.parse_receipt({"id": "mock-1", "status": "banana"})


# --- SubmitResult ----------------------------------------------------------


def test_submit_result_is_frozen():
    result = SubmitResult(accepted=True, provider_message_id="x", error=None, retriable=False)
    with pytest.raises(Exception):
        result.accepted = False


def test_submit_result_requires_every_field():
    """No defaults: a forgotten retriable= would dead-letter a message that
    should have been retried, and nothing would look wrong."""
    with pytest.raises(TypeError):
        SubmitResult(accepted=False)


# --- the africastalking seam ----------------------------------------------


def test_africastalking_is_a_skeleton_and_says_so():
    at = AfricasTalkingAdapter()

    with pytest.raises(NotImplementedError):
        at.submit(TO, BODY, "k")
    with pytest.raises(NotImplementedError):
        at.verify_webhook({}, b"")
    with pytest.raises(NotImplementedError):
        at.parse_receipt({})


def test_africastalking_records_the_real_endpoint():
    assert SEND_MESSAGE_URL == "https://api.africastalking.com/version1/messaging"


def test_africastalking_adds_no_network_dependency():
    """The seam must not have quietly become an integration."""
    import inspect

    import bga.adapters.africastalking as module

    source = inspect.getsource(module)
    for banned in ("import requests", "import httpx", "urllib.request", "socket."):
        assert banned not in source


# --- demonstration-gateway allowlist ---------------------------------------
# The seeded register holds 800 structurally valid Zimbabwean numbers using
# real network prefixes. A demonstration gateway pointed at it without this
# control would send unsolicited SMS to strangers, and a single four-unit
# request matches up to forty donors. These tests exist so that control cannot
# be removed or weakened without a test going red.


def _android(monkeypatch, allowlist):
    from bga.adapters import allowlist as al
    from bga.adapters import android_sms

    monkeypatch.setattr(al.settings, "sms_allowlist", allowlist, raising=False)
    monkeypatch.setattr(
        android_sms.settings, "android_sms_url", "http://127.0.0.1:9", raising=False
    )
    return android_sms.AndroidSmsAdapter()


def test_empty_allowlist_blocks_every_real_send(monkeypatch):
    """Empty must mean 'send nothing', never 'send to anyone'.

    A forgotten setting has to fail safe. The opposite default would make the
    failure silent, irreversible and aimed at people who never consented.
    """
    result = _android(monkeypatch, "").submit("+263771234567", "body", "key")
    assert result.accepted is False
    assert result.retriable is False
    assert "SMS_ALLOWLIST" in result.error


def test_number_not_on_the_allowlist_is_suppressed(monkeypatch):
    """A seeded donor's number must not be messaged even when matching picks it."""
    adapter = _android(monkeypatch, "+263771234567")
    result = adapter.submit("+263790000001", "body", "key")
    assert result.accepted is False
    assert result.retriable is False, "retrying a suppression is pure noise"


def test_suppression_never_echoes_the_number(monkeypatch):
    """The blocked number belongs to someone. It must not leak into an error."""
    adapter = _android(monkeypatch, "+263771234567")
    result = adapter.submit("+263790000001", "body", "key")
    assert "790000001" not in (result.error or "")


def test_allowlisted_number_passes_the_guard(monkeypatch):
    """The control must not block the number it exists to permit."""
    adapter = _android(monkeypatch, "+263771234567")
    result = adapter.submit("+263771234567", "body", "key")
    # Reaches the transport and fails there, rather than being suppressed.
    assert "allowlist" not in (result.error or "").lower()


def test_allowlist_accepts_local_format_and_several_entries(monkeypatch):
    """0771234567 and +263771234567 are the same person."""
    adapter = _android(monkeypatch, "0771234567, +263712222222")
    result = adapter.submit("+263771234567", "body", "key")
    assert "allowlist" not in (result.error or "").lower()
