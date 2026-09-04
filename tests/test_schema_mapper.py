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

    # 都没有 -> error 信号（srid=None）
    meta["srs"] = None
    rule.srid = None
    defaults2 = Defaults(srid=None, mode="create")
    geom_pg, srid, _ = sm.geom_plan(meta, rule, defaults2)
    assert srid is None


def test_generic_geometry_for_curves():
    defaults = Defaults(srid=4490, mode="create")
    # wkbCompoundCurve 等曲线类型 -> GENERIC
    meta = {"geom_ogrid": ogr.wkbCompoundCurve, "geom_name": "Compound Curve", "srs": None}
    geom_pg, srid, issues = sm.geom_plan(meta, LayerRule(source="x"), defaults)
    assert geom_pg == "GENERIC"


def test_build_layer_plan_no_srid_error():
    defaults = Defaults(srid=None, mode="create")
    meta = {"geom_ogrid": ogr.wkbPoint, "geom_name": "Point", "srs": None,
            "fields": [{"name": "nm", "type": ogr.OFTString, "width": 0}],
            "feature_count": 3}
    plan = sm.build_layer_plan("pts", LayerRule(source="pts"), meta, defaults, "public")
    assert plan.errors and "SRID" in plan.errors[0]


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"PASS {fn.__name__}")
    print("all passed")