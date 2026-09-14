# -*- coding: utf-8 -*-
"""schema_mapper 纯函数单测。运行：.conda/bin/python -m pytest tests/test_schema_mapper.py -q"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osgeo import ogr

from gdb2pg import schema_mapper as sm
from gdb2pg.model import Defaults, LayerRule


def test_pg_type_for_field():
    assert sm.pg_type_for_field(ogr.OFTInteger, 0) == ("integer", None)
    assert sm.pg_type_for_field(ogr.OFTInteger64, 0) == ("bigint", None)
    assert sm.pg_type_for_field(ogr.OFTReal, 0) == ("double precision", None)
    assert sm.pg_type_for_field(ogr.OFTString, 0) == ("text", None)
    assert sm.pg_type_for_field(ogr.OFTString, 30) == ("varchar(30)", None)
    assert sm.pg_type_for_field(ogr.OFTString, 9000) == ("text", None)
    assert sm.pg_type_for_field(ogr.OFTDate, 0) == ("date", None)
    assert sm.pg_type_for_field(ogr.OFTTime, 0) == ("time", None)
    assert sm.pg_type_for_field(ogr.OFTDateTime, 0) == ("timestamptz", None)
    assert sm.pg_type_for_field(ogr.OFTBinary, 0) == ("bytea", None)
    assert sm.pg_type_for_field(ogr.OFTInteger, 1, ogr.OFSTBoolean) == ("boolean", None)
    pg, warn = sm.pg_type_for_field(ogr.OFTIntegerList, 0)
    assert pg == "text" and warn


def test_launder_and_normalize():
    assert sm.launder_name("XZQMC 名称") == "xzqmc_名称"
    assert sm.launder_name("AbC__ Def") == "abc_def"
    assert sm.launder_name("数据") != ""
    # 超长表名截断 + 哈希后缀
    long_name = "层" * 30  # 90 字节 UTF-8
    out = sm.normalize_table_name(long_name)
    assert len(out.encode("utf-8")) <= 63
    assert sm.normalize_table_name("name") == "name"


def test_column_defs_rename_and_dup():
    fields = [
        {"name": "a", "type": ogr.OFTString, "width": 10},
        {"name": "b", "type": ogr.OFTInteger, "width": 0},
    ]
    rule = LayerRule(source="x", columns={"a": "renamed"})
    cols, warns = sm.column_defs(fields, rule, launder=False)
    assert cols == [("a", "renamed", "varchar(10)"), ("b", "b", "integer")]
    assert warns == []


def test_geom_plan_srid_resolution():
    defaults = Defaults(srid=4490, mode="create")
    rule = LayerRule(source="x", srid=None)
    meta = {"geom_ogrid": ogr.wkbMultiLineString, "geom_name": "Multi Line String", "srs": None}
    geom_pg, srid, issues = sm.geom_plan(meta, rule, defaults)
    assert geom_pg == "MULTILINESTRING" and srid == 4490

    plan = sm.build_layer_plan("lines", rule, {
        **meta, "fields": [], "feature_count": 0,
    }, defaults, "public")
    assert plan.geom_column == "shape"

    # 图层自带 SRS 优先（未强制时）
    from osgeo import osr
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    meta["srs"] = srs
    geom_pg, srid, _ = sm.geom_plan(meta, rule, defaults)
    assert srid == 4326

    # 强制与自带冲突 -> 以强制为准 + 告警
    rule.srid = 4547
    geom_pg, srid, issues = sm.geom_plan(meta, rule, defaults)
    assert srid == 4547 and any("不一致" in i for i in issues)

    # 都没有 -> 回落 SRID=0（坐标系未知），不再阻断导入
    meta["srs"] = None
    rule.srid = None
    defaults2 = Defaults(srid=None, mode="create")
    geom_pg, srid, issues = sm.geom_plan(meta, rule, defaults2)
    assert srid == 0, srid
    assert geom_pg is not None
    assert any("SRID=0" in i for i in issues), issues


def test_generic_geometry_for_curves():
    defaults = Defaults(srid=4490, mode="create")
    # wkbCompoundCurve 等曲线类型 -> GENERIC
    meta = {"geom_ogrid": ogr.wkbCompoundCurve, "geom_name": "Compound Curve", "srs": None}
    geom_pg, srid, issues = sm.geom_plan(meta, LayerRule(source="x"), defaults)
    assert geom_pg == "GENERIC"


def test_attribute_only_layer_has_no_geometry_column():
    defaults = Defaults(srid=None, mode="create")
    meta = {"geom_ogrid": ogr.wkbNone, "geom_name": "None", "srs": None,
            "fields": [{"name": "name", "type": ogr.OFTString, "width": 0}],
            "feature_count": 1}
    plan = sm.build_layer_plan("attrs", LayerRule(source="attrs"), meta, defaults, "public")
    assert plan.geometry_pg is None
    assert not plan.errors


def test_gdb_native_fid_strategy_is_unchanged():
    defaults = Defaults(srid=4326, mode="create")
    meta = {
        "geom_ogrid": ogr.wkbPoint,
        "geom_name": "Point",
        "srs": None,
        "fields": [{"name": "OBJECTID", "type": ogr.OFTInteger, "width": 0}],
        "feature_count": 1,
    }
    plan = sm.build_layer_plan(
        "points", LayerRule(source="points"), meta, defaults, "public",
        source_kind="gdb",
    )
    assert plan.pk_source == "fid"
    assert plan.pk_column == "objectid"
    assert plan.fid_pk is True
    assert "OBJECTID" not in {src for src, _dst, _pg in plan.columns}


def test_build_layer_plan_unknown_crs_falls_back_to_srid_0():
    """CRS 未知（缺 .prj）时不再报错阻断，改为建 SRID=0 的列。

    旧行为是 errors 里写「无法确定 SRID」并要求配置 default.srid；
    实测数据常缺 .prj，强行猜一个 SRID 会产出静默错误的坐标语义。
    """
    defaults = Defaults(srid=None, mode="create")
    meta = {"geom_ogrid": ogr.wkbPoint, "geom_name": "Point", "srs": None,
            "fields": [{"name": "nm", "type": ogr.OFTString, "width": 0}],
            "feature_count": 3}
    plan = sm.build_layer_plan("pts", LayerRule(source="pts"), meta, defaults, "public")
    assert not plan.errors, plan.errors
    assert plan.srid == 0, plan.srid
    assert plan.geometry_pg == "POINT", plan.geometry_pg
    assert any("SRID=0" in i for i in plan.issues), plan.issues


def test_geom_ddl_keeps_srid_0_typmod():
    """srid=0 必须写成 geometry(Type,0)，不能用真值判断退化成无 typmod 列。"""
    from gdb2pg.pg_writer import PgWriter
    assert PgWriter._geom_ddl("shape", "MULTILINESTRING", 0) is not None
    sql0 = PgWriter._geom_ddl("shape", "MULTILINESTRING", 0).as_string(None)
    sql4490 = PgWriter._geom_ddl("shape", "MULTILINESTRING", 4490).as_string(None)
    sqlnone = PgWriter._geom_ddl("shape", "MULTILINESTRING", None).as_string(None)
    assert sql0 == '"shape" geometry(MULTILINESTRING,0)', sql0
    assert sql4490 == '"shape" geometry(MULTILINESTRING,4490)', sql4490
    # 仅 srid=None 才省略 typmod
    assert sqlnone == '"shape" geometry(MULTILINESTRING)', sqlnone


def test_strip_ewkb_srid_removes_header_srid():
    """SRID=0 时必须清掉 EWKB 头部 SRID，否则行内 SRID 与列定义不一致。"""
    from gdb2pg.gdb_reader import set_ewkb_srid, strip_ewkb_srid
    line = ogr.Geometry(ogr.wkbLineString)
    line.AddPoint_2D(1.0, 2.0)
    line.AddPoint_2D(3.0, 4.0)
    plain = line.ExportToWkb(ogr.wkbNDR)

    stripped = strip_ewkb_srid(set_ewkb_srid(plain, 4490))
    assert not (int.from_bytes(stripped[1:5], "little") & 0x20000000)
    assert stripped == plain, "清除后应还原为原始 ISO WKB"

    # 本就不含 SRID 时保持不变
    assert strip_ewkb_srid(plain) == plain

    # 清除后仍可被 OGR 解析（几何未损坏）
    geom = ogr.CreateGeometryFromWkb(stripped)
    assert geom.GetGeometryName().upper() == "LINESTRING"
    assert geom.GetPointCount() == 2


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"PASS {fn.__name__}")
    print("all passed")
