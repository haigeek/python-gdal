# -*- coding: utf-8 -*-
"""业务库连接与启动建表（幂等执行 migrations.sql）。

业务库 = 任务元数据（纯 Postgres，不要求 PostGIS）。建表在服务启动时自动完成，
也可在脚本/测试中直接调用 ensure_meta_schema() 复用。
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg

from ..model import DatabaseConfig

_MIGRATIONS = Path(__file__).parent / "migrations.sql"
_VALID_SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")


def ensure_meta_schema(db: DatabaseConfig, schema: str) -> None:
    """幂等执行业务库 DDL。schema 名仅允许小写字母数字下划线（防注入）。"""
    if not _VALID_SCHEMA.fullmatch(schema or ""):
        raise ValueError(f"非法 schema 名: {schema!r}（仅允许小写字母数字下划线）")
    sql_text = _MIGRATIONS.read_text(encoding="utf-8").replace("__SCHEMA__", schema)
    stmts = [s.strip() for s in sql_text.split(";") if s.strip()]
    with psycopg.connect(db.dsn(), autocommit=True) as conn:
        for stmt in stmts:
            conn.execute(stmt)