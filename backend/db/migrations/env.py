"""Alembic 環境設定。

backend.db.session の engine とモデル定義を直接参照することで、
sqlalchemy.url を ini で二重管理せず、autogenerate も自動で動く形にする。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

# モデルを import して metadata を読み込む（autogenerate が動くために必要）
from backend.db import models  # noqa: F401  # モデル定義を読み込ませるための side effect import
from backend.db.session import DATABASE_URL, Base, engine

# Alembic 設定オブジェクト
config = context.config

# logging 設定の読み込み
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate のためのターゲット metadata
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """オフラインモード（DB に接続せず SQL を出力するだけ）。"""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite は ALTER TABLE が限定的なので、batch_mode を有効化
        render_as_batch=DATABASE_URL.startswith("sqlite"),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """オンラインモード（実際の DB に接続して適用）。"""
    from sqlalchemy import text as _text

    with engine.connect() as connection:
        # SQLite の batch_alter_table は内部で DROP TABLE → RENAME を行う。
        # PRAGMA foreign_keys=ON のままだと CASCADE DELETE が発火してデータが
        # 消える事故が起きる（2026-09-07 本番事故の教訓）。
        # マイグレーション中は FK を無効化し、終了後に戻す。
        _is_sqlite = DATABASE_URL.startswith("sqlite")
        if _is_sqlite:
            connection.execute(_text("PRAGMA foreign_keys=OFF"))

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite,
        )

        with context.begin_transaction():
            context.run_migrations()

        if _is_sqlite:
            connection.execute(_text("PRAGMA foreign_keys=ON"))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
