"""Alembic environment configuration for SaaS PostgreSQL / SQLite migrations."""

from logging.config import fileConfig

from alembic import context

from saas_app.config import saas_settings
from saas_app.db import Base, build_engine
import saas_app.models  # noqa: F401  -- registra tutti i modelli su Base.metadata

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: non silenziare i logger dell'applicazione (es. seed_cli / seed)
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    return config.get_main_option("sqlalchemy.url") or saas_settings.DATABASE_URL


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = get_url()
    connectable = build_engine(url)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
