"""データベース層のエントリポイント。

公開する主なシンボル:
- Base: 全 ORM モデルの基底
- engine: SQLAlchemy エンジン
- SessionLocal: セッションファクトリ
- get_db: FastAPI 依存（リクエストごとに DB セッションを払い出す）

モデル定義は ``backend.db.models`` を参照。
"""

from backend.db.session import Base, SessionLocal, engine, get_db

__all__ = ["Base", "SessionLocal", "engine", "get_db"]
