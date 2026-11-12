"""Terms of the leave and of the retention task. Read by the API (when a leave is
scheduled) and by the worker (which makes it effective and deletes)."""

from datetime import datetime

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.offboarding import RetentionPolicy


class RetentionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FICHAITOR_RETENTION_", env_file=".env", extra="ignore"
    )

    # Everything obtained from Google or derived from it: days after the effective leave.
    google_data_days: int = Field(default=30, ge=0, le=30)
    # Reviews and profiles Google stopped returning: days after it stopped.
    removed_days: int = Field(default=30, ge=0, le=30)
    # What was obtained from Google of a profile the owner deactivated: days after the
    # deactivation, unless it is activated again (docs/multinegocio.md, section 9).
    inactive_days: int = Field(default=30, ge=0, le=30)
    # Sessions: days after they expired or were closed.
    session_days: int = Field(default=30, ge=0, le=30)
    # The record of published replies, the activation log and the plan entitlements:
    # months after the leave. The database never lets audit_log go below 12 (its policy).
    audit_months: int = Field(default=12, ge=12, le=120)
    # Waiting list: months after signing up, and everything before the launch notice.
    waitlist_months: int = Field(default=12, ge=0, le=12)
    waitlist_launched_at: datetime | None = None
    # Fingerprints of the codes of a leave: days after they expired.
    code_days: int = Field(default=30, ge=0, le=30)
    # Messaging channels: messages, events seen, finished proposals and unlinked
    # addresses, days after they were written or ended (docs/canales.md).
    channel_days: int = Field(default=90, ge=1, le=90)
    # Finished jobs (done or failed): days after they finished. Never less than 2, so
    # that the idempotency key of yesterday's daily job is still there.
    job_days: int = Field(default=14, ge=2, le=90)
    # Hour (UTC) of the daily pass.
    hour_utc: int = Field(default=3, ge=0, le=23)

    def policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            google_data_days=self.google_data_days,
            removed_days=self.removed_days,
            inactive_days=self.inactive_days,
            session_days=self.session_days,
            audit_months=self.audit_months,
            waitlist_months=self.waitlist_months,
            waitlist_launched_at=self.waitlist_launched_at,
            code_days=self.code_days,
            channel_days=self.channel_days,
            job_days=self.job_days,
        )
