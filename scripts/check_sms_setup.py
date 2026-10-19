"""Is the real-SMS demonstration ready? Plain answers, in order.

    python scripts/check_sms_setup.py
    python scripts/check_sms_setup.py +263771234567

Run this before you rely on the demo. It checks every link in the chain and
tells you the ONE next thing to fix, rather than a wall of diagnostics.

Give it your phone number and it also confirms that number is actually in the
donor register - the step people most often forget, and the one that makes the
difference between a message arriving and nothing happening at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from bga.config import settings  # noqa: E402
from bga.crypto.field import blind_index, mask_msisdn, normalise_msisdn  # noqa: E402

OK, BAD, WARN = "  [ OK ]  ", "  [FIX!]  ", "  [ ?? ]  "
_problems: list[str] = []


def fail(line: str, fix: str) -> None:
    print(BAD + line)
    _problems.append(fix)


def check_database() -> bool:
    try:
        with create_engine(settings.database_url_owner).connect() as c:
            c.execute(text("select 1"))
        print(OK + "Database is running.")
        return True
    except Exception:
        fail(
            "Database is NOT running.",
            "Open a terminal in the project folder and run:  docker compose up -d",
        )
        return False


def check_gateway() -> str:
    """Validate the name against the real registry, not against 'not mock'.

    An earlier version of this check accepted any name that was not "mock" and
    reported it as OK. A transposed 'andriod' therefore passed the readiness
    check and then failed at dispatch with unknown gateway - the check said
    ready, the system sent nothing, and the two disagreed silently. A readiness
    check that can pass while the system is broken is worse than no check.
    """
    from bga.adapters.base import get_adapter

    name = settings.primary_gateway

    for label, value in (("PRIMARY_GATEWAY", name),
                         ("SECONDARY_GATEWAY", settings.secondary_gateway)):
        try:
            get_adapter(value)
        except ValueError as exc:
            fail(
                f"{label} is '{value}', which is not a real gateway.",
                f"In .env correct {label}. {exc}".replace("unknown gateway", "Valid names are"),
            )
            return "invalid"

    if name == "mock":
        print(
            WARN
            + "Gateway is 'mock' - realistic on screen, but NO SMS leaves the laptop."
        )
        _problems.append(
            "In .env set PRIMARY_GATEWAY and SECONDARY_GATEWAY to 'android' "
            "(or 'twilio'), then restart the dashboard and the worker."
        )
    else:
        print(OK + f"Gateway is '{name}' - real messages will be sent.")
    return name


def check_allowlist(number: str | None) -> None:
    """The allowlist decides who can actually be messaged. Check it explicitly."""
    from bga.adapters.allowlist import allowed_numbers

    permitted = allowed_numbers()
    if not permitted:
        fail(
            "SMS_ALLOWLIST is empty - every real send is blocked.",
            "In .env set SMS_ALLOWLIST to your test number, e.g. "
            "SMS_ALLOWLIST=+263771234567",
        )
        return

    print(OK + f"Allowlist permits {len(permitted)} number(s) - nobody else can be messaged.")

    if number:
        try:
            e164 = normalise_msisdn(number)
        except Exception:
            return
        if e164 not in permitted:
            fail(
                f"{mask_msisdn(e164)} is NOT on the allowlist, so it will be blocked.",
                f"In .env add it:  SMS_ALLOWLIST={e164}",
            )


def check_android() -> None:
    if not settings.android_sms_url:
        fail(
            "The phone's address is not set.",
            "In .env fill in ANDROID_SMS_URL, ANDROID_SMS_USERNAME and "
            "ANDROID_SMS_PASSWORD from the SMS-gateway app on your phone.",
        )
        return

    auth = None
    if settings.android_sms_username and settings.android_sms_password:
        auth = (settings.android_sms_username, settings.android_sms_password)

    url = settings.android_sms_url.rstrip("/")
    try:
        # Any answer at all proves the phone is reachable on this network.
        r = httpx.get(f"{url}/health", auth=auth, timeout=6.0)
        reachable = True
    except httpx.HTTPError:
        try:
            r = httpx.post(f"{url}/message", json={}, auth=auth, timeout=6.0)
            reachable = True
        except httpx.HTTPError:
            reachable = False
            r = None

    if not reachable:
        fail(
            f"Cannot reach the phone at {url}.",
            "Check: the phone is awake, the gateway app is OPEN and showing "
            "'running', and the phone is on the SAME wifi/hotspot as this "
            "laptop. Then re-check the IP address in the app - it changes when "
            "the phone rejoins a network.",
        )
        return

    if r is not None and r.status_code in (401, 403):
        fail(
            "The phone answered, but rejected the username or password.",
            "Re-copy ANDROID_SMS_USERNAME and ANDROID_SMS_PASSWORD from the app "
            "into .env, exactly as shown.",
        )
        return

    print(OK + f"Phone is reachable at {url} and accepted the login.")


def check_twilio() -> None:
    if not (settings.twilio_account_sid and settings.twilio_auth_token
            and settings.twilio_from_number):
        fail(
            "Twilio is selected but the account details are not filled in.",
            "In .env fill in TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and "
            "TWILIO_FROM_NUMBER from your Twilio console.",
        )
        return
    try:
        r = httpx.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}.json",
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            timeout=10.0,
        )
    except httpx.HTTPError:
        fail(
            "Could not reach Twilio.",
            "Check this laptop has internet access.",
        )
        return

    if r.status_code == 401:
        fail(
            "Twilio rejected the account details.",
            "Re-copy TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN from the console.",
        )
        return
    if r.status_code != 200:
        fail(f"Twilio answered with HTTP {r.status_code}.", "Check the account is active.")
        return

    kind = r.json().get("type", "")
    print(OK + f"Twilio account works ({kind or 'unknown type'}).")
    if kind == "Trial":
        print(
            WARN
            + "Trial account: it will ONLY send to numbers you have verified in "
            "the Twilio console, and it adds a prefix to every message."
        )


def check_number_in_register(raw: str) -> None:
    try:
        e164 = normalise_msisdn(raw)
    except Exception:
        fail(f"{raw!r} is not a valid Zimbabwean mobile number.",
             "Use the form +263771234567 or 0771234567.")
        return

    try:
        with create_engine(settings.database_url_owner).connect() as c:
            row = c.execute(
                text(
                    """
                    SELECT d.id, w.name AS ward, d.abo, d.rh, d.availability,
                           d.last_alerted_at, e.deferral_flag, e.next_eligible_date
                      FROM core.donor d
                      JOIN core.ward w ON w.id = d.ward_id
                      LEFT JOIN core.donor_eligibility e ON e.donor_id = d.id
                     WHERE d.msisdn_blind_index = :bi
                    """
                ),
                {"bi": blind_index(e164)},
            ).first()
    except Exception:
        return  # database problem already reported

    if row is None:
        fail(
            f"{mask_msisdn(e164)} is NOT in the donor register - no SMS can reach you.",
            f"Run:  python scripts/set_demo_donor.py {raw}",
        )
        return

    group = f"{row.abo}{'+' if row.rh == 'POS' else '-'}"
    print(OK + f"{mask_msisdn(e164)} is in the register - {row.ward}, {group}.")

    if row.last_alerted_at is not None:
        print(
            WARN
            + "This donor was contacted recently, so the 14-day frequency cap "
            "will skip them."
        )
        _problems.append(
            f"Run:  python scripts/set_demo_donor.py {raw}   (it clears that cap)"
        )
    if row.deferral_flag:
        print(BAD + "This donor is deferred and will never be alerted - by design.")
        _problems.append(f"Pick a different donor:  python scripts/set_demo_donor.py {raw}")


def main() -> int:
    print("\n  BLOOD GEO ALERT - real SMS readiness check\n")

    if not check_database():
        _report()
        return 1

    gateway = check_gateway()
    if gateway == "android":
        check_android()
    elif gateway == "twilio":
        check_twilio()

    if gateway not in ("mock", "invalid"):
        check_allowlist(sys.argv[1] if len(sys.argv) > 1 else None)

    if len(sys.argv) > 1:
        check_number_in_register(sys.argv[1])
    else:
        print(
            WARN
            + "No phone number given, so I cannot confirm your number is in the "
            "register."
        )
        _problems.append(
            "Re-run with your number:  python scripts/check_sms_setup.py +263771234567"
        )

    return _report()


def _report() -> int:
    print()
    if not _problems:
        print("  Everything is ready. Raise a request and the SMS will arrive.\n")
        return 0
    print("  DO THIS NEXT:\n")
    for i, fix in enumerate(_problems, 1):
        print(f"   {i}. {fix}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
