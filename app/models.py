"""Database models."""
from datetime import datetime
from sqlalchemy import String, Integer, Text, DateTime, Boolean, ForeignKey, UniqueConstraint
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
    # Structural template ("editorial" default / "banner" - masthead+author-byline
    # style, see email_template.py) - distinct from `theme` (color only), which
    # Claude still picks per issue.
    newsletter_layout: Mapped[str] = mapped_column(String(20), default="editorial")
    schedule_active: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Manual warm-up ramp for a fresh sending domain (0 = no limit, send to everyone).
    # Set this low for the first real campaign, raise/clear it over the ramp
    # schedule in Newsletter_Tool_Email_Sending_Plan.docx, then set back to 0.
    warmup_limit: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    senders: Mapped[list["Sender"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    contacts: Mapped[list["Contact"]] = relationship(back_populates="user", cascade="all, delete-orphan")


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


class Contact(Base):
    """A recipient in a user's own contact list, with suppression state.

    Replaces the old flat User.recipients string. status starts "active" on
    upload; a re-upload of the same email NEVER resets a suppressed status
    back to active (see main.py's upload_recipients) - only a webhook-driven
    bounce/complaint, or the unsubscribe link, changes status after that."""
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("user_id", "email", name="uq_contact_user_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    first_name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active/bounced/unsubscribed/complained
    source: Mapped[str] = mapped_column(String(255), default="")  # uploaded filename, for display
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="contacts")


class Campaign(Base):
    """One final send of a newsletter to the recipient list, via Resend."""
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    subject: Mapped[str] = mapped_column(String(512), default="")
    recipient_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    recipients: Mapped[list["CampaignRecipient"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan")


class CampaignRecipient(Base):
    """Per-contact delivery/engagement status for one campaign - the analytics
    source of truth, updated by the Resend webhook (see main.py's /webhooks/resend)."""
    __tablename__ = "campaign_recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    resend_message_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent/delivered/opened/clicked/bounced/complained/unsubscribed
    last_event_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="recipients")


class Draft(Base):
    """One run of the pipeline: research happens internally, then the FULL drafted
    newsletter is emailed for review in one shot.

    stage lifecycle:
      review -> sent
      (or -> cancelled / no_content / error)

    While in "review", a reply either approves (-> sent) or is treated as feedback
    (stays in "review", content_json is rewritten and re-sent - see pipeline._revise).
    """
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    stage: Mapped[str] = mapped_column(String(32), default="review", index=True)

    # Content
    source_summary: Mapped[str] = mapped_column(Text, default="")  # internal research brief that fed the newsletter (not itself reviewed)
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


class UsageLog(Base):
    """One row per Claude API call, for the /dev/usage page - development-time
    visibility into exactly what's being spent, per call, since this is otherwise
    invisible (no Admin API access on this key)."""
    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    tag: Mapped[str] = mapped_column(String(64), default="")           # call site, e.g. "build_summary"
    description: Mapped[str] = mapped_column(String(255), default="")  # short human reason for the call
    model: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
