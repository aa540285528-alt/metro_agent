from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def get_auth_database_url() -> str:
    return os.environ["AUTH_DATABASE_URL"]


def create_auth_engine() -> Engine:
    return create_engine(get_auth_database_url(), pool_pre_ping=True)


def create_auth_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=create_auth_engine(), expire_on_commit=False)
