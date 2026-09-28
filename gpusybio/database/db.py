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
    _migrate_columns(engine)


def _scalar_default(col):
    """A DDL-safe scalar default (int/float/str/bool), or None if the column
    has no scalar default (function defaults are left to the ORM layer)."""
    d = col.default
    if d is None or not d.is_scalar:
        return None
    arg = d.arg
    if isinstance(arg, (bool, int, float, str)) and not isinstance(arg, type):
        return arg
    return None


def _migrate_columns(engine: Engine) -> None:
    """Schema evolution for the single local DB (spec 16): ``create_all``
    creates missing tables but never adds columns to existing ones (no alembic).
    Introspect each table and ``ALTER TABLE ... ADD COLUMN`` any model column
    that is missing yet, carrying its type and scalar default — SQLite
    back-fills existing rows with that default on add.
    """
    from sqlalchemy import inspect, text

    if engine.dialect.name != "sqlite":
        return
    existing = inspect(engine)
    tables = existing.get_table_names()
    for table in models.Base.metadata.tables.values():
        if table.name not in tables:
            continue
        have = {c["name"] for c in existing.get_columns(table.name)}
        for col in table.columns:
            if col.name in have or col.primary_key:
                continue
            ddl = f"{col.name} {col.type.compile(dialect=engine.dialect)}"
            default = _scalar_default(col)
            if default is not None:
                if isinstance(default, str):
                    ddl += f" DEFAULT '{default.replace(chr(39), chr(39) * 2)}'"
                else:
                    ddl += f" DEFAULT {default}"
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))


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
