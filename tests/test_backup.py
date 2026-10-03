"""数据库备份模块（db/backup.py）测试。

全部使用 tmp_path 构造临时源库，不触碰真实 db/ct_monitor.db。
覆盖：备份生成与内容可查询（含 WAL 未 checkpoint 数据）、文件名格式、
integrity_check、keep 保留策略、list_backups 排序、label 隔离、
out_dir 自动创建，以及源库缺失 / 损坏 / 校验失败等异常路径。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from db import backup as backup_mod
from db.backup import backup_database, ensure_backup_dir, list_backups

_NAME_RE = re.compile(r"^db_\d{8}_\d{6}\.db$")


def _make_wal_source(db_file: Path, rows: int = 5) -> sqlite3.Connection:
    """在 WAL 模式下建源库并写入数据；返回仍打开的写连接。

    刻意不 close / 不 checkpoint：数据只提交到 -wal，备份必须能读到，
    以证明 backup() 走的是 WAL 安全的在线备份路径。
    """
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO demo(name) VALUES (?)",
        [(f"row-{i}",) for i in range(rows)],
    )
    conn.commit()
    # journal_mode 已是 WAL 且连接保持打开 → 数据留在 -wal 中
    return conn


def _count_rows(db_file: Path) -> int:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT count(*) FROM demo").fetchone()[0]
    finally:
        conn.close()


def _select_names(db_file: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM demo ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def wal_source(tmp_path):
    """WAL 模式源库（写连接保持打开，数据留在 -wal），teardown 时关闭。"""
    db_file = tmp_path / "src.db"
    conn = _make_wal_source(db_file)
    yield db_file
    conn.close()


@pytest.fixture
def fake_clock(monkeypatch):
    """把 backup._now 换成逐次 +60 秒的假时钟，保证文件名时间戳递增。"""
    state = {"tick": 0}

    def fake_now() -> datetime:
        state["tick"] += 1
        return datetime(2026, 1, 1) + timedelta(minutes=state["tick"])

    monkeypatch.setattr(backup_mod, "_now", fake_now)
    return state


# ── 基本功能 ────────────────────────────────────────────────────────────────

class TestBackupBasic:
    def test_backup_creates_queryable_file(self, wal_source, tmp_path):
        """备份生成、文件名格式正确、integrity 通过、内容可 SELECT。"""
        out_dir = tmp_path / "backups"
        result = backup_database(str(wal_source), str(out_dir))

        assert result.is_file()
        assert result.parent == out_dir
        assert _NAME_RE.match(result.name), f"文件名不符合备份命名规则: {result.name}"

        # 内容可查询：WAL 中未 checkpoint 的 5 行必须都在备份里
        assert _count_rows(result) == 5
        names = _select_names(result)
        assert names[0] == "row-0" and names[-1] == "row-4"

        # integrity_check 通过
        assert backup_mod._integrity_check(result) == "ok"

        # 备份是自包含单文件：无 -wal/-shm 伴生文件，只读方式可直接打开
        conn = sqlite3.connect(f"file:{result}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
        for suffix in ("-wal", "-shm", "-journal"):
            assert not Path(str(result) + suffix).exists()

        # 无残留临时文件
        assert list(out_dir.glob("*.tmp")) == []

    def test_backup_out_dir_auto_created(self, wal_source, tmp_path):
        """out_dir 不存在时自动递归创建。"""
        out_dir = tmp_path / "nested" / "deep" / "backups"
        assert not out_dir.exists()
        result = backup_database(str(wal_source), str(out_dir))
        assert out_dir.is_dir()
        assert result.is_file()

    def test_ensure_backup_dir_idempotent(self, tmp_path):
        """ensure_backup_dir 幂等，重复调用不报错。"""
        out_dir = tmp_path / "x" / "y"
        first = ensure_backup_dir(str(out_dir))
        assert first == out_dir and out_dir.is_dir()
        assert ensure_backup_dir(str(out_dir)) == out_dir

    def test_custom_label_in_filename(self, wal_source, tmp_path):
        """label 反映在文件名中。"""
        result = backup_database(str(wal_source), str(tmp_path / "out"), label="nightly")
        assert re.match(r"^nightly_\d{8}_\d{6}\.db$", result.name)

    def test_backup_empty_source_ok(self, tmp_path):
        """空库（无任何表）也能备份且 integrity ok。"""
        src = tmp_path / "empty.db"
        conn = sqlite3.connect(str(src))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        result = backup_database(str(src), str(tmp_path / "out"))
        assert result.is_file()
        assert backup_mod._integrity_check(result) == "ok"


# ── 保留策略（keep）────────────────────────────────────────────────────────

class TestRetention:
    def test_sequential_backups_keep_prunes_oldest(
        self, wal_source, tmp_path, fake_clock
    ):
        """连续备份 4 次、keep=2：只留最近 2 份，最新保留、最旧被清理。"""
        out_dir = tmp_path / "backups"
        results = [
            backup_database(str(wal_source), str(out_dir), keep=2)
            for _ in range(4)
        ]
        backups = list_backups(str(out_dir))
        assert len(backups) == 2
        names = {p.name for p, _ in backups}
        assert results[-1].name in names   # 最新保留
        assert results[0].name not in names  # 最旧被清理
        assert results[1].name not in names
        # 最新一份内容仍然可查询
        assert _count_rows(results[-1]) == 5

    def test_keep_one_leaves_only_newest(self, wal_source, tmp_path, fake_clock):
        out_dir = tmp_path / "backups"
        first = backup_database(str(wal_source), str(out_dir), keep=1)
        second = backup_database(str(wal_source), str(out_dir), keep=1)
        backups = list_backups(str(out_dir))
        assert [p.name for p, _ in backups] == [second.name]
        assert not first.exists()
        assert second.exists()

    def test_illegal_names_never_touched(self, wal_source, tmp_path, fake_clock):
        """非法文件名（含形似备份但时间戳非法的）一律跳过不动。"""
        out_dir = tmp_path / "backups"
        out_dir.mkdir()
        strangers = [
            out_dir / "notes.txt",
            out_dir / "db_not_a_timestamp.db",
            out_dir / "db_20201399_999999.db",  # 形似备份但月份/时分秒非法
            out_dir / "db_20200101_0000.db",    # 时间戳不完整
            out_dir / "other_20260101_010101.db",  # 其他 label 的备份
        ]
        for f in strangers:
            f.write_bytes(b"placeholder")

        backup_database(str(wal_source), str(out_dir), keep=1)
        for f in strangers:
            assert f.exists(), f"无关文件被误删: {f}"


# ── list_backups ────────────────────────────────────────────────────────────

class TestListBackups:
    def test_sorted_desc_and_filters_foreign_files(self, tmp_path):
        out_dir = tmp_path / "backups"
        out_dir.mkdir()
        valid = [
            ("db_20260101_120000.db", datetime(2026, 1, 1, 12, 0, 0)),
            ("db_20250704_090000.db", datetime(2025, 7, 4, 9, 0, 0)),
            ("db_20260301_000000.db", datetime(2026, 3, 1, 0, 0, 0)),
        ]
        for name, _ in valid:
            (out_dir / name).write_bytes(b"x")
        # 应被忽略的文件
        (out_dir / "other_20260101_120000.db").write_bytes(b"x")  # 其他 label
        (out_dir / "garbage.txt").write_bytes(b"x")               # 非备份名
        (out_dir / "db_20201399_999999.db").write_bytes(b"x")     # 时间戳非法
        (out_dir / "subdir").mkdir()                              # 子目录

        result = list_backups(str(out_dir))
        assert [(p.name, ts) for p, ts in result] == sorted(
            valid, key=lambda item: item[1], reverse=True
        )
        for path, ts in result:
            assert isinstance(path, Path) and isinstance(ts, datetime)

    def test_list_backups_missing_dir_returns_empty(self, tmp_path):
        assert list_backups(str(tmp_path / "nope")) == []

    def test_label_isolation(self, wal_source, tmp_path, fake_clock):
        """两个 label 互不清理对方的备份文件。"""
        out_dir = tmp_path / "backups"
        # label=beta 先放 3 份（超出随后 alpha 的 keep）
        beta_files = [
            backup_database(str(wal_source), str(out_dir), keep=7, label="beta")
            for _ in range(3)
        ]
        # label=alpha 连续备份 3 次，keep=2
        for _ in range(3):
            backup_database(str(wal_source), str(out_dir), keep=2, label="alpha")

        alpha = list_backups(str(out_dir), label="alpha")
        beta = list_backups(str(out_dir), label="beta")
        assert len(alpha) == 2  # alpha 自己的 keep 生效
        assert len(beta) == 3   # beta 完全未受影响
        assert all(p.exists() for p in beta_files)


# ── 异常路径 ────────────────────────────────────────────────────────────────

class TestFailurePaths:
    def test_missing_source_raises_file_not_found(self, tmp_path):
        out_dir = tmp_path / "out"
        with pytest.raises(FileNotFoundError):
            backup_database(str(tmp_path / "no_such.db"), str(out_dir))
        assert not out_dir.exists()  # 提前失败，不应创建输出目录

    def test_corrupt_source_db(self, tmp_path):
        """损坏源库：sqlite3.DatabaseError，且不留临时/半成品文件。"""
        src = tmp_path / "corrupt.db"
        src.write_bytes(b"this is definitely not a sqlite database" * 64)
        out_dir = tmp_path / "out"
        with pytest.raises(sqlite3.DatabaseError):
            backup_database(str(src), str(out_dir))
        assert list(out_dir.glob("*.tmp")) == []
        assert list(out_dir.glob("*.db")) == []

    def test_integrity_check_failure_deletes_file(
        self, wal_source, tmp_path, monkeypatch
    ):
        """integrity_check 非 ok：删除坏文件并抛 RuntimeError，不落地成品。"""
        monkeypatch.setattr(
            backup_mod, "_integrity_check", lambda _p: "corrupt page detected"
        )
        out_dir = tmp_path / "out"
        with pytest.raises(RuntimeError, match="integrity_check"):
            backup_database(str(wal_source), str(out_dir))
        assert list(out_dir.glob("*")) == []  # 临时文件与成品都不存在


# ── 真实库保护 ──────────────────────────────────────────────────────────────

def test_tests_never_reference_production_db(tmp_path):
    """防呆：本模块测试的源库均为 tmp_path 下的临时库，绝不指向
    db/ct_monitor.db；顺带验证 backup_database 接受 Path 与 str 两种入参
    （真实库的备份演练由主流程负责）。
    """
    src = tmp_path / "src.db"
    conn = _make_wal_source(src, rows=1)
    try:
        out_dir = tmp_path / "out"
        as_path = backup_database(src, out_dir)  # type: ignore[arg-type]
        assert as_path.is_file() and _count_rows(as_path) == 1
        as_str = backup_database(str(src), str(out_dir))
        assert as_str.is_file() and _count_rows(as_str) == 1
    finally:
        conn.close()
