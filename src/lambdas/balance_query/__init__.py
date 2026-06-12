"""balance-query Action Group Lambda package.

Implements Requirement 7 (Consulta de Saldos): given an authenticated client's
phone number, query the inline Mock_Core and return the balances of their
products (Fondos de Inversión and Cuenta Corriente) in COP. Invoked by the
Strands_Agent ``query_balance`` tool via ``boto3 lambda.invoke``.

Runs **inside the VPC** (banking domain) in production; that is purely a
deployment concern (Task 15) and requires no code change here.
"""
