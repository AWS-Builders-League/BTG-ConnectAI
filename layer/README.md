# Shared Lambda Layer

This layer bundles the shared application code (`src/shared/`) together with the
runtime dependencies declared in [`src/requirements.txt`](../src/requirements.txt)
(`twilio`, `aws-lambda-powertools[parser]`, `strands-agents`, `fpdf2`). Every
Lambda in this repo attaches this layer and imports the helpers as:

```python
from shared.logger import get_logger
from shared.masking import mask_phone
from shared.formatting import format_cop
from shared.constants import MAX_TWILIO_MESSAGE_LENGTH
from shared.types import TwilioWebhookPayload
```

## Packaging convention (Python 3.13)

AWS Lambda expects the contents of a Python layer under a top-level `python/`
directory, which is added to `sys.path` at runtime. The build produces:

```
layer/
  python/
    shared/            # copied verbatim from src/shared/
      __init__.py
      constants.py
      formatting.py
      logger.py
      masking.py
      types.py
    aws_lambda_powertools/   # pip-installed dependency
    twilio/                  # pip-installed dependency
    fpdf/                    # pip-installed dependency
    ...                      # remaining deps + dist-info
  shared-layer.zip     # the artifact uploaded to the infra artifacts bucket
```

The `python/` tree and the `*.zip` artifact are build outputs and are
**git-ignored** — only the build scripts and this README are committed.

## Building

Build dependencies on a **Linux x86_64** target so that any wheels with native
extensions (e.g. `pydantic` pulled in by `aws-lambda-powertools[parser]`) match
the Lambda runtime. CI runs `build.sh`; locally use the same script under WSL or
a Linux container.

```bash
# from the repo root
bash layer/build.sh
```

`build.ps1` is provided for convenience on Windows but cross-compiles wheels via
`pip --platform manylinux2014_x86_64`; prefer `build.sh` on Linux/CI for
reproducible artifacts.

## How it maps to CloudFormation

The CloudFormation Lambda templates (Task 15) reference the published
`shared-layer.zip` from the `infra` artifacts bucket and expose it as a
`AWS::Lambda::LayerVersion` consumed via each function's `Layers:` property.
