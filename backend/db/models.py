"""SQLAlchemy ORM モデル定義。

スキーマの正典は ``docs/DATA_MODEL.md``。ここの定義と一致させること。
スキーマを変更したら Alembic でマイグレーションを生成すること:

    alembic revision --autogenerate -m "短い説明"
    alembic upgrade head
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.session import Base


def _utcnow() -> datetime:
    """SQLite では timezone-aware を保持するために、こちらで UTC 時刻を生成する。"""
    return datetime.now(timezone.utc)


# ── users ──────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    picture_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )

    transcripts: Mapped[list["Transcript"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    speaker_history: Mapped[list["SpeakerHistory"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


# ── transcripts ────────────────────────────────────────────────────────────
class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    # ユーザーがリネームした表示名。未設定なら original_filename を表示に使う。
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ステータス列挙: uploading / processing / completed / failed / deleted
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # モデル選択: best / nano
    model_tier: Mapped[str] = mapped_column(String(10), nullable=False, default="best")
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="ja")

    assemblyai_transcript_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_yen: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    user: Mapped["User"] = relationship(back_populates="transcripts")
    segments: Mapped[list["Segment"]] = relationship(
        back_populates="transcript",
        cascade="all, delete-orphan",
        order_by="Segment.order_index",
    )
    speakers: Mapped[list["Speaker"]] = relationship(
        back_populates="transcript",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # 「一覧表示」（ユーザー別・soft delete 除外・新しい順）用の複合 INDEX
        Index(
            "ix_transcripts_user_deleted_created",
            "user_id",
            "deleted_at",
            "created_at",
        ),
    )


# ── segments ───────────────────────────────────────────────────────────────
class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transcript_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False)

    # 内部ラベル（例: "SPEAKER_00"）
    speaker_label: Mapped[str] = mapped_column(String(50), nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)

    # 個別の話者名上書き（NULL なら speakers テーブルの display_name を引く）
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    is_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    transcript: Mapped["Transcript"] = relationship(back_populates="segments")

    __table_args__ = (
        Index("ix_segments_transcript_order", "transcript_id", "order_index"),
    )


# ── speakers ───────────────────────────────────────────────────────────────
class Speaker(Base):
    __tablename__ = "speakers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transcript_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    speaker_label: Mapped[str] = mapped_column(String(50), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    transcript: Mapped["Transcript"] = relationship(back_populates="speakers")

    __table_args__ = (
        UniqueConstraint(
            "transcript_id", "speaker_label", name="uq_speakers_transcript_label"
        ),
    )


# ── speaker_history ────────────────────────────────────────────────────────
class SpeakerHistory(Base):
    __tablename__ = "speaker_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    user: Mapped["User"] = relationship(back_populates="speaker_history")

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_speaker_history_user_name"),
        Index("ix_speaker_history_user_recent", "user_id", "last_used_at"),
    )
