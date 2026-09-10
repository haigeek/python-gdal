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


def pg_type_for_field(ftype: int, width: int, subtype: int = 0) -> tuple[str, Optional[str]]:
    """OGR 字段类型码/子类型 -> (PG 类型, 警告)。"""
    if subtype == getattr(ogr, "OFSTBoolean", -1):
        return "boolean", None
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
        pg, warn = pg_type_for_field(f["type"], f["width"], f.get("subtype", 0))
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


def _geometry_dimension_suffix(ogr_type: int) -> str:
    """OGR 几何类型的维度后缀（Z/M/ZM），未知时返回空。"""
    has_z = bool(getattr(ogr, "GT_HasZ", lambda _: False)(ogr_type))
    has_m = bool(getattr(ogr, "GT_HasM", lambda _: False)(ogr_type))
    return ("z" if has_z else "") + ("m" if has_m else "")


def geom_plan(meta: dict, rule, defaults) -> tuple[Optional[str], Optional[int], list[str]]:
    """几何列计划 -> (PG几何类型|None, SRID|None, 问题清单)。

    - geometries=False -> 不导入几何；
    - 类型未知/CURVE 类 -> 'GENERIC'（泛型 geometry 列）；
    - SRID 优先级：图层规则 > 图层自带 SRS > default.srid；都没有 -> 错误。
    """
    issues: list[str] = []
    if not defaults.geometries:
        return None, None, issues
    # wkbNone 表示纯属性层，不是未知几何；不能创建泛型 geometry 列，
    # 否则无 SRS 的 DBF/SHP 也会被错误要求 SRID。
    if meta["geom_ogrid"] == ogr.wkbNone:
        return None, None, issues

    geom_pg = pg_geom_type(meta["geom_ogrid"])
    if geom_pg is None:
        dimension = _geometry_dimension_suffix(meta["geom_ogrid"])
        geom_pg = "GENERIC" + dimension.upper()
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


def _unique_target_name(base: str, used: set[str]) -> str:
    """在目标列名集合中生成不冲突的辅助列名。"""
    candidate = base
    n = 2
    while candidate.lower() in used:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def build_layer_plan(source: str, rule, meta, defaults, schema: str,
                     source_kind: str = "gdb") -> "LayerPlan":
    """组装单图层计划（供 dry-run 与执行共用）。

    SHP 与 GDB 的主键策略分开处理：SHP 默认保留全部 DBF 字段，
    只有配置 ``pk_field`` 时才把某个源字段设为主键；GDB 沿用原有
    ``fid_pk``/``pk_column`` 逻辑。
    """
    from .model import LayerPlan

    issues: list[str] = []
    errors: list[str] = []
    is_shp = source_kind == "shp"

    table = rule.table or source
    table = normalize_table_name(table, launder=defaults.launder_tables)
    if defaults.launder_tables and table != (rule.table or source):
        issues.append(f"表名转小写：{rule.table or source} -> {table}")

    cols, warns = column_defs(meta["fields"], rule, defaults.launder_columns)
    issues.extend(warns)

    geom_pg, srid, gissues = geom_plan(meta, rule, defaults)
    issues.extend(gissues)
    geom_column = rule.geom_column or defaults.geom_column

    # 默认 pk_source=None 时按旧字段推导，兼容外部直接构造 LayerPlan 的调用方。
    pk_source = "fid" if defaults.fid_pk else "auto"
    pk_field = None
    pk_column = defaults.pk_column if defaults.fid_pk else "fid"

    rule_pk_field = rule.pk_field
    if rule_pk_field is None or (isinstance(rule_pk_field, str) and not rule_pk_field.strip()):
        requested_value = defaults.pk_field
    else:
        requested_value = rule_pk_field
    requested = requested_value.strip() if isinstance(requested_value, str) else ""
    if is_shp and requested_value is not None and not isinstance(requested_value, str):
        errors.append("主键字段必须为字符串")
        requested_value = None
    if is_shp and requested:
        matches = [src for src, _dst, _pg in cols if src == requested]
        if not matches:
            matches = [src for src, _dst, _pg in cols
                       if src.lower() == requested.lower()]
        if not matches:
            errors.append(f"主键字段不存在：{requested}")
            pk_field = requested
            pk_column = requested
        else:
            pk_field = matches[0]
            pk_column = next(dst for src, dst, _pg in cols if src == pk_field)
        pk_source = "field"
    elif is_shp:
        # SHP 默认不使用 OGR FID；fid_pk=True 仅用于兼容此前已经保存的配置。
        pk_source = "fid" if defaults.fid_pk else "auto"
        if pk_source == "fid":
            issues.append("兼容旧配置：SHP 使用 OGR FID；建议改为选择 pk_field")

    if is_shp and pk_source in ("field", "auto"):
        # 源字段必须原样保留。若源字段恰好叫 shape，则移动辅助几何列，
        # 不通过重命名源字段来解决 DDL 冲突。
        used = {dst.lower() for _src, dst, _pg in cols}
        if geom_pg is not None and geom_column.lower() in used:
            old_geom_column = geom_column
            geom_column = _unique_target_name(f"{geom_column}_geom", used)
            issues.append(
                f"几何列 {old_geom_column} 与源字段冲突，几何列改为 {geom_column}，保留源字段")
            used.add(geom_column.lower())
        if pk_source == "auto":
            old_pk_column = pk_column
            pk_column = _unique_target_name(pk_column, used | {
                geom_column.lower() if geom_pg is not None else "",
            })
            if pk_column != old_pk_column:
                issues.append(
                    f"自增主键列 {old_pk_column} 与源字段冲突，改为 {pk_column}")
    else:
        # GDB 以及显式启用旧 SHP OGR FID 的配置保持原有冲突处理行为。
        if defaults.fid_pk:
            removed = [src for src, dst, _ in cols
                       if dst.lower() == pk_column.lower()]
            if removed:
                cols = [c for c in cols if c[1].lower() != pk_column.lower()]
                issues.append(f"字段 {removed} 与主键列 {pk_column} 同名，不再重复导入为普通列")

        reserved = {"fid"} if not defaults.fid_pk else set()
        if geom_pg is not None:
            reserved.add(geom_column.lower())
        if reserved:
            used = {dst.lower() for _, dst, _ in cols}
            adjusted = []
            for src, dst, pg in cols:
                if dst.lower() not in reserved:
                    adjusted.append((src, dst, pg))
                    continue
                base = f"{dst}_attr"
                candidate = _unique_target_name(base, used | reserved)
                used.add(candidate.lower())
                adjusted.append((src, candidate, pg))
                issues.append(f"字段 {src} 与保留列 {dst} 冲突，目标列改为 {candidate}")
            cols = adjusted

    mode = rule.mode or defaults.mode
    if mode not in ("create", "overwrite", "append"):
        errors.append(f"非法 mode: {mode}")

    if geom_pg is not None and srid is None:
        errors.append("无法确定 SRID：图层无 SRS 且未配置 default.srid / rule.srid")

    return LayerPlan(
        source=source,
        table=table,
        schema=schema,
        geom_column=geom_column,
        geometry_pg=geom_pg,
        srid=srid,
        mode=mode,
        feature_count=meta["feature_count"],
        columns=cols,
        issues=issues,
        errors=errors,
        fid_pk=pk_source == "fid",
        pk_column=pk_column,
        pk_source=pk_source,
        pk_field=pk_field,
    )
