"""Shared utilities for BTG ConnectAI Lambdas.

Packaged into a common Lambda Layer so every function can import them as
``from shared.masking import mask_phone`` etc.

The package intentionally performs **no eager submodule imports** so that
lightweight, dependency-free helpers (``masking``, ``formatting``,
``constants``, ``types``) can be imported without pulling in heavier runtime
dependencies (e.g. ``aws_lambda_powertools`` used by ``shared.logger``). Import
the specific submodule you need.
"""

__all__: list[str] = []
