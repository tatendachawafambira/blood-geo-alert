"""MSISDN encryption and blind index. ADR-012.

The mobile number is stored twice, because two requirements pull in opposite
directions. FR-DON-07 needs deduplication on the number, which requires
equality comparison; randomised encryption makes equality impossible.
Encrypting deterministically would restore equality and destroy the security
property at the same time - identical numbers would produce identical
ciphertext and the column would become a frequency-analysable identifier.

So: AES-256-GCM with a random nonce for the value that gets used, and an
HMAC-SHA256 blind index under a separate pepper for the value that gets looked
up. Lookup and deduplication touch only the blind index and never decrypt.

Both operations happen in the application. The database never holds key
material, which is what makes a database backup something other than a
decryptable artefact.

Two rules this module keeps, and every caller must keep too:
  - No key, and no decrypted number, ever appears in an exception message, a
    log line, or a repr. The ValueErrors below deliberately describe the shape
    of the problem without quoting the input.
  - Decrypted numbers live in local variables for the length of one gateway
    submission and are not stored, cached or returned upwards.
"""

from __future__ import annotations

import base64
import hmac
import os
import re
from functools import lru_cache
from hashlib import sha256

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bga.config import settings

# AES-256 needs a 256-bit key; GCM's standard nonce is 96 bits. A 12-byte
# random nonce with a per-row lifetime is far inside the birthday bound for a
# register of this size.
_KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16

# Characters that carry no information in a written phone number. Anything
# else is a data error and is rejected rather than guessed at.
_SEPARATORS = re.compile(r"[\s\-().]")

# Zimbabwe national significant number: nine digits beginning with 7.
# Covers Econet 77/78, NetOne 71 and Telecel 73, and any future 7x range.
_NATIONAL = re.compile(r"^7\d{8}$")

COUNTRY_CODE = "+263"


def normalise_msisdn(raw: str) -> str:
    """Return a Zimbabwean mobile number as +263XXXXXXXXX.

    Accepts 0771234567, 263771234567 and +263771234567, with or without
    spaces, hyphens, dots or brackets. Raises ValueError on anything else.

    The error deliberately does not quote the offending value: this function
    sits on the enrolment and import paths, and an exception that echoes its
    input is how a mobile number ends up in a log file.
    """
    if not isinstance(raw, str):
        raise ValueError("msisdn must be a string")

    cleaned = _SEPARATORS.sub("", raw.strip())

    if cleaned.startswith(COUNTRY_CODE):
        national = cleaned[len(COUNTRY_CODE):]
    elif cleaned.startswith("263"):
        national = cleaned[3:]
    elif cleaned.startswith("0"):
        national = cleaned[1:]
    else:
        raise ValueError("msisdn is not in a recognised Zimbabwean format")

    if not _NATIONAL.match(national):
        raise ValueError("msisdn is not a valid Zimbabwean mobile number")

    return COUNTRY_CODE + national


@lru_cache(maxsize=2)
def _cipher(key_b64: str) -> AESGCM:
    """Cached on the configured value, so rotating the setting takes effect."""
    try:
        key = base64.b64decode(key_b64, validate=True)
    except Exception:  # noqa: BLE001
        # "from None" drops the cause: a chained binascii error would repr the
        # offending value, which is the key.
        raise ValueError("field key is not valid base64") from None
    if len(key) != _KEY_BYTES:
        raise ValueError(f"field key must be {_KEY_BYTES} bytes, got {len(key)}")
    return AESGCM(key)


@lru_cache(maxsize=2)
def _pepper(pepper_b64: str) -> bytes:
    try:
        pepper = base64.b64decode(pepper_b64, validate=True)
    except Exception:  # noqa: BLE001
        raise ValueError("blind index pepper is not valid base64") from None
    if not pepper:
        raise ValueError("blind index pepper is empty")
    return pepper


def encrypt_msisdn(plain_e164: str) -> bytes:
    """AES-256-GCM. Returns nonce || ciphertext || tag as one value.

    The number is normalised first, so a record enrolled as 0771234567 and one
    enrolled as +263771234567 decrypt to the same string.
    """
    normalised = normalise_msisdn(plain_e164)
    nonce = os.urandom(_NONCE_BYTES)
    sealed = _cipher(settings.field_key_b64).encrypt(nonce, normalised.encode("ascii"), None)
    return nonce + sealed


def decrypt_msisdn(blob: bytes) -> str:
    """Reverse encrypt_msisdn. Raises if the value has been tampered with.

    GCM authenticates as well as encrypts, so a single flipped bit anywhere in
    the nonce, ciphertext or tag fails here rather than returning a plausible
    wrong number.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError("ciphertext must be bytes")

    blob = bytes(blob)
    if len(blob) < _NONCE_BYTES + _TAG_BYTES:
        raise ValueError("ciphertext is too short to be a valid AES-GCM value")

    nonce, sealed = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    return _cipher(settings.field_key_b64).decrypt(nonce, sealed, None).decode("ascii")


def blind_index(plain_e164: str) -> bytes:
    """Deterministic HMAC-SHA256 of the normalised number.

    This is what the UNIQUE constraint on donor.msisdn_blind_index enforces
    deduplication against, without anything ever being decrypted.

    Normalisation happens inside deliberately. If the caller were trusted to
    normalise first, one path passing 0771234567 and another passing
    +263771234567 would produce two different indexes for one person, and the
    deduplication requirement would fail silently - which is the exact failure
    this design exists to prevent.
    """
    normalised = normalise_msisdn(plain_e164)
    return hmac.new(
        _pepper(settings.blind_pepper_b64), normalised.encode("ascii"), sha256
    ).digest()


def mask_msisdn(plain_e164: str) -> str:
    """'+263 77* *** 567'. Gateway console display only.

    Shows the network prefix and the last three digits: enough for an operator
    to correlate a message with a delivery report, not enough to dial.
    """
    national = normalise_msisdn(plain_e164)[len(COUNTRY_CODE):]
    return f"{COUNTRY_CODE} {national[:2]}* *** {national[-3:]}"
