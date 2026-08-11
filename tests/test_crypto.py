"""Field encryption and blind index. ADR-012.

Two of these tests are the ones worth showing a panel: the same number
encrypted twice produces different ciphertext, and the same number indexed
twice produces identical bytes. Those two facts together are the whole design.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidTag

from bga.crypto.field import (
    blind_index,
    decrypt_msisdn,
    encrypt_msisdn,
    mask_msisdn,
    normalise_msisdn,
)

NUMBER = "+263771234567"
OTHER = "+263772234567"


# --- normalisation ---------------------------------------------------------


def test_all_three_documented_formats_normalise_to_the_same_value():
    assert normalise_msisdn("0771234567") == NUMBER
    assert normalise_msisdn("263771234567") == NUMBER
    assert normalise_msisdn("+263771234567") == NUMBER


def test_normalisation_is_idempotent():
    assert normalise_msisdn(normalise_msisdn("0771234567")) == NUMBER


def test_separators_are_stripped():
    assert normalise_msisdn("077 123 4567") == NUMBER
    assert normalise_msisdn("+263-77-123-4567") == NUMBER
    assert normalise_msisdn(" (0771) 234567 ") == NUMBER


def test_all_zimbabwean_mobile_networks_are_accepted():
    for prefix in ("071", "073", "077", "078"):  # NetOne, Telecel, Econet
        assert normalise_msisdn(f"{prefix}1234567").startswith("+263")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "0123456789",       # not a mobile prefix
        "077123456",        # too short
        "07712345678",      # too long
        "+27771234567",     # South Africa
        "771234567",        # no country code and no leading zero
        "+263abcdefghi",    # not digits
        "0771234567x",
    ],
)
def test_malformed_numbers_raise(bad):
    with pytest.raises(ValueError):
        normalise_msisdn(bad)


def test_non_string_input_raises():
    with pytest.raises(ValueError):
        normalise_msisdn(263771234567)


def test_the_error_message_never_quotes_the_number():
    """An exception that echoes its input is how an MSISDN reaches a log file."""
    for bad in ("0771234567x", "+27771234567", "0123456789"):
        with pytest.raises(ValueError) as exc:
            normalise_msisdn(bad)
        message = str(exc.value)
        assert not any(ch.isdigit() for ch in message), message
        assert bad not in message


# --- encryption ------------------------------------------------------------


def test_round_trip():
    assert decrypt_msisdn(encrypt_msisdn(NUMBER)) == NUMBER


def test_round_trip_normalises_on_the_way_in():
    assert decrypt_msisdn(encrypt_msisdn("0771234567")) == NUMBER


def test_two_encryptions_of_the_same_number_differ():
    """Proves the nonce is random. If these matched, the column would be a
    frequency-analysable identifier and the encryption would buy nothing."""
    first = encrypt_msisdn(NUMBER)
    second = encrypt_msisdn(NUMBER)

    assert first != second
    assert first[:12] != second[:12], "the nonces themselves must differ"
    assert decrypt_msisdn(first) == decrypt_msisdn(second) == NUMBER


def test_ciphertext_layout_is_nonce_ciphertext_tag():
    blob = encrypt_msisdn(NUMBER)
    # 12-byte nonce + 13 bytes of plaintext + 16-byte GCM tag
    assert len(blob) == 12 + len(NUMBER) + 16


def test_ciphertext_does_not_contain_the_number():
    """Acceptance requirement 4, at the value level: bytes, not a phone number."""
    blob = encrypt_msisdn(NUMBER)
    assert NUMBER.encode() not in blob
    assert b"771234567" not in blob
    # Deliberately not asserting on a 3-byte needle like b"263": in 41 bytes of
    # ciphertext that collides by chance about once in 400,000 runs.


def test_decrypt_of_tampered_bytes_raises():
    blob = bytearray(encrypt_msisdn(NUMBER))
    blob[-1] ^= 0x01  # flip one bit of the authentication tag

    with pytest.raises(InvalidTag):
        decrypt_msisdn(bytes(blob))


def test_decrypt_of_a_tampered_nonce_raises():
    blob = bytearray(encrypt_msisdn(NUMBER))
    blob[0] ^= 0x01

    with pytest.raises(InvalidTag):
        decrypt_msisdn(bytes(blob))


def test_decrypt_of_a_truncated_value_raises():
    with pytest.raises(ValueError):
        decrypt_msisdn(encrypt_msisdn(NUMBER)[:20])


def test_decrypt_of_something_that_is_not_bytes_raises():
    with pytest.raises(ValueError):
        decrypt_msisdn("not bytes")


# --- blind index -----------------------------------------------------------


def test_two_blind_indexes_of_the_same_number_are_identical():
    """This is what the UNIQUE constraint deduplicates on, without decrypting."""
    assert blind_index(NUMBER) == blind_index(NUMBER)


def test_blind_index_is_identical_across_input_formats():
    """The dedup property survives a donor enrolling twice in different formats."""
    assert blind_index("0771234567") == blind_index("+263771234567") == blind_index(
        "263 77 123 4567"
    )


def test_blind_indexes_of_different_numbers_differ():
    assert blind_index(NUMBER) != blind_index(OTHER)


def test_blind_index_is_a_sha256_digest():
    assert len(blind_index(NUMBER)) == 32


def test_blind_index_does_not_contain_the_number():
    assert b"771234567" not in blind_index(NUMBER)


def test_blind_index_is_not_a_plain_hash():
    """Keyed with the pepper, so a rainbow table over the numbering plan does
    not recover it from the digest alone."""
    from hashlib import sha256

    assert blind_index(NUMBER) != sha256(NUMBER.encode()).digest()


# --- masking ---------------------------------------------------------------


def test_mask_format():
    assert mask_msisdn(NUMBER) == "+263 77* *** 567"


def test_mask_accepts_any_input_format():
    assert mask_msisdn("0771234567") == "+263 77* *** 567"


def test_mask_hides_the_middle_digits():
    masked = mask_msisdn(NUMBER)
    assert "1234" not in masked
    assert masked.count("*") == 4


# --- key handling ----------------------------------------------------------


def looks_like_key_material(source: str) -> str | None:
    """Any string literal that decodes to 16+ bytes of base64, or 32+ hex chars.

    Walks the parse tree rather than the text, so docstrings and comments are
    skipped and a real literal cannot hide behind formatting.
    """
    import ast
    import binascii

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        candidate = node.value.strip()
        if len(candidate) < 24:
            continue
        if all(c in "0123456789abcdefABCDEF" for c in candidate):
            return candidate
        try:
            if len(base64.b64decode(candidate, validate=True)) >= 16:
                return candidate
        except (binascii.Error, ValueError):
            continue
    return None


def test_no_key_material_is_hardcoded_in_the_module():
    """Keys come from settings only (NFR-SEC-04)."""
    import inspect

    from bga.crypto import field

    found = looks_like_key_material(inspect.getsource(field))
    assert found is None, f"possible hardcoded key material: {found!r}"


def test_the_hardcoded_key_detector_is_not_vacuous():
    """A guard that cannot fail is not a guard."""
    planted = base64.b64encode(b"k" * 32).decode()
    assert looks_like_key_material(f"KEY = {planted!r}") == planted
    assert looks_like_key_material("KEY = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6'") is not None
    assert looks_like_key_material("KEY = settings.field_key_b64") is None


def test_a_wrong_length_key_is_rejected(monkeypatch):
    from bga.config import settings

    monkeypatch.setattr(settings, "field_key_b64", base64.b64encode(b"tooshort").decode())
    with pytest.raises(ValueError) as exc:
        encrypt_msisdn(NUMBER)
    assert "32 bytes" in str(exc.value)


def test_key_errors_do_not_leak_the_key(monkeypatch):
    from bga.config import settings

    secret = base64.b64encode(b"x" * 8).decode()
    monkeypatch.setattr(settings, "field_key_b64", secret)
    with pytest.raises(ValueError) as exc:
        encrypt_msisdn(NUMBER)
    assert secret not in str(exc.value)
