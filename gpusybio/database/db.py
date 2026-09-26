"""Engine + session factory (SQLite WAL)."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine as _sa_create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from gpusybio.database import models  # noqa: F401  (register mappings)


def create_engine(db_path: str) -> Engine:
    engine = _sa_create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    models.Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> "SessionFactory":
    return SessionFactory(sessionmaker(bind=engine, expire_on_commit=False, future=True))


class SessionFactory:
    """Thin wrapper: `with factory() as s:` yields a session."""

    def __init__(self, factory: sessionmaker):
        self._factory = factory

    def __call__(self) -> "SessionFactoryContext":
        return SessionFactoryContext(self._factory())

    def session(self) -> Iterator[Session]:
        """Context-manager style access for callers that want a plain session."""
        s = self._factory()
        try:
            yield s
        finally:
            s.close()


class SessionFactoryContext:
    def __init__(self, session: Session):
        self.session = session

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *exc):
        if exc[0] is None:
            self.session.commit()
        else:
            self.session.rollback()
        self.session.close()
        return False
