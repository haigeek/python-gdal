# -*- coding: utf-8 -*-
"""任务元数据存取：业务库（纯 Postgres，不要求 PostGIS，schema 默认 g2p）。

- 每条任务 = <schema>.task 一行，config / result / progress 为 jsonb，日志在 task_log；
- 每次操作独立短连接（autocommit），天然线程安全（Web 请求线程与 worker 线程共用）；
- 建表（幂等 DDL）由 web/db.py 在服务启动时执行 migrations.sql，本模块不负责建表。
"""

from __future__ import annotations

from typing import Optional

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..model import DatabaseConfig

# ---------------------------------------------------------------- 状态常量

STATUS_DRAFT = "draft"          # 已创建未运行
STATUS_QUEUED = "queued"        # 已入队待执行
STATUS_RUNNING = "running"      # 执行中
STATUS_SUCCEEDED = "succeeded"  # 成功
STATUS_FAILED = "failed"        # 失败
STATUS_CANCELLED = "cancelled"  # 被取消

ALL_STATUS = (STATUS_DRAFT, STATUS_QUEUED, STATUS_RUNNING,
              STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED)
TERMINAL = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED)
# 可编辑（重命名/改配置）的状态：运行相关的不可编辑
EDITABLE = (STATUS_DRAFT, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED)
# 可入队运行的状态
RUNNABLE = (STATUS_DRAFT,) + TERMINAL


class TaskStore:
    def __init__(self, db: DatabaseConfig, schema: str = "g2p"):
        self.db = db
        self.schema = schema

    # ------------------------------------------------------------ 连接

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.db.dsn(), autocommit=True, row_factory=dict_row)

    @staticmethod
    def _valid_schema(name: str) -> bool:
        import re
        return bool(re.fullmatch(r"[a-z_][a-z0-9_]*", name or ""))

    # ------------------------------------------------------------ CRUD

    def create(self, type_: str, name: str, config: dict) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("INSERT INTO {s}.task (type, name, config) "
                            "VALUES (%s, %s, %s) RETURNING id").format(
                        s=sql.Identifier(self.schema)),
                    (type_, name, Jsonb(config or {})),
                )
                return cur.fetchone()["id"]

    def get(self, task_id: int) -> Optional[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT id, type, name, status, config, progress, "
                            "result, error, created_at, updated_at, "
                            "started_at, finished_at "
                            "FROM {s}.task WHERE id = %s").format(
                        s=sql.Identifier(self.schema)),
                    (task_id,),
                )
                return cur.fetchone()

    def list(self, *, status: Optional[str] = None, type_: Optional[str] = None,
             q: Optional[str] = None, page: int = 1, page_size: int = 20,
             sort_by: str = "id", order: str = "desc",
             ) -> tuple[list[dict], int]:
        """分页列表。返回 (items, total)。sort_by 限白名单，order 限 asc/desc。"""
        where = []
        args: list = []
        if status:
            args.append(status)
            where.append(sql.SQL("status = %s"))
        if type_:
            args.append(type_)
            where.append(sql.SQL("type = %s"))
        if q:
            args.append(f"%{q}%")
            where.append(sql.SQL("name ILIKE %s"))
        cond = sql.SQL(" AND ").join(where) if where else sql.SQL("TRUE")
        if sort_by not in ("id", "created_at", "updated_at"):
            sort_by = "id"
        order = order if order in ("asc", "desc") else "desc"
        sort_sql = (sql.SQL("ORDER BY {} {}").format(sql.Identifier(sort_by), sql.SQL(order)))
        page = max(1, page)
        page_size = max(1, min(200, page_size))

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT count(*) AS n FROM {s}.task WHERE {c}").format(
                        s=sql.Identifier(self.schema), c=cond),
                    args,
                )
                total = cur.fetchone()["n"]
                cur.execute(
                    sql.SQL("SELECT id, type, name, status, config, progress, result, "
                            "error, created_at, updated_at, started_at, finished_at "
                            "FROM {s}.task WHERE {c} {srt} "
                            "LIMIT %s OFFSET %s").format(
                        s=sql.Identifier(self.schema), c=cond, srt=sort_sql),
                    args + [page_size, (page - 1) * page_size],
                )
                return cur.fetchall(), total

    def update(self, task_id: int, *, name: Optional[str] = None,
               config: Optional[dict] = None) -> None:
        """更新名称/配置（调用方需先校验状态可编辑）。"""
        sets = []
        args: list = []
        if name is not None:
            sets.append(sql.SQL("name = %s"))
            args.append(name)
        if config is not None:
            sets.append(sql.SQL("config = %s"))
            args.append(Jsonb(config))
        if not sets:
            return
        sets.append(sql.SQL("updated_at = now()"))
        args.append(task_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("UPDATE {s}.task SET {sets} WHERE id = %s").format(
                        s=sql.Identifier(self.schema),
                        sets=sql.SQL(", ").join(sets)),
                    args,
                )

    def delete(self, task_id: int) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DELETE FROM {s}.task WHERE id = %s").format(
                        s=sql.Identifier(self.schema)),
                    (task_id,),
                )

    # ------------------------------------------------------------ 状态流转

    # 哨兵：区分"未提供（不更新）"与"显式置 NULL"
    _UNSET = object()

    def update_state(self, task_id: int, *, status: object = _UNSET,
                     progress: object = _UNSET,
                     result: object = _UNSET,
                     error: object = _UNSET,
                     started_at: object = _UNSET,
                     finished_at: object = _UNSET) -> None:
        """更新运行态字段。提供即写入（error/进度传 None 表示清空）；
        started_at/finished_at 传 True 表示置 now()，传 None 表示清空。"""
        sets = [sql.SQL("updated_at = now()")]
        args: list = []
        if status is not self._UNSET:
            sets.append(sql.SQL("status = %s"))
            args.append(status)
        if progress is not self._UNSET:
            sets.append(sql.SQL("progress = %s"))
            args.append(Jsonb(progress) if progress is not None else None)
        if result is not self._UNSET:
            sets.append(sql.SQL("result = %s"))
            args.append(Jsonb(result) if result is not None else None)
        if error is not self._UNSET:
            sets.append(sql.SQL("error = %s"))
            args.append(error)
        if started_at is True:
            sets.append(sql.SQL("started_at = now()"))
        elif started_at is not self._UNSET:
            sets.append(sql.SQL("started_at = NULL"))
        if finished_at is True:
            sets.append(sql.SQL("finished_at = now()"))
        elif finished_at is not self._UNSET:
            sets.append(sql.SQL("finished_at = NULL"))
        args.append(task_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("UPDATE {s}.task SET {sets} WHERE id = %s").format(
                        s=sql.Identifier(self.schema),
                        sets=sql.SQL(", ").join(sets)),
                    args,
                )

    def orphan_recover(self, error_msg: str) -> int:
        """服务启动时调用：遗留 running/queued 标记为 failed。返回受影响行数。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("UPDATE {s}.task SET status = %s, error = %s, "
                            "finished_at = now(), updated_at = now() "
                            "WHERE status IN (%s, %s)").format(
                        s=sql.Identifier(self.schema)),
                    (STATUS_FAILED, error_msg, STATUS_RUNNING, STATUS_QUEUED),
                )
                return cur.rowcount

    # ------------------------------------------------------------ 日志

    def append_log(self, task_id: int, message: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("INSERT INTO {s}.task_log (task_id, message) "
                            "VALUES (%s, %s)").format(
                        s=sql.Identifier(self.schema)),
                    (task_id, message),
                )

    def logs(self, task_id: int, after_id: int = 0, limit: int = 500) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT id, ts, message FROM {s}.task_log "
                            "WHERE task_id = %s AND id > %s "
                            "ORDER BY id LIMIT %s").format(
                        s=sql.Identifier(self.schema)),
                    (task_id, after_id, limit),
                )
                return cur.fetchall()

    # ------------------------------------------------------------ 数据源

    def datasource_create(self, name: str, config: dict) -> int:
        """新建数据源（name 唯一，冲突由数据库 UNIQUE 约束抛错）。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("INSERT INTO {s}.datasource (name, config) "
                            "VALUES (%s, %s) RETURNING id").format(
                        s=sql.Identifier(self.schema)),
                    (name, Jsonb(config or {})),
                )
                return cur.fetchone()["id"]

    def datasource_get(self, ds_id: int) -> Optional[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT id, name, config, created_at, updated_at "
                            "FROM {s}.datasource WHERE id = %s").format(
                        s=sql.Identifier(self.schema)),
                    (ds_id,),
                )
                return cur.fetchone()

    def datasource_list(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT id, name, config, created_at, updated_at "
                            "FROM {s}.datasource ORDER BY name").format(
                        s=sql.Identifier(self.schema)),
                )
                return cur.fetchall()

    def datasource_update(self, ds_id: int, *,
                          name: Optional[str] = None,
                          config: Optional[dict] = None) -> bool:
        sets, args = [sql.SQL("updated_at = now()")], []
        if name is not None:
            sets.append(sql.SQL("name = %s"))
            args.append(name)
        if config is not None:
            sets.append(sql.SQL("config = %s"))
            args.append(Jsonb(config))
        if len(sets) == 1:
            return True
        args.append(ds_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("UPDATE {s}.datasource SET {sets} WHERE id = %s").format(
                        s=sql.Identifier(self.schema),
                        sets=sql.SQL(", ").join(sets)),
                    args,
                )
                return cur.rowcount == 1

    def datasource_delete(self, ds_id: int) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DELETE FROM {s}.datasource WHERE id = %s").format(
                        s=sql.Identifier(self.schema)),
                    (ds_id,),
                )
                return cur.rowcount == 1

    # ------------------------------------------------------------ 上传记录（MD5 去重 / TTL 清理）

    def upload_find(self, md5: str) -> Optional[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT id, md5, gdb_path, size_bytes, created_at, "
                            "last_used_at FROM {s}.upload WHERE md5 = %s").format(
                        s=sql.Identifier(self.schema)),
                    (md5,),
                )
                return cur.fetchone()

    def upload_record(self, md5: str, gdb_path: str, size_bytes: int) -> int:
        """记录一次上传；同 md5 重复时刷新 gdb_path 与 last_used_at（幂等）。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("INSERT INTO {s}.upload (md5, gdb_path, size_bytes) "
                            "VALUES (%s, %s, %s) "
                            "ON CONFLICT (md5) DO UPDATE SET "
                            "gdb_path = EXCLUDED.gdb_path, last_used_at = now() "
                            "RETURNING id").format(s=sql.Identifier(self.schema)),
                    (md5, gdb_path, size_bytes),
                )
                return cur.fetchone()["id"]

    def upload_touch(self, upload_id: int) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("UPDATE {s}.upload SET last_used_at = now() "
                            "WHERE id = %s").format(s=sql.Identifier(self.schema)),
                    (upload_id,),
                )

    def upload_delete(self, upload_id: int) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DELETE FROM {s}.upload WHERE id = %s").format(
                        s=sql.Identifier(self.schema)),
                    (upload_id,),
                )

    def upload_stale(self, before: object) -> list[dict]:
        """last_used_at 早于 before 的上传记录（清理候选）。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT id, md5, gdb_path, last_used_at FROM {s}.upload "
                            "WHERE last_used_at < %s ORDER BY last_used_at").format(
                        s=sql.Identifier(self.schema)),
                    (before,),
                )
                return cur.fetchall()

    def task_gdb_paths(self) -> set:
        """全部任务引用的 GDB/SHP 源路径（清理时保护上传文件）。

        方法名保留以兼容现有清理器和外部调用；SHP 路径一并纳入保护。
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT DISTINCT g FROM ("
                            "SELECT config->>'gdb' AS g FROM {s}.task "
                            "WHERE config ? 'gdb' AND config->>'gdb' IS NOT NULL "
                            "UNION ALL "
                            "SELECT config->>'shp' AS g FROM {s}.task "
                            "WHERE config ? 'shp' AND config->>'shp' IS NOT NULL"
                            ") AS sources WHERE g IS NOT NULL").format(
                        s=sql.Identifier(self.schema)),
                )
                return {r["g"] for r in cur.fetchall()}