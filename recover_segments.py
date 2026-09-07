#!/usr/bin/env python3
"""
セグメントデータWAL復旧スクリプト。

migration ae525721359b の batch_alter_table('transcripts') が
PRAGMA foreign_keys=ON のままトランザクション内で DROP TABLE を実行し、
segments が全件 CASCADE DELETE された事故からの復旧に使う。

仕組み:
- SQLite WAL はページ単位の append-only ログ
- WAL 内の「ページの最初の出現」= マイグレーション実行前の状態
- そのページ群で DB コピーを上書きすることで、マイグレーション前の
  スナップショット (recovery.db) を作る
- recovery.db から segments を SELECT して SQL INSERT として出力
"""
import struct
import shutil
import sqlite3
import os
import sys
from pathlib import Path

WAL = Path("/data/app.db-wal.bak")
DB  = Path("/data/app.db.bak")
RECOVERY_DB = Path("/tmp/recovery.db")
OUTPUT_SQL  = Path("/data/segments_recovered.sql")


# ── WAL パーサー ──────────────────────────────────────────────────────────────

def read_wal(path: Path):
    with open(path, "rb") as f:
        data = f.read()

    magic = struct.unpack(">I", data[:4])[0]
    if magic == 0x377f0682:
        e = ">"
    elif magic == 0x377f0683:
        e = "<"
    else:
        raise ValueError(f"WAL magic が不正: {hex(magic)}")

    page_size = struct.unpack(f"{e}I", data[8:12])[0]
    print(f"WAL: {len(data):,} bytes, page_size={page_size}")

    frame_size = 24 + page_size
    frames = []
    pos = 32
    while pos + frame_size <= len(data):
        fh = data[pos : pos + 24]
        pd = data[pos + 24 : pos + frame_size]
        page_no = struct.unpack(f"{e}I", fh[:4])[0]
        db_size = struct.unpack(f"{e}I", fh[4:8])[0]
        frames.append((page_no, db_size, pd))
        pos += frame_size

    print(f"フレーム総数: {len(frames)}")

    # トランザクション単位にグループ化（commit frame = db_size > 0）
    txs = []
    cur = []
    for page_no, db_size, pd in frames:
        cur.append((page_no, db_size, pd))
        if db_size > 0:
            txs.append(cur)
            cur = []

    print(f"トランザクション数: {len(txs)}")
    for i, tx in enumerate(txs):
        pages = sorted({pn for pn, _, _ in tx})
        print(f"  TX[{i}]: {len(tx)} frames, pages={pages[:10]}{'...' if len(pages)>10 else ''}")

    return page_size, frames, txs


# ── 復旧 DB 構築 ───────────────────────────────────────────────────────────────

def build_recovery_db(page_size: int, frames: list, txs: list) -> bool:
    """
    WAL の各ページにつき「最初の出現フレーム」だけを使って
    recovery.db を作る。最初の出現 = マイグレーション前 or マイグレーション中の
    削除前のバージョンに対応する可能性が高い。
    """
    # ページ番号 → 最初の出現インデックス
    first_frame: dict[int, bytes] = {}
    for page_no, _, pd in frames:
        if page_no not in first_frame:
            first_frame[page_no] = pd

    print(f"\nWAL 内ユニークページ数: {len(first_frame)}")
    print(f"ページ番号一覧: {sorted(first_frame.keys())[:20]}")

    # ベース DB をコピーして、WAL の最初のページで上書き
    shutil.copy2(DB, RECOVERY_DB)
    with open(RECOVERY_DB, "r+b") as f:
        for page_no, pd in first_frame.items():
            offset = (page_no - 1) * page_size
            f.seek(offset)
            f.write(pd)

    return True


# ── segments 取り出し & SQL 出力 ─────────────────────────────────────────────

def extract_segments() -> int:
    conn = sqlite3.connect(str(RECOVERY_DB))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA integrity_check")

    try:
        count = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    except Exception as e:
        print(f"segments テーブルの読み取りに失敗: {e}")
        conn.close()
        return 0

    print(f"\n[recovery.db] segments 件数: {count}")
    if count == 0:
        conn.close()
        return 0

    # サンプル表示
    sample = conn.execute(
        "SELECT transcript_id, speaker_label, text_content "
        "FROM segments ORDER BY transcript_id, order_index LIMIT 5"
    ).fetchall()
    print("サンプル:")
    for r in sample:
        print(f"  tid={r[0]} {r[1]}: {r[2][:60]}")

    # SQL ファイルに出力
    rows = conn.execute(
        "SELECT transcript_id, order_index, start_seconds, end_seconds, "
        "speaker_label, text_content, display_name, is_edited "
        "FROM segments ORDER BY transcript_id, order_index"
    ).fetchall()
    conn.close()

    with open(OUTPUT_SQL, "w", encoding="utf-8") as f:
        f.write("BEGIN;\n")
        for r in rows:
            tid, oidx, ss, es, sl, text, dn, ie = r
            text_esc = text.replace("'", "''") if text else ""
            dn_val   = f"'{dn.replace(chr(39), chr(39)*2)}'" if dn else "NULL"
            f.write(
                f"INSERT INTO segments "
                f"(transcript_id,order_index,start_seconds,end_seconds,"
                f"speaker_label,text_content,display_name,is_edited,"
                f"created_at,updated_at) VALUES "
                f"({tid},{oidx},{ss},{es},"
                f"'{sl}','{text_esc}',{dn_val},{int(ie)},"
                f"datetime('now'),datetime('now'));\n"
            )
        f.write("COMMIT;\n")

    print(f"\n→ {count} 件を {OUTPUT_SQL} に出力しました")
    return count


# ── speakers 取り出し & SQL 出力 ─────────────────────────────────────────────

def extract_speakers():
    conn = sqlite3.connect(str(RECOVERY_DB))
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        rows = conn.execute(
            "SELECT transcript_id, speaker_label, display_name, color "
            "FROM speakers"
        ).fetchall()
    except Exception as e:
        print(f"speakers 読み取り失敗: {e}")
        conn.close()
        return

    conn.close()
    if not rows:
        return

    spk_sql = Path("/data/speakers_recovered.sql")
    with open(spk_sql, "w", encoding="utf-8") as f:
        f.write("BEGIN;\n")
        for r in rows:
            tid, sl, dn, color = r
            dn_esc = dn.replace("'", "''") if dn else ""
            color_val = f"'{color}'" if color else "NULL"
            f.write(
                f"INSERT OR IGNORE INTO speakers "
                f"(transcript_id,speaker_label,display_name,color,"
                f"created_at,updated_at) VALUES "
                f"({tid},'{sl}','{dn_esc}',{color_val},"
                f"datetime('now'),datetime('now'));\n"
            )
        f.write("COMMIT;\n")
    print(f"speakers {len(rows)} 件 → {spk_sql}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== WAL セグメント復旧スクリプト ===\n")

    for p in [WAL, DB]:
        if not p.exists():
            print(f"ERROR: {p} が存在しません")
            print("先に以下でバックアップを作成してください:")
            print("  cp /data/app.db /data/app.db.bak")
            print("  cp /data/app.db-wal /data/app.db-wal.bak")
            print("  cp /data/app.db-shm /data/app.db-shm.bak")
            sys.exit(1)

    # WAL に SPEAKER_ データがあるか確認
    with open(WAL, "rb") as f:
        wal_bytes = f.read()
    spk_cnt = wal_bytes.count(b"SPEAKER_0")
    print(f"WAL 内 'SPEAKER_0' 出現数: {spk_cnt}")
    if spk_cnt == 0:
        print("セグメントデータが WAL に見つかりません。復旧不可能です。")
        print("音声ファイルから再アップロードしてください。")
        sys.exit(0)

    page_size, frames, txs = read_wal(WAL)
    build_recovery_db(page_size, frames, txs)

    count = extract_segments()
    if count > 0:
        extract_speakers()
        print("\n====== 復旧手順 ======")
        print("1. segments を復元:")
        print("   sqlite3 /data/app.db < /data/segments_recovered.sql")
        print("2. speakers を復元:")
        print("   sqlite3 /data/app.db < /data/speakers_recovered.sql")
        print("3. ブラウザでアプリを開き、文字起こし一覧を確認")
    else:
        print("\nWAL から segments を復元できませんでした。")
        print("音声ファイルから再アップロードしてください。")
        print(f"音声: ls /data/audio/")


if __name__ == "__main__":
    main()
