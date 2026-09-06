# -*- coding: utf-8 -*-
"""Schema 映射：OGR 图层 -> PostgreSQL DDL 计划。

纯函数，可单测；产出的是"计划"（字符串/列表），真正的 SQL 生成在 pg_writer
（用 psycopg.sql.Identifier 安全引用）。名称返回【原始名】，由调用方引用。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Optional

from osgeo import ogr

from .gdb_reader import pg_geom_type

PG_MAX_IDENT_BYTES = 63

# OGR 字段类型码 -> PG 类型（宽字符串并入 String 处理）
_OGR_TO_PG = {
    ogr.OFTInteger: "integer",
    ogr.OFTInteger64: "bigint",
    ogr.OFTReal: "double precision",
    ogr.OFTString: None,  # 按宽度决定 varchar(n) / text
    ogr.OFTWideString: None,
    ogr.OFTDate: "date",
    ogr.OFTTime: "time",
    ogr.OFTDateTime: "timestamptz",
    ogr.OFTBinary: "bytea",
}

# 需要告警的非常见类型
_UNSUPPORTED_FIELD_TYPES = {
    ogr.OFTIntegerList: "IntegerList",
    ogr.OFTRealList: "RealList",
    ogr.OFTStringList: "StringList",
    ogr.OFTWideStringList: "WideStringList",
    ogr.OFTInteger64List: "Integer64List",
}


def launder_name(name: str) -> str:
    """列名清洗：小写、非字母数字（含中文等 Unicode 字母数字保留）转下划线。"""
    s = unicodedata.normalize("NFKC", name).strip().lower()
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "col"


def normalize_table_name(name: str, max_bytes: int = PG_MAX_IDENT_BYTES,
                         launder: bool = False) -> str:
    """表名规范化：默认保留原名（调用方加引号）；launder=True 时转小写、
    特殊字符转下划线（同列名清洗）；超长则截断加短哈希防碰撞。"""
    if launder:
        name = launder_name(name)
    raw = name.encode("utf-8")
    if len(raw) <= max_bytes:
        return name
    # 保留前若干字节（避免截断多字节字符），尾部拼短哈希
    prefix = raw[: max_bytes - 11].decode("utf-8", errors="ignore")
    suffix = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{suffix}"


def pg_type_for_field(ftype: int, width: int) -> tuple[str, Optional[str]]:
    """OGR 字段类型码 -> (PG 类型, 警告)。"""
    if ftype in (ogr.OFTString, ogr.OFTWideString):
        if 0 < width <= 8192:
            return f"varchar({width})", None
        return "text", None
    if ftype in _OGR_TO_PG:
        return _OGR_TO_PG[ftype], None
    if ftype in _UNSUPPORTED_FIELD_TYPES:
        return "text", f"字段类型 {_UNSUPPORTED_FIELD_TYPES[ftype]} 不支持，降级为 text"
    return "text", f"未知字段类型码 {ftype}，降级为 text"


def column_defs(meta_fields: list[dict], rule, launder: bool) -> tuple[list[tuple], list[str]]:
    """字段 -> [(源列名, 目标列名, PG类型)] + 警告。

    rule.columns: {源列名: 目标列名} 重命名映射。
    """
    cols: list[tuple] = []
    warns: list[str] = []
    assigned = set()
    for f in meta_fields:
        src = f["name"]
        dst = rule.columns.get(src, src)
        if launder:
            dst = launder_name(dst)
        pg, warn = pg_type_for_field(f["type"], f["width"])
        if warn:
            warns.append(f"[{src}] {warn}")
        if dst in assigned:
            warns.append(f"[{src}] 目标列名 '{dst}' 重复，保留原名")
            dst = src
        assigned.add(dst)
        cols.append((src, dst, pg))
    return cols, warns


def layer_srs_srid(srs) -> Optional[int]:
    """从图层 SRS 提取 EPSG 编码；取不到返回 None。"""
    if srs is None:
        return None
    for auth in ("PROJCS", "GEOGCS"):
        code = srs.GetAuthorityCode(auth)
        if code:
            return int(code)
    return None


def geom_plan(meta: dict, rule, defaults) -> tuple[Optional[str], Optional[int], list[str]]:
    """几何列计划 -> (PG几何类型|None, SRID|None, 问题清单)。

    - geometries=False -> 不导入几何；
    - 类型未知/CURVE 类 -> 'GENERIC'（泛型 geometry 列）；
    - SRID 优先级：图层规则 > 图层自带 SRS > default.srid；都没有 -> 错误。
    """
    issues: list[str] = []
    if not defaults.geometries:
        return None, None, issues

    geom_pg = pg_geom_type(meta["geom_ogrid"])
    if geom_pg is None:
        geom_pg = "GENERIC"
        issues.append(f"图层几何类型 {meta['geom_name']} 映射为泛型 geometry 列")

    # SRID 优先级：图层规则强制 > 图层自带 SRS > default.srid 兜底
    forced = rule.srid
    layer_srid = layer_srs_srid(meta["srs"])
    if forced is not None:
        if layer_srid and layer_srid != forced:
            issues.append(f"图层自带 SRID={layer_srid} 与规则指定的 {forced} 不一致，以规则为准")
        srid = forced
    elif layer_srid is not None:
        srid = layer_srid
    else:
        srid = defaults.srid
    return geom_pg, srid, issues


def build_layer_plan(source: str, rule, meta: dict, defaults, schema: str) -> "LayerPlan":
    """组装单图层计划（供 dry-run 与执行共用）。"""
    from .model import LayerPlan

    issues: list[str] = []
    errors: list[str] = []

    table = rule.table or source
    table = normalize_table_name(table, launder=defaults.launder_tables)
    if defaults.launder_tables and table != (rule.table or source):
        issues.append(f"表名转小写：{rule.table or source} -> {table}")

    cols, warns = column_defs(meta["fields"], rule, defaults.launder_columns)
    issues.extend(warns)

    # 主键列名：仅来自 default.pk_column（默认 OBJECTID）；
    # 值始终用 GDB 原生 FID（=OBJECTID）
    pk_column = defaults.pk_column
    # 若该名字与某普通字段同名，此字段不再作为普通列导入（避免重名列）
    removed = [src for src, dst, _ in cols
               if dst.lower() == pk_column.lower()]
    if removed:
        cols = [c for c in cols if c[1].lower() != pk_column.lower()]
        issues.append(f"字段 {removed} 与主键列 {pk_column} 同名，不再重复导入为普通列")

    geom_pg, srid, gissues = geom_plan(meta, rule, defaults)
    issues.extend(gissues)

    mode = rule.mode or defaults.mode
    if mode not in ("create", "overwrite", "append"):
        errors.append(f"非法 mode: {mode}")

    if geom_pg is not None and srid is None:
        errors.append("无法确定 SRID：图层无 SRS 且未配置 default.srid / rule.srid")

    return LayerPlan(
        source=source,
        table=table,
        schema=schema,
        geom_column=rule.geom_column or defaults.geom_column,
        geometry_pg=geom_pg,
        srid=srid,
        mode=mode,
        feature_count=meta["feature_count"],
        columns=cols,
        issues=issues,
        errors=errors,
        fid_pk=defaults.fid_pk,
        pk_column=pk_column,
    )