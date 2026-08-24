from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker


def validate_auth_database_url(database_url: str | None) -> str:
    if database_url is None or not database_url.strip():
        raise ValueError("AUTH_DATABASE_URL must be set")

    try:
        parsed_url = make_url(database_url)
    except ArgumentError as exc:
        raise ValueError("AUTH_DATABASE_URL must be a valid SQLAlchemy URL") from exc

    if parsed_url.drivername != "mysql+pymysql":
        raise ValueError("AUTH_DATABASE_URL must use mysql+pymysql")
    if parsed_url.database is None or not parsed_url.database.strip():
        raise ValueError("AUTH_DATABASE_URL must include a database name")

    return database_url


def get_auth_database_url() -> str:
    return validate_auth_database_url(os.environ.get("AUTH_DATABASE_URL"))


def create_auth_engine() -> Engine:
    return create_engine(get_auth_database_url(), pool_pre_ping=True)


def create_auth_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=create_auth_engine(), expire_on_commit=False)
