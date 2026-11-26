"""
FILE: saas_app/db.py
RUOLO: Gestione della connessione SQLAlchemy (PostgreSQL in Kubernetes / SQLite per test).
"""

from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from saas_app.config import saas_settings


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str):
    """Crea l'engine SQLAlchemy configurando correttamente SQLite vs PostgreSQL."""
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            return create_engine(
                database_url,
                connect_args=connect_args,
                poolclass=StaticPool,
            )
        return create_engine(database_url, connect_args=connect_args)

    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


engine = build_engine(saas_settings.DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """Dependency FastAPI per fornire una sessione transazionale per richiesta."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
