"""SQLAlchemy engine, session factory and schema bootstrap."""

from __future__ import annotations

from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL, ensure_dirs
from .models import Base

ensure_dirs()

engine = create_engine(
    DATABASE_URL,
    # SQLite + FastAPI threadpool: sessions can hop threads.
    connect_args={"check_same_thread": False},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


# Columns added after Stage 1 shipped. create_all() only creates missing
# tables, so an existing mrlistener.db needs these bolted on by hand.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("meetings", "pipeline_stage", "VARCHAR(32)"),
    ("meetings", "pipeline_error", "TEXT"),
    ("meetings", "auto_title", "VARCHAR(500)"),
    ("meetings", "suggestions_json", "TEXT"),
)


def _migrate() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, column, ddl_type in _ADDED_COLUMNS:
            if table not in tables:
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            if column in existing:
                continue
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
