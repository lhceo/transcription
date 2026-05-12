"""SQLAlchemy エンジンとセッションの初期化。

SQLite を使う前提で、起動時に PRAGMA で WAL モード等を有効化する。
本番運用時にエンジンの差し替えが必要になったら、ここを env で
切り替えられる形に拡張する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# DB ファイルはリポジトリルートの data/ 配下に置く。
# .gitignore に登録済みなので Git に乗らない。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _REPO_ROOT / "data"
_DATA_DIR.mkdir(exist_ok=True)
_DB_PATH = _DATA_DIR / "app.db"


# DATABASE_URL 環境変数で上書き可能（本番では Postgres などに切り替える想定）
import os

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{_DB_PATH}")


# SQLite の場合は同一スレッドチェックを無効化する必要がある（FastAPI は
# スレッドプールで動くため）。Postgres などの場合は不要。
_engine_kwargs: dict = {"future": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
    """SQLite 接続ごとに PRAGMA を設定する。

    - foreign_keys=ON: 外部キー制約を有効化（SQLite はデフォルトで無効）
    - journal_mode=WAL: 並列読み書きを改善
    - synchronous=NORMAL: 書き込みの安全性と速度のバランス
    """
    if not DATABASE_URL.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """全 ORM モデルの基底クラス。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依存。リクエストごとに DB セッションを開いて自動でクローズする。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
