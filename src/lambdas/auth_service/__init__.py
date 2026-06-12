"""Auth_Service Lambda package.

Mock authentication service (Lambda + DynamoDB) backing the Login_Page. Exposes
a single ``POST /authenticate`` endpoint via a Lambda Function URL: it validates
the signed callback token embedded in the login link, checks the submitted
credentials against a small set of hardcoded demo users, verifies the user's
phone number matches the one the login link was issued for, and on success
writes an Auth_Session item (30-minute TTL) to the Auth_Session DynamoDB table
owned by the ``infra`` repo.

See :mod:`handler` for the request/response contract and :mod:`users` for the
hardcoded demo users.
"""

from __future__ import annotations
