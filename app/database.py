"""SQLAlchemy engine + session helpers (SQLite), with a tiny auto-migration."""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from . import config

engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False},  # allow scheduler-thread access
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def _migrate():
    """Lightweight schema catch-up so existing dev DBs keep working.
    - adds users.run_hour if missing
    - drops an old-schema `drafts` table (pre email-review) so it can be recreated
    """
    insp = inspect(engine)
    tables = insp.get_table_names()
    with engine.begin() as conn:
        if "users" in tables:
            ucols = [c["name"] for c in insp.get_columns("users")]
            if "run_hour" not in ucols:
                conn.execute(text("ALTER TABLE users ADD COLUMN run_hour INTEGER DEFAULT 7"))
            if "recipients_file" not in ucols:
                conn.execute(text("ALTER TABLE users ADD COLUMN recipients_file VARCHAR(255) DEFAULT ''"))
            if "brand_name" not in ucols:
                conn.execute(text("ALTER TABLE users ADD COLUMN brand_name VARCHAR(255) DEFAULT ''"))
            if "brand_signature" not in ucols:
                conn.execute(text("ALTER TABLE users ADD COLUMN brand_signature TEXT DEFAULT ''"))
            if "provider" not in ucols:
                conn.execute(text("ALTER TABLE users ADD COLUMN provider VARCHAR(16) DEFAULT 'google'"))
            if "imap_conn_enc" not in ucols:
                conn.execute(text("ALTER TABLE users ADD COLUMN imap_conn_enc TEXT DEFAULT ''"))
            if "subscribe_url" not in ucols:
                conn.execute(text("ALTER TABLE users ADD COLUMN subscribe_url VARCHAR(512) DEFAULT ''"))
            if "business_address" not in ucols:
                conn.execute(text("ALTER TABLE users ADD COLUMN business_address VARCHAR(512) DEFAULT ''"))
            if "warmup_limit" not in ucols:
                conn.execute(text("ALTER TABLE users ADD COLUMN warmup_limit INTEGER DEFAULT 0"))
        if "drafts" in tables:
            dcols = [c["name"] for c in insp.get_columns("drafts")]
            if "stage" not in dcols:  # old schema -> recreate (no live drafts yet)
                conn.execute(text("DROP TABLE drafts"))
            else:
                if "theme" not in dcols:
                    conn.execute(text("ALTER TABLE drafts ADD COLUMN theme VARCHAR(32) DEFAULT 'general'"))
                if "content_json" not in dcols:
                    conn.execute(text("ALTER TABLE drafts ADD COLUMN content_json TEXT DEFAULT ''"))


def init_db():
    from . import models  # noqa: F401  (ensure models are registered)
    Base.metadata.create_all(engine)  # ensure base tables exist
    _migrate()                         # adjust older schemas
    Base.metadata.create_all(engine)  # recreate anything dropped


def get_session():
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
