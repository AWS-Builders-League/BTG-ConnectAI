"""Audio transcription module for the Message_Processor Lambda.

Implements the voice-note pipeline described in design §Transcripción de Audio:
a WhatsApp audio note is downloaded from Twilio, handed to Amazon Transcribe and
turned into Spanish text that is then fed to the Strands_Agent as if the client
had typed it.

Requirements covered
--------------------
* **Req 2.2** — an audio note is sent to Amazon Transcribe and the resulting
  text is processed as agent input. :func:`transcribe_audio` returns that text
  (the caller passes it to the agent); on any failure it returns ``None`` so the
  caller can reply with an error message.
* **Req 2.3** — audio is transcribed to Spanish text. The job is started with
  ``LanguageCode="es-CO"`` (Colombian Spanish). The async SQS-driven design
  removes Twilio-side time pressure; polling is bounded (default 30s) well above
  the 10s target for ≤60s notes.
* **Req 2.6** — WhatsApp's native OGG/Opus is supported without prior
  conversion: the object is uploaded as ``audio/ogg`` and the job is started
  with ``MediaFormat="ogg"``.

Cross-stack / environment contract
----------------------------------
The temporary audio bucket (``Audio_Temp``) is owned by the ``infra`` repo and
its name is injected via the ``AUDIO_TEMP_BUCKET`` environment variable
(resolved from the cross-stack contract ``BTGConnectAI-sandbox-AudioTempBucketName``
/ SSM ``/btgconnectai/sandbox/s3/audio-temp-name``). Twilio credentials used to
download the media come from ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN``
(loaded from Secrets Manager in production). Every variable is read lazily so the
module can be imported without the environment being configured (tests inject it
before calling).

Dependency note
---------------
Media is downloaded with the standard-library :mod:`urllib.request` using HTTP
Basic Auth rather than ``requests`` (which is intentionally **not** in
``requirements.txt``) so this module adds no new runtime dependency. ``boto3`` is
provided by the Lambda runtime.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any
from urllib.request import Request, urlopen

import boto3

from shared.logger import get_logger
from shared.masking import mask_phone

logger = get_logger("message-processor")

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Amazon Transcribe job-name prefix (job names must be unique per account/region;
# a uuid4 suffix guarantees uniqueness).
JOB_NAME_PREFIX: str = "btg-connectai-"

# Colombian Spanish, per Req 2.3.
LANGUAGE_CODE: str = "es-CO"

# WhatsApp ships voice notes as OGG/Opus; Transcribe consumes it natively
# (Req 2.6) — no transcoding step.
MEDIA_FORMAT: str = "ogg"

# S3 key prefixes inside the Audio_Temp bucket.
AUDIO_KEY_PREFIX: str = "audio-temp"
TRANSCRIPT_KEY_PREFIX: str = "transcriptions"

# Polling defaults (overridable by callers/tests). The 30s ceiling matches the
# design's "Max wait para Amazon Transcribe" budget; the 120s Lambda timeout
# leaves comfortable headroom.
DEFAULT_TIMEOUT_SECONDS: float = 30.0
DEFAULT_POLL_INTERVAL: float = 1.0

# Maximum bytes to read from a Twilio media download. WhatsApp voice notes are
# tiny, but bounding the read avoids unbounded memory use on a bad URL.
MAX_MEDIA_BYTES: int = 16 * 1024 * 1024  # 16 MiB

# Terminal Transcribe job states.
_JOB_COMPLETED = "COMPLETED"
_JOB_FAILED = "FAILED"

# Module-level boto3 clients so connections are reused across warm invocations.
s3 = boto3.client("s3")
transcribe = boto3.client("transcribe")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_audio_temp_bucket() -> str:
    """Return the Audio_Temp bucket name from the environment (read lazily).

    Returns:
        The S3 bucket name configured via ``AUDIO_TEMP_BUCKET``.

    Raises:
        KeyError: If ``AUDIO_TEMP_BUCKET`` is not set.
    """
    return os.environ["AUDIO_TEMP_BUCKET"]


def _twilio_basic_auth_header() -> str:
    """Build the HTTP Basic Auth header value for Twilio media downloads.

    Twilio media URLs are protected and require the Account SID / Auth Token as
    Basic Auth credentials. Reads ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN``
    lazily so importing the module never requires them.

    Returns:
        The ``"Basic <base64>"`` header value.

    Raises:
        KeyError: If the Twilio credential environment variables are not set.
    """
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    raw = f"{account_sid}:{auth_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def download_twilio_media(media_url: str) -> bytes:
    """Download the audio bytes from a Twilio Media URL using Basic Auth.

    Args:
        media_url: The Twilio-hosted media URL (``MediaUrl0`` from the webhook).

    Returns:
        The raw media bytes.

    Raises:
        urllib.error.URLError: If the HTTP request fails.
        KeyError: If the Twilio credential environment variables are not set.
    """
    request = Request(media_url, headers={"Authorization": _twilio_basic_auth_header()})
    with urlopen(request) as response:  # noqa: S310 - URL comes from trusted Twilio webhook
        return response.read(MAX_MEDIA_BYTES)


def wait_for_transcription(
    job_name: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> str | None:
    """Poll Amazon Transcribe until the job completes, fails or times out.

    The polling parameters are injectable so tests can drive the loop without
    real waits.

    Args:
        job_name: The Transcribe job name to poll.
        timeout_seconds: Maximum total time to wait before giving up.
        poll_interval: Seconds to sleep between ``get_transcription_job`` calls.
        sleep: Sleep function (injectable for tests). Defaults to ``time.sleep``.
        monotonic: Monotonic clock (injectable for tests). Defaults to
            ``time.monotonic``.

    Returns:
        The transcript text on success, or ``None`` if the job failed or the
        timeout elapsed before completion.
    """
    deadline = monotonic() + timeout_seconds
    while True:
        response = transcribe.get_transcription_job(TranscriptionJobName=job_name)
        job = response["TranscriptionJob"]
        status = job["TranscriptionJobStatus"]

        if status == _JOB_COMPLETED:
            return _read_transcript_from_job(job)

        if status == _JOB_FAILED:
            logger.error(
                "transcription job failed",
                extra={
                    "jobName": job_name,
                    "failureReason": job.get("FailureReason"),
                },
            )
            return None

        if monotonic() >= deadline:
            logger.warning(
                "transcription job timed out",
                extra={"jobName": job_name, "timeoutSeconds": timeout_seconds},
            )
            return None

        sleep(poll_interval)


def _read_transcript_from_job(job: dict[str, Any]) -> str | None:
    """Extract the transcript text for a COMPLETED job from its S3 output.

    The job is configured with ``OutputBucketName``/``OutputKey`` so the result
    JSON lives in the Audio_Temp bucket. We read that object and pull
    ``results.transcripts[0].transcript``.

    Args:
        job: The ``TranscriptionJob`` dict from ``get_transcription_job``.

    Returns:
        The transcript string, or ``None`` if it could not be located/parsed.
    """
    bucket = _get_audio_temp_bucket()
    transcript_uri = job.get("Transcript", {}).get("TranscriptFileUri")
    key = _output_key_from_job_name(job["TranscriptionJobName"])

    # Prefer reading by the deterministic OutputKey we set on the job, but fall
    # back to parsing the bucket/key out of the returned TranscriptFileUri.
    if transcript_uri:
        parsed_key = _key_from_s3_uri(transcript_uri, bucket)
        if parsed_key:
            key = parsed_key

    response = s3.get_object(Bucket=bucket, Key=key)
    payload = json.loads(response["Body"].read())
    transcripts = payload.get("results", {}).get("transcripts", [])
    if not transcripts:
        logger.error("transcription result has no transcripts", extra={"key": key})
        return None
    return transcripts[0].get("transcript")


def _output_key_from_job_name(job_name: str) -> str:
    """Return the deterministic transcription output key for a job name."""
    return f"{TRANSCRIPT_KEY_PREFIX}/{job_name}.json"


def _key_from_s3_uri(uri: str, bucket: str) -> str | None:
    """Best-effort extraction of an S3 object key from a transcript file URI.

    Transcribe returns either an ``s3://bucket/key`` URI or an HTTPS URL of the
    form ``https://s3.<region>.amazonaws.com/<bucket>/<key>``. We only need the
    key (the bucket is already known from the contract).

    Args:
        uri: The ``TranscriptFileUri`` returned by Transcribe.
        bucket: The known Audio_Temp bucket name.

    Returns:
        The object key, or ``None`` if it could not be derived.
    """
    marker = f"{bucket}/"
    idx = uri.find(marker)
    if idx == -1:
        return None
    return uri[idx + len(marker) :]


def cleanup_temp_files(*keys: str) -> None:
    """Delete temporary S3 objects, swallowing per-object errors.

    Cleanup is best-effort: a failure to delete a temp object must never mask a
    successful transcription (the bucket also has a 1-day lifecycle rule as a
    safety net). Each delete is attempted independently.

    Args:
        *keys: S3 object keys to delete from the Audio_Temp bucket.
    """
    bucket = _get_audio_temp_bucket()
    for key in keys:
        if not key:
            continue
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception:  # noqa: BLE001 - cleanup must not raise
            logger.warning("failed to delete temp object", extra={"key": key})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transcribe_audio(
    twilio_media_url: str,
    phone_number: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> str | None:
    """Transcribe a WhatsApp voice note to Colombian Spanish text.

    Pipeline (design §Transcripción de Audio):

    1. Download the audio bytes from the Twilio Media URL (HTTP Basic Auth with
       the Twilio Account SID / Auth Token).
    2. Upload the bytes to the Audio_Temp bucket as ``audio/ogg``.
    3. Start an Amazon Transcribe job (``es-CO``, OGG, output written back to the
       same bucket).
    4. Poll until the job completes/fails or the timeout elapses.
    5. Read the transcript text from the job's S3 output.
    6. Clean up the temporary audio + transcript objects.

    All failures are caught and logged; the function returns ``None`` so the
    caller can reply with the ``transcription_failed`` error message (Req 2.2).

    Args:
        twilio_media_url: The Twilio media URL of the voice note (``MediaUrl0``).
        phone_number: The client's phone number (used only for masked logging).
        timeout_seconds: Max time to wait for the job (default 30s, injectable).
        poll_interval: Seconds between polls (default 1s, injectable for tests).

    Returns:
        The transcribed Spanish text, or ``None`` on any failure.
    """
    audio_key = f"{AUDIO_KEY_PREFIX}/{uuid.uuid4()}.ogg"
    transcript_key: str | None = None
    masked = mask_phone(phone_number)

    try:
        bucket = _get_audio_temp_bucket()

        # 1. Download audio from Twilio.
        audio_bytes = download_twilio_media(twilio_media_url)

        # 2. Upload to the temporary bucket for Transcribe (OGG/Opus, Req 2.6).
        s3.put_object(
            Bucket=bucket,
            Key=audio_key,
            Body=audio_bytes,
            ContentType="audio/ogg",
        )

        # 3. Start the transcription job (Colombian Spanish, Req 2.3).
        job_name = f"{JOB_NAME_PREFIX}{uuid.uuid4()}"
        transcript_key = _output_key_from_job_name(job_name)
        transcribe.start_transcription_job(
            TranscriptionJobName=job_name,
            LanguageCode=LANGUAGE_CODE,
            MediaFormat=MEDIA_FORMAT,
            Media={"MediaFileUri": f"s3://{bucket}/{audio_key}"},
            OutputBucketName=bucket,
            OutputKey=transcript_key,
        )
        logger.info(
            "started transcription job",
            extra={"jobName": job_name, "phone": masked},
        )

        # 4 + 5. Poll until done and read the transcript.
        transcript = wait_for_transcription(
            job_name,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )

        if transcript is not None:
            logger.info("transcription succeeded", extra={"phone": masked})
        return transcript
    except Exception:
        logger.exception("Audio transcription failed", extra={"phone": masked})
        return None
    finally:
        # 6. Best-effort cleanup of temp files regardless of outcome.
        cleanup_temp_files(audio_key, transcript_key or "")


__all__ = [
    "JOB_NAME_PREFIX",
    "LANGUAGE_CODE",
    "MEDIA_FORMAT",
    "download_twilio_media",
    "wait_for_transcription",
    "cleanup_temp_files",
    "transcribe_audio",
]
