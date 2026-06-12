"""Property-based tests for the Auth_Service credential check.

Covers the invalid-credentials correctness property from the design document:

    * Property 8 — Invalid Credentials Rejection (Validates: Requirements 5.5)

Testing approach
----------------
``find_user`` is a *pure* function over the hardcoded ``TEST_USERS`` store: it
returns the matching user only when the username exists AND the password matches
exactly, and ``None`` for every other input. Because it touches no AWS, these
properties are tested directly with no moto / DynamoDB fixture.

The core property (Req 5.5) is that *any* (username, password) pair that is not a
registered credential pair is rejected (``find_user`` → ``None``). We exercise
this through several disjoint families of "invalid" inputs:

    * unknown username (not in ``TEST_USERS``) — any password,
    * a registered username with a wrong password,
    * empty / blank username or password.

A positive control (registered username + correct password → the user) guards
against a degenerate implementation that rejects everything.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from lambdas.auth_service.users import TEST_USERS, find_user

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_REGISTERED_USERNAMES = {u["username"] for u in TEST_USERS}

_MAX_EXAMPLES = 100

# Arbitrary text for usernames/passwords across the input space.
_text = st.text()

# A registered demo user (for the wrong-password and positive-control cases).
_registered_users = st.sampled_from(TEST_USERS)


# ---------------------------------------------------------------------------
# Property 8: Invalid Credentials Rejection
# Validates: Requirements 5.5
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(username=_text, password=_text)
def test_unknown_username_is_rejected(username: str, password: str) -> None:
    """Any username not in TEST_USERS is rejected regardless of password."""
    assume(username not in _REGISTERED_USERNAMES)
    assert find_user(username, password) is None


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(user=_registered_users, password=_text)
def test_registered_username_wrong_password_is_rejected(user, password: str) -> None:
    """A registered username with a non-matching password is rejected."""
    assume(password != user["password"])
    assert find_user(user["username"], password) is None


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(
    username=st.sampled_from(sorted(_REGISTERED_USERNAMES) + ["", " "]),
    password=st.sampled_from(["", " "]),
)
def test_blank_credentials_are_rejected(username: str, password: str) -> None:
    """Empty/blank username or password never authenticates."""
    # An empty username or a blank/non-matching password must be rejected.
    assume(username == "" or username == " " or password in ("", " "))
    assert find_user(username, password) is None


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(user=_registered_users)
def test_valid_credentials_are_accepted(user) -> None:
    """Positive control: a registered (username, correct password) pair matches.

    Guards against a degenerate ``find_user`` that rejects every input, which
    would make the rejection properties vacuously true.
    """
    result = find_user(user["username"], user["password"])
    assert result is not None
    assert result["username"] == user["username"]
