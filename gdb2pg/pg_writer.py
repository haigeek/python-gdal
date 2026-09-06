# -*- coding: utf-8 -*-
"""PostGIS 写入层：psycopg 直连，建表 + COPY 批量写入 + GIST 索引 + 对账。

- 所有标识符经 psycopg.sql.Identifier 引用（天然防注入、支持中文名）；
- 几何列以 hex EWKB 文本形式走 COPY（PG 原生解析，SRID 内嵌于头部）；
- 每个图层一个事务：失败整体回滚（create/overwrite 模式下表也不残留）。
"""

from __future__ import annotations

from typing import Callable, Iterator, Optional

import psycopg
from psycopg import sql

from .model import DatabaseConfig, LayerPlan


class PgWriter:
    def __init__(self, cfg: DatabaseConfig):
        self.cfg = cfg
        self.conn: Optional[psycopg.Connection] = None

    # ------------------------------------------------------------ 生命周期

    def connect(self) -> "PgWriter":
        self.conn = psycopg.connect(self.cfg.dsn(), autocommit=True)
        return self

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 基础设施

    def postgis_version(self) -> Optional[str]:
        with self.conn.cursor() as cur:
            try:
                cur.execute("SELECT PostGIS_Version()")
                return cur.fetchone()[0]
            except psycopg.Error as e:
                # 非 psycopg 错误码时也吞掉，交给调用方判断
                if getattr(e, "sqlstate", None) == "42883":  # function does not exist
                    return None
                raise

    def ensure_postgis(self):
        if self.postgis_version() is not None:
            return
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    def ensure_schema(self, schema: str):
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(schema)))

    def table_exists(self, schema: str, table: str) -> bool:
        with self.conn.cursor() as cur:
            # 必须带双引号，否则 PG 会把大写/驼峰表名折叠成小写去查
            cur.execute(
                "SELECT to_regclass(%s)",
                (f'"{schema}"."{table}"',),
            )
            return cur.fetchone()[0] is not None

    # ------------------------------------------------------------ DDL

    @staticmethod
    def _geom_ddl(geom_column: str, geom_pg: str, srid: Optional[int]) -> Optional[sql.Composed]:
        if geom_pg is None:
            return None
        type_name = "Geometry" if geom_pg == "GENERIC" else geom_pg
        if srid:
            pgtype = f"{type_name},{srid}"
        else:
            pgtype = type_name
        return sql.SQL("{col} geometry({type})").format(
            col=sql.Identifier(geom_column), type=sql.SQL(pgtype))

    def create_table(self, plan: LayerPlan):
        schema, table = plan.schema, plan.table
        col_defs = [
            sql.SQL("{} {}").format(sql.Identifier(dst), sql.SQL(pg))
            for _, dst, pg in plan.columns
        ]
        geom_ddl = self._geom_ddl(plan.geom_column, plan.geometry_pg, plan.srid)
        if geom_ddl is not None:
            col_defs.append(geom_ddl)
        # 主键列：fid_pk=True（默认，GDB 原生主键）建 plan.pk_column
        #（默认 OBJECTID）integer 主键，COPY 时写入 GDB FID；否则回退自增
        # bigserial（列名 fid）。列名经 Identifier 加引号，保留原大小写，
        # 与 COPY 列名一致（未加引号的 OBJECTID 会被 PG 折叠成小写）。
        if plan.fid_pk:
            pk_ddl = sql.SQL("{} integer PRIMARY KEY").format(
                sql.Identifier(plan.pk_column))
        else:
            pk_ddl = sql.SQL("fid bigserial PRIMARY KEY")
        query = sql.SQL("CREATE TABLE {schema}.{table} ({pk}, {cols})").format(
            schema=sql.Identifier(schema),
            table=sql.Identifier(table),
            pk=pk_ddl,
            cols=sql.SQL(", ").join(col_defs),
        )
        with self.conn.cursor() as cur:
            cur.execute(query)

    def drop_table(self, schema: str, table: str):
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {schema}.{table}").format(
                schema=sql.Identifier(schema), table=sql.Identifier(table)))

    def create_spatial_index(self, plan: LayerPlan):
        idx = f"{plan.table}_geom_idx"
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {idx} ON {schema}.{table} USING GIST ({geom})")
                .format(
                    idx=sql.Identifier(idx),
                    schema=sql.Identifier(plan.schema),
                    table=sql.Identifier(plan.table),
                    geom=sql.Identifier(plan.geom_column),
                )
            )

    # ------------------------------------------------------------ 数据写入

    def copy_features(
        self,
        plan: LayerPlan,
        features: Iterator[tuple[dict, Optional[bytes]]],
        progress: Optional[Callable[[int], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
        commit_every: int = 0,
    ) -> int:
        """COPY 写入一个图层。返回写入行数。

        features: 逐条产出 (属性dict[源字段名], EWKB bytes|None, FID int)。
        progress(n): 每 5000 条与结束时回调已写行数；
        cancel(): 每 5000 条检查，返回 True 时抛 ImportCancelled
                  （调用方负责事务回滚与状态标记）；
        commit_every>0 时每 N 条 COMMIT 一次（大表降内存占用；默认单事务）。
        """
        # 目标列（按计划顺序）：(可选主键列) + 字段列 + (可选) 几何列
        has_geom = plan.geometry_pg is not None
        col_idents = []
        if plan.fid_pk:
            col_idents.append(sql.Identifier(plan.pk_column))
        col_idents += [sql.Identifier(dst) for _, dst, _ in plan.columns]
        if has_geom:
            col_idents.append(sql.Identifier(plan.geom_column))
        src_names = [src for src, _, _ in plan.columns]

        copy_sql = sql.SQL("COPY {schema}.{table} ({cols}) FROM STDIN").format(
            schema=sql.Identifier(plan.schema),
            table=sql.Identifier(plan.table),
            cols=sql.SQL(", ").join(col_idents),
        )

        # EWKB hex -> PG geometry 文本输入；fid_pk=True 时首列写 GDB FID
        def fmt_row(fid: int, attrs: dict, ewkb: Optional[bytes]):
            row = [fid] if plan.fid_pk else []
            row += [attrs.get(name) for name in src_names]
            if has_geom:
                row.append(ewkb.hex() if ewkb else None)
            return row

        n = 0
        # 单事务模式：外层事务已在 importer 开启；这里直接 COPY
        with self.conn.cursor() as cur:
            with cur.copy(copy_sql) as copy:
                for attrs, ewkb, fid in features:
                    if cancel is not None and n % 5000 == 0 and n > 0 and cancel():
                        from .importer import ImportCancelled
                        raise ImportCancelled("用户取消导入")
                    copy.write_row(fmt_row(fid, attrs, ewkb))
                    n += 1
                    if progress is not None and n % 5000 == 0:
                        progress(n)
        if progress is not None:
            progress(n)
        return n

    def count_rows(self, schema: str, table: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT count(*) FROM {schema}.{table}").format(
                schema=sql.Identifier(schema), table=sql.Identifier(table)))
            return cur.fetchone()[0]