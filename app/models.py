"""Database models."""
from datetime import datetime
from sqlalchemy import String, Integer, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), default="google")  # google | imap
    google_sub: Mapped[str] = mapped_column(String(255), default="", index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    # Encrypted OAuth material (google) / IMAP+SMTP connection JSON (imap)
    token_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    imap_conn_enc: Mapped[str] = mapped_column(Text, default="")

    # Newsletter settings
    frequency: Mapped[str] = mapped_column(String(32), default="")  # daily/twice_week/thrice_week/weekly
    run_hour: Mapped[int] = mapped_column(Integer, default=7)       # hour of day (UTC) to start a run
    recipients: Mapped[str] = mapped_column(Text, default="")       # emails parsed from the uploaded Excel
    recipients_file: Mapped[str] = mapped_column(String(255), default="")  # original filename, for display

    # Branding / voice used when writing the newsletter
    brand_name: Mapped[str] = mapped_column(String(255), default="")       # e.g. "Rohan / Irizpro Learning Solutions"
    brand_signature: Mapped[str] = mapped_column(Text, default="")         # sign-off block, e.g. name, title, company, link
    subscribe_url: Mapped[str] = mapped_column(String(512), default="")    # link for the Subscribe button
    business_address: Mapped[str] = mapped_column(String(512), default="") # footer address (bulk-email compliance)
    schedule_active: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    senders: Mapped[list["Sender"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Sender(Base):
    """A detected newsletter sender the user may choose to include."""
    __tablename__ = "senders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(64), default="other")  # newsletter/marketing/ai/other
    count: Mapped[int] = mapped_column(Integer, default=0)              # msgs seen in scan window
    selected: Mapped[bool] = mapped_column(Boolean, default=False)      # user opted in

    user: Mapped["User"] = relationship(back_populates="senders")


class Draft(Base):
    """One run of the pipeline, moving through email-based review stages.

    stage lifecycle:
      summary_review -> email_review -> sent
      (or -> cancelled / no_content / error)
    """
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    stage: Mapped[str] = mapped_column(String(32), default="summary_review", index=True)

    # Content
    source_summary: Mapped[str] = mapped_column(Text, default="")  # the (revisable) summary under review
    subject: Mapped[str] = mapped_column(String(512), default="")
    body_html: Mapped[str] = mapped_column(Text, default="")       # legacy/unused - see content_json
    content_json: Mapped[str] = mapped_column(Text, default="")    # structured sections (see ai_service.py), the (revisable) newsletter content under review
    theme: Mapped[str] = mapped_column(String(32), default="general")  # visual template: tech/business/general
    source_count: Mapped[int] = mapped_column(Integer, default=0)

    # Email-review bookkeeping
    thread_id: Mapped[str] = mapped_column(String(128), default="")       # current approval thread being watched
    awaiting_msg_id: Mapped[str] = mapped_column(String(128), default="") # replies newer than this count
    revision_count: Mapped[int] = mapped_column(Integer, default=0)

    error: Mapped[str] = mapped_column(Text, default="")
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="drafts")
