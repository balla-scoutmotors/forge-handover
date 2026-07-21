import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

from streamlit_markdown_converter import StreamlitJSONToMarkdown

try:
    import boto3
except Exception:  # pragma: no cover - import error handled at runtime
    boto3 = None

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - import error handled at runtime
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = BASE_DIR / ".env"
REQUIRED_ENV_KEYS = ("S3_BUCKET_KEY_ID", "S3_BUCKET_SECRET_ID", "S3_BUCKET_NAME")


def load_runtime_env() -> None:
    """Load environment variables from .env if python-dotenv is available."""
    if load_dotenv is None:
        return
    try:
        env_path = Path(os.getenv("FORGE_ENV_FILE", str(DEFAULT_ENV_PATH)))
        loaded = load_dotenv(env_path)
        if not loaded:
            raise FileNotFoundError(f"Unable to read .env file at {env_path}")
    except Exception as ex:
        raise RuntimeError(f"Unable to read .env file at {DEFAULT_ENV_PATH}: {ex}") from ex


def validate_runtime_env() -> None:
    """Validate required runtime values loaded from .env or process env."""
    missing_keys = [key for key in REQUIRED_ENV_KEYS if not os.getenv(key)]
    if missing_keys:
        raise ValueError(
            "Missing required environment values: " + ", ".join(missing_keys)
        )


load_runtime_env()


class DetectionEventPublisher:
    """Create per-event JSON/MD artifacts and upload both to S3."""

    def __init__(
        self,
        s3_bucket: str,
        s3_prefix: str = "detections",
        s3_upload_enabled: bool = True,
    ) -> None:
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix.strip("/")
        self.s3_upload_enabled = s3_upload_enabled

        self.s3_client = None
        if self.s3_upload_enabled:
            if boto3 is None:
                raise ImportError(
                    "boto3 is required for S3 upload. Install with: pip install boto3"
                )
            self.s3_client = self._create_s3_client()

    @staticmethod
    def _create_s3_client():
        """Create S3 client using env-routed credentials when provided."""
        try:
            validate_runtime_env()
            access_key = os.getenv("S3_BUCKET_KEY_ID")
            secret_key = os.getenv("S3_BUCKET_SECRET_ID")

            if not access_key or not secret_key:
                raise ValueError(
                    "S3_BUCKET_KEY_ID and S3_BUCKET_SECRET_ID must be present in .env"
                )

            return boto3.client(
                "s3",
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )
        except Exception as ex:
            raise RuntimeError(f"Failed to create S3 client from .env values: {ex}") from ex

    def publish_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Build one event's JSON + MD and upload both objects to S3."""
        event_id = self._build_event_id(event)
        json_filename = f"{event_id}.json"
        md_filename = f"{event_id}.md"

        event_with_id = dict(event)
        event_with_id["event_id"] = event_id

        json_content = json.dumps(event_with_id, indent=2)
        markdown_content = StreamlitJSONToMarkdown.json_events_to_markdown([event_with_id])

        result = {
            "event_id": event_id,
            "s3_json_key": None,
            "s3_md_key": None,
            "uploaded_to_s3": False,
        }

        if self.s3_upload_enabled and self.s3_client is not None:
            json_key = f"{self.s3_prefix}/defect-json/{json_filename}"
            md_key = f"{self.s3_prefix}/defect-md/{md_filename}"

            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=json_key,
                Body=json_content.encode("utf-8"),
                ContentType="application/json",
                Metadata={"event_id": event_id, "artifact_type": "json"},
            )
            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=md_key,
                Body=markdown_content.encode("utf-8"),
                ContentType="text/markdown",
                Metadata={"event_id": event_id, "artifact_type": "md"},
            )

            result["s3_json_key"] = json_key
            result["s3_md_key"] = md_key
            result["uploaded_to_s3"] = True

        return result

    @staticmethod
    def _build_event_id(event: Dict[str, Any]) -> str:
        """Build a stable, readable unique ID for one event."""
        event_time = event.get("event_time") or datetime.now(timezone.utc).isoformat()
        safe_time = (
            str(event_time)
            .replace(":", "")
            .replace("-", "")
            .replace("T", "_")
            .replace(".", "")
            .replace("+", "")
            .replace("Z", "")
        )
        station = str(event.get("station", "na"))
        camera = str(event.get("camera", "na"))
        track_id = str(event.get("track_id", "na"))
        class_name = str(event.get("class_name", "na"))
        short_uuid = uuid4().hex[:8]

        return f"{safe_time}_{station}_{camera}_{class_name}_{track_id}_{short_uuid}".lower()
