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

    source: str                # GDB/SHP 图层名（支持 * ? 通配，可用逗号分隔多个）
    table: Optional[str] = None  # 目标表名；缺省 = 源名（保留原名）
    srid: Optional[int] = None   # 强制 SRID；缺省继承 default.srid
    mode: Optional[str] = None   # create | overwrite | append；缺省继承 default.mode
    geom_column: Optional[str] = None  # 几何列名；缺省继承 default.geom_column
    pk_field: Optional[str] = None  # SHP 源字段主键；缺省继承 default.pk_field
    columns: dict = field(default_factory=dict)  # 源列名 -> 目标列名（rename）


@dataclass
class Defaults:
    srid: Optional[int] = None        # 无 SRS 图层的兜底 SRID（必填或逐层指定）
    mode: str = "create"              # create | overwrite | append
    geometries: bool = True           # 是否导入几何
    create_spatial_index: bool = True
    launder_columns: bool = True      # 列名转小写下划线（默认开启）
    launder_tables: bool = True       # 表名转小写下划线（默认开启）
    on_error: str = "abort"           # abort | skip
    fid_pk: bool = True               # GDB 使用原生 FID（OBJECTID）；SHP 仅兼容旧配置；
                                      # False 时回退自增 bigserial
    pk_column: str = "objectid"       # 原生 FID 主键列名（默认 objectid）
    pk_field: Optional[str] = None     # SHP 默认源字段主键；缺省使用目标自增键
    geom_column: str = "shape"        # 几何列名缺省（可被图层规则覆盖）


@dataclass
class ImportConfig:
    # 保留 gdb 字段作为现有配置契约；SHP 任务使用 shp 字段。
    # gdb 给默认空值，便于同一套计划/导入编排解析两种数据源。
    gdb: str = ""
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    default: Defaults = field(default_factory=Defaults)
    selectors: dict = field(default_factory=lambda: {"include": ["*"], "exclude": []})
    layers: list = field(default_factory=list)  # list[LayerRule]
    shp: Optional[str] = None                  # .shp 文件或包含 shp 的目录

    def source_path(self) -> str:
        """返回当前配置的数据源路径；SHP 配置优先使用 shp 字段。"""
        return self.shp or self.gdb

    def source_kind(self) -> str:
        """返回数据源类型（gdb 或 shp）。"""
        return "shp" if self.shp else "gdb"

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
                    geom_column=r.geom_column, pk_field=r.pk_field,
                    columns=dict(r.columns),
                )
                rules.append(rr)
        return rules

    @staticmethod
    def from_dict(d: dict) -> "ImportConfig":
        """从 dict 构造。构造时过滤未知键（如任务表单带入的 datasource_id
        引用字段），而不是把未知参数传给 dataclass 构造器。"""
        d = dict(d)

        def known(cls, fields: dict) -> dict:
            return {k: v for k, v in fields.items() if k in cls.__dataclass_fields__}

        d["database"] = DatabaseConfig(**known(DatabaseConfig, d.get("database") or {}))
        default_values = dict(d.get("default") or {})
        if d.get("shp"):
            if "fid_pk" not in default_values:
                # SHP 默认不把记录序号当作业务主键，改用目标表自增键。
                default_values["fid_pk"] = False
            if "launder_columns" not in default_values:
                # DBF 字段默认按源名称保留；用户仍可显式打开清洗。
                default_values["launder_columns"] = False
        d["default"] = Defaults(**known(Defaults, default_values))
        d["layers"] = [LayerRule(**known(LayerRule, x)) for x in d.get("layers", [])]
        return ImportConfig(**known(ImportConfig, d))

    @staticmethod
    def from_json(path: str) -> "ImportConfig":
        import json

        with open(path, "r", encoding="utf-8") as f:
            return ImportConfig.from_dict(json.load(f))


# ---------------------------------------------------------------- 导入计划

@dataclass
class LayerPlan:
    """单图层导入前计算好的计划（dry-run 输出项，也是执行时的蓝图）。"""

    source: str                 # GDB/SHP 图层名
    table: str                  # 目标表名（已规范化）
    schema: str                 # 目标 schema
    geom_column: str            # 几何列名
    geometry_pg: Optional[str]  # 如 MULTILINESTRING / GENERIC / None(无几何)
    srid: Optional[int]
    mode: str                   # create | overwrite | append
    feature_count: int
    columns: list                # [(源列名, 目标列名, pg类型)]
    issues: list                 # 警告列表
    errors: list                 # 致命错误（不通则跳过该层）
    fid_pk: bool = True          # 兼容 GDB/旧配置：目标表主键用源原生 FID
    pk_column: str = "objectid"  # 原生 FID 或自增主键的目标列名
    pk_source: Optional[str] = None  # fid | field | auto
    pk_field: Optional[str] = None   # SHP 作为主键的源字段名
