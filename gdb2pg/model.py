# -*- coding: utf-8 -*-
"""配置与数据模型（dataclasses）。

这些类直接对应 JSON 配置文件的字段，是未来可视化 GUI 的绑定契约：
GUI 只需读写 JSON / 构造这些对象，然后调用 importer.run(config)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------- 连接配置

@dataclass
class DatabaseConfig:
    """PostgreSQL/PostGIS 连接配置。

    密码可明文（password）或从环境变量取（password_env，优先级更高）。
    """

    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "postgres"
    user: str = "postgres"
    password: Optional[str] = None
    password_env: Optional[str] = None  # 环境变量名，如 GDB2PG_PGPASSWORD
    schema: str = "public"
    ssl: str = "prefer"
    ensure_postgis: bool = False  # 若 true，导入前执行 CREATE EXTENSION IF NOT EXISTS postgis

    def resolved_password(self) -> Optional[str]:
        if self.password_env:
            val = os.environ.get(self.password_env)
            if val:
                return val
        return self.password

    def dsn(self) -> str:
        pwd = self.resolved_password()
        kv = [
            f"host={self.host}",
            f"port={self.port}",
            f"dbname={self.dbname}",
            f"user={self.user}",
            f"sslmode={self.ssl}",
        ]
        if pwd:
            kv.insert(1, f"password={pwd}")
        return " ".join(kv)

    def redacted(self) -> dict:
        d = asdict(self)
        if d.get("password"):
            d["password"] = "***"
        return d


# ---------------------------------------------------------------- 图层规则

@dataclass
class LayerRule:
    """单个图层的导入规则；缺失字段表示继承 default。"""

    source: str                # GDB 内图层名（支持 * ? 通配，可用逗号分隔多个）
    table: Optional[str] = None  # 目标表名；缺省 = 源名（保留原名）
    srid: Optional[int] = None   # 强制 SRID；缺省继承 default.srid
    mode: Optional[str] = None   # create | overwrite | append；缺省继承 default.mode
    geom_column: str = "geom"
    columns: dict = field(default_factory=dict)  # 源列名 -> 目标列名（rename）


@dataclass
class Defaults:
    srid: Optional[int] = None        # 无 SRS 图层的兜底 SRID（必填或逐层指定）
    mode: str = "create"              # create | overwrite | append
    geometries: bool = True           # 是否导入几何
    create_spatial_index: bool = True
    launder_columns: bool = False     # 列名转小写下划线（默认保真）
    on_error: str = "abort"           # abort | skip


@dataclass
class ImportConfig:
    gdb: str
    database: DatabaseConfig
    default: Defaults = field(default_factory=Defaults)
    selectors: dict = field(default_factory=lambda: {"include": ["*"], "exclude": []})
    layers: list = field(default_factory=list)  # list[LayerRule]

    # ------------------------------------------------ 便捷解析

    def layer_rules(self) -> list[LayerRule]:
        """返回显式规则列表（source 含逗号时展开成多条）。"""
        rules: list[LayerRule] = []
        for r in self.layers:
            if not isinstance(r, LayerRule):
                r = LayerRule(**r)
            for src in [s.strip() for s in r.source.split(",") if s.strip()]:
                rr = LayerRule(
                    source=src, table=r.table, srid=r.srid, mode=r.mode,
                    geom_column=r.geom_column, columns=dict(r.columns),
                )
                rules.append(rr)
        return rules

    @staticmethod
    def from_dict(d: dict) -> "ImportConfig":
        d = dict(d)
        d["database"] = DatabaseConfig(**d["database"])
        d["default"] = Defaults(**d.get("default", {}))
        d["layers"] = [LayerRule(**x) for x in d.get("layers", [])]
        return ImportConfig(**d)

    @staticmethod
    def from_json(path: str) -> "ImportConfig":
        import json

        with open(path, "r", encoding="utf-8") as f:
            return ImportConfig.from_dict(json.load(f))


# ---------------------------------------------------------------- 导入计划

@dataclass
class LayerPlan:
    """单图层导入前计算好的计划（dry-run 输出项，也是执行时的蓝图）。"""

    source: str                 # GDB 图层名
    table: str                  # 目标表名（已规范化）
    schema: str                 # 目标 schema
    geom_column: str            # 几何列名
    geometry_pg: Optional[str]  # 如 MULTILINESTRING / GENERIC / None(无几何)
    srid: Optional[int]
    mode: str                   # create | overwrite | append
    feature_count: int
    columns: list                # [(源列名, 目标列名, pg类型, ogr类型)]
    issues: list                 # 警告列表
    errors: list                 # 致命错误（不通则跳过该层）