"""
数据库在线备份模块 — 基于 sqlite3 Connection.backup() 的 WAL 安全备份。

选型说明：
  使用 sqlite3 的在线备份 API（Connection.backup()）而非 VACUUM INTO：
    - backup() 走 SQLite 分页级拷贝，源库可以是只读连接，且不长时间锁库，
      对 WAL 模式的生产库安全（备份内容包含已提交到 -wal 的数据）；
    - VACUUM INTO 在部分老版本 SQLite（< 3.27）不可用，且会在源库上
      隐式开启写事务语义；backup() 语义更简单、兼容面更广。

备份文件名：{label}_YYYYMMDD_HHMMSS.db（本地时间），先写同目录临时文件，
校验 integrity_check 通过后原子 rename（os.replace）。备份副本会被转为
普通回滚日志模式（journal_mode=DELETE），成为无 -wal/-shm 伴生文件、
可直接只读打开的自包含单文件。

保留策略：按文件名中的时间戳排序，仅保留同 label 最近 keep 份备份；
文件名不符合备份命名规则的文件一律跳过、不做任何处理。
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

# 备份文件名规则：{label}_YYYYMMDD_HHMMSS.db（label 允许含下划线，
# 以"最后一个 _ + 时间戳"为界切分）
_BACKUP_NAME_RE = re.compile(r"^(?P<label>.+)_(?P<ts>\d{8}_\d{6})\.db$")

_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"


def _now() -> datetime:
    """当前本地时间（独立小函数，便于测试注入固定时间）。"""
    return datetime.now()


def ensure_backup_dir(out_dir: str) -> Path:
    """确保备份目录存在（幂等，不存在则递归创建），返回其 Path。"""
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_backup_name(name: str, label: str) -> Optional[datetime]:
    """解析备份文件名中的时间戳；label 不匹配或时间戳非法时返回 None。"""
    match = _BACKUP_NAME_RE.match(name)
    if match is None or match.group("label") != label:
        return None
    try:
        return datetime.strptime(match.group("ts"), _TIMESTAMP_FMT)
    except ValueError:
        # 形如备份但时间戳非法（如 20201399_999999）→ 视为无关文件，跳过
        return None


def list_backups(out_dir: str, label: str = "db") -> list[tuple[Path, datetime]]:
    """列出 out_dir 下属于指定 label 的备份，按时间戳倒序（最新在前）。

    只统计文件名符合 {label}_YYYYMMDD_HHMMSS.db 且时间戳合法的普通文件；
    其余文件一律忽略。目录不存在时返回空列表。
    """
    out = Path(out_dir)
    if not out.is_dir():
        return []
    found: list[tuple[Path, datetime]] = []
    for entry in sorted(out.iterdir()):
        if not entry.is_file():
            continue
        ts = _parse_backup_name(entry.name, label)
        if ts is not None:
            found.append((entry, ts))
    found.sort(key=lambda item: item[1], reverse=True)
    return found


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读语义打开 SQLite 库（不做任何数据写操作）。

    优先用只读 URI（file:...?mode=ro，不产生写副作用）；若库是"已完整
    checkpoint 的 WAL 库"（-wal/-shm 文件不存在），只读连接无法初始化
    共享内存，则退化为 query_only 会话（同样禁止任何写操作；建连时
    SQLite 可能重建空的 -wal/-shm 伴生文件，属无害副作用）。
    """
    uri = "file:" + quote(str(db_path.resolve())) + "?mode=ro"
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        # 立即读一页，强制完成 WAL/shm 初始化，尽早暴露只读打开问题
        conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        return conn
    except sqlite3.OperationalError:
        if conn is not None:
            conn.close()
        logger.warning("mode=ro 打开失败（可能为无 -wal 文件的 WAL 库），"
                       "改用 query_only 只读会话: %s", db_path)
    fallback = sqlite3.connect(str(db_path))
    fallback.execute("PRAGMA query_only=ON")
    return fallback


def _integrity_check(db_path: Path) -> str:
    """对新备份文件跑 PRAGMA integrity_check，返回检查结果字符串。"""
    conn = _open_readonly(db_path)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _remove_sqlite_sidecars(db_path: Path) -> None:
    """防御性清理 SQLite 伴生文件（-wal/-shm/-journal），存在才删。"""
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(db_path) + suffix)
        try:
            if sidecar.exists():
                sidecar.unlink()
        except OSError as exc:
            logger.warning("清理伴生文件失败（跳过）: %s (%s)", sidecar, exc)


def backup_database(db_path: str, out_dir: str, keep: int = 7, label: str = "db") -> Path:
    """对 SQLite 数据库做在线备份，返回新备份文件的 Path。

    流程：
      1. 只读方式打开源库，用 Connection.backup() 拷贝到 out_dir 下的临时文件；
      2. 对临时文件跑 PRAGMA integrity_check，非 "ok" 则删除并抛 RuntimeError；
      3. 原子 rename 为 {label}_YYYYMMDD_HHMMSS.db；
      4. 清理 out_dir 下同 label 的旧备份，仅保留最近 keep 份
         （按文件名时间戳排序；非法文件名跳过不动）。

    异常安全：备份中途出错时删除残留的临时文件，再向上抛出原始异常。
    """
    src = Path(db_path)
    if not src.is_file():
        raise FileNotFoundError(f"源数据库不存在: {src}")

    dest_dir = ensure_backup_dir(out_dir)
    stamp = _now().strftime(_TIMESTAMP_FMT)
    final_path = dest_dir / f"{label}_{stamp}.db"
    tmp_path = dest_dir / f"{label}_{stamp}.db.tmp"

    src_conn = _open_readonly(src)
    try:
        dst_conn = sqlite3.connect(str(tmp_path))
        try:
            src_conn.backup(dst_conn)
            # backup() 会把源库的 WAL 头一并拷入副本；将副本转为普通
            # 回滚日志模式，使其成为自包含单文件（无 -wal/-shm 伴生文件，
            # 任何 sqlite 工具都能直接只读打开）。应用恢复该备份后，
            # db/connection.py 建连时会重新把 journal_mode 设回 WAL。
            dst_conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst_conn.close()
            _remove_sqlite_sidecars(tmp_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        logger.error("备份失败（已清理临时文件）: %s -> %s", src, tmp_path)
        raise
    finally:
        src_conn.close()

    check = _integrity_check(tmp_path)
    if check != "ok":
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"备份文件完整性校验失败（integrity_check={check!r}）: {tmp_path}")

    # 同目录原子 rename，保证读方永远看到完整文件
    os.replace(str(tmp_path), str(final_path))
    logger.info("数据库备份完成: %s (%.1f MiB)", final_path,
                final_path.stat().st_size / (1024 * 1024))

    _prune_old_backups(dest_dir, label, keep)
    return final_path


def _prune_old_backups(dest_dir: Path, label: str, keep: int) -> None:
    """保留策略：同 label 的备份仅保留最近 keep 份，多余的删除。"""
    if keep < 0:
        keep = 0
    backups = list_backups(str(dest_dir), label=label)
    for path, _ts in backups[keep:]:
        try:
            path.unlink()
            logger.info("清理旧备份: %s", path)
        except OSError as exc:
            logger.warning("清理旧备份失败（跳过）: %s (%s)", path, exc)
