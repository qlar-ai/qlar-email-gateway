"""First-run enrolment.

The downloadable bundle contains a **one-time enrolment code**, never a private key. The
gateway generates its own key pair here, on the customer's machine, and registers only the
public half. That way the private key has never existed anywhere else — not in Qlar's
memory, not in a browser's download folder, not in whatever chat app the bundle was
forwarded through.

The enrolment request is signed with the very key being registered. Qlar verifies it
against the submitted public key, which proves the caller actually holds the private half
rather than having copied someone else's public key out of a screenshot.

Enrolment does not make the gateway usable. Qlar puts it in `pending_approval` until a
human in the CMS compares the fingerprint shown there with the one printed here and
approves it. That comparison is what stops a stolen enrolment code from silently
registering someone else's machine.
"""

from __future__ import annotations

import platform
from datetime import UTC, datetime

from . import PROTOCOL_VERSION, __version__
from .client import QlarClient, QlarNotAnEndpoint, QlarRejected
from .config import EnrollmentState, Settings
from .crypto import fingerprint, load_or_create_private_key, public_key_pem

ENROLL_PATH = "/enroll"


class EnrollmentError(Exception):
    pass


def enroll(settings: Settings) -> tuple[EnrollmentState, str]:
    """Registers this gateway with Qlar. Returns the new state and the key fingerprint."""
    if not settings.enrollment_code:
        raise EnrollmentError(
            "QLAR_ENROLLMENT_CODE is not set. Copy the code shown in the Qlar CMS "
            "(Plugins -> SQL Database Reader -> Gateway) into your .env and run again."
        )

    existing = EnrollmentState.load(settings.state_file)
    if existing is not None:
        raise EnrollmentError(
            f"this gateway is already enrolled as {existing.gateway_id}. "
            f"To re-enrol, delete {settings.state_file} (and {settings.key_file} to rotate the key)."
        )

    private_key, created = load_or_create_private_key(settings.key_file)
    public_pem = public_key_pem(private_key)
    key_fingerprint = fingerprint(public_pem)

    client = QlarClient(
        base_url=settings.base_url,
        private_key=private_key,
        gateway_id=None,
        verify_tls=settings.verify_tls,
    )

    payload = {
        "enrollmentCode": settings.enrollment_code,
        "publicKeyPem": public_pem,
        "fingerprint": key_fingerprint,
        "name": settings.gateway_name,
        "hostname": platform.node(),
        "platform": f"{platform.system()} {platform.release()}",
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
        "providers": [settings.database.provider],
    }

    try:
        _, body = client.post(ENROLL_PATH, payload, timeout=30.0)
    except QlarNotAnEndpoint as wrong_address:
        # Checked before QlarRejected, which it subclasses: a 404 from a static website
        # used to be reported as a rejected code, which sends someone back to the CMS to
        # generate fresh codes that fail exactly the same way.
        raise EnrollmentError(
            f"{wrong_address}.\n"
            f"  That is not Qlar's API, so the enrolment code was never seen. QLAR_BASE_URL is\n"
            f"  currently {settings.base_url} - it must be the API endpoint shown in the CMS\n"
            "  gateway panel, which ends in /api/email-gateway, not the address of the Qlar web\n"
            "  interface. Fix it in .env, or run: qlar-email-gateway enroll --init"
        ) from wrong_address
    except QlarRejected as rejection:
        if rejection.status in (400, 404, 410):
            raise EnrollmentError(
                "Qlar rejected the enrolment code. Codes are single-use and expire after "
                "15 minutes - generate a fresh one in the CMS and try again."
            ) from rejection
        raise EnrollmentError(f"enrolment failed: {rejection}") from rejection

    gateway_id = body.get("gatewayId")
    qlar_public_key = body.get("qlarPublicKeyPem")
    if not gateway_id or not qlar_public_key:
        raise EnrollmentError(f"Qlar's response was missing gatewayId/qlarPublicKeyPem: {body}")

    state = EnrollmentState(
        gateway_id=str(gateway_id),
        # Pinned here and used to verify every later job. Trust is established once, at
        # enrolment, and never re-fetched — a key that could be replaced at runtime would
        # be no protection at all.
        qlar_public_key_pem=str(qlar_public_key),
        enrolled_at=datetime.now(UTC).isoformat(),
        base_url=settings.base_url,
    )
    state.save(settings.state_file)

    if created:
        pass  # the key file was written with 0600 by load_or_create_private_key

    return state, key_fingerprint
