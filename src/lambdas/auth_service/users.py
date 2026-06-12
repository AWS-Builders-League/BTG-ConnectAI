"""Hardcoded demo users for the mock Auth_Service (Req 5.7).

This module is the mock identity store for the hackathon MVP. It holds at least
three test users with predefined credentials so the Login_Page flow can be
demoed end-to-end without a real identity provider (Req 5.7).

.. warning::
    **Mock-only / demo data.** Passwords are stored in *plaintext* on purpose so
    the demo is easy to run and reason about. This is acceptable ONLY because
    these are throwaway synthetic users for a hackathon. Never use this pattern
    for real credentials — extension EXT-2 replaces this module entirely with a
    real identity provider (Cognito / OIDC) and MFA.

Consistency note
----------------
The ``phone_number`` and ``document_id`` of each user are the join keys into the
``Mock_Core`` banking data used by the Action Group Lambdas (balance-query,
transfer-breb, statement-generator, implemented in task 7). Keep these values in
sync with that mock data so an authenticated user actually has banking products
to operate on.
"""

from __future__ import annotations

from typing import TypedDict


class TestUser(TypedDict):
    """A hardcoded demo user record (mock identity store entry)."""

    username: str
    password: str  # plaintext — mock/demo only (see module warning)
    phone_number: str  # E.164, links to Mock_Core
    name: str
    document_id: str  # identity document, links to Mock_Core
    email: str


# At least 3 predefined test users for the demo (Req 5.7). The phone numbers are
# Colombian E.164 numbers; document ids are synthetic Colombian "cédula" numbers.
TEST_USERS: list[TestUser] = [
    {
        "username": "carlos.rodriguez",
        "password": "BtgDemo2025!",
        "phone_number": "+573001112233",
        "name": "Carlos Rodríguez",
        "document_id": "1010101010",
        "email": "carlos.rodriguez@example.com",
    },
    {
        "username": "maria.lopez",
        "password": "BtgDemo2025!",
        "phone_number": "+573004445566",
        "name": "María López",
        "document_id": "2020202020",
        "email": "maria.lopez@example.com",
    },
    {
        "username": "juan.garcia",
        "password": "BtgDemo2025!",
        "phone_number": "+573007778899",
        "name": "Juan García",
        "document_id": "3030303030",
        "email": "juan.garcia@example.com",
    },
]

# Index by username for O(1) lookup.
_USERS_BY_USERNAME: dict[str, TestUser] = {u["username"]: u for u in TEST_USERS}


def find_user(username: str, password: str) -> TestUser | None:
    """Return the demo user matching ``username`` + ``password``, else ``None``.

    Implements the credential check for Req 5.3 / 5.5: a user is returned only
    when both the username exists and the password matches exactly. Any mismatch
    (unknown username or wrong password) yields ``None`` so the caller can reject
    the login with an "invalid credentials" error (Req 5.5).

    Inputs are treated as opaque strings; ``None``/empty inputs simply fail to
    match. No normalization is applied to the username (it must match exactly).

    Args:
        username: The submitted username.
        password: The submitted plaintext password (mock/demo only).

    Returns:
        The matching :class:`TestUser`, or ``None`` if no user matches.
    """
    if not username or password is None:
        return None

    user = _USERS_BY_USERNAME.get(username)
    if user is None:
        return None

    # Plaintext comparison — mock/demo only. A real implementation would use a
    # constant-time hash verification (e.g. bcrypt/argon2).
    if user["password"] != password:
        return None

    return user


__all__ = ["TestUser", "TEST_USERS", "find_user"]
