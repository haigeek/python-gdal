# -*- coding: utf-8 -*-
"""SHP 导入配置/计划测试（无需 PostgreSQL）。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osgeo import ogr, osr

from gdb2pg import shp_reader
from gdb2pg.importer import plan_all
from gdb2pg.model import ImportConfig
from gdb2pg.tasks import get_task_type
from gdb2pg.web import api
from gdb2pg.web.config import WebConfig
from gdb2pg.web.schemas import ShpLayersRequest


def _make_shp(root: Path, name: str, geom_type=ogr.wkbPoint,
              fields: tuple[str, ...] = ("OBJECTID", "shape", "fid")) -> Path:
    path = root / f"{name}.shp"
    driver = ogr.GetDriverByName("ESRI Shapefile")
    ds = driver.CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer = ds.CreateLayer(name, srs=srs, geom_type=geom_type)
    for field_name in fields:
        layer.CreateField(ogr.FieldDefn(field_name, ogr.OFTString))
    feature = ogr.Feature(layer.GetLayerDefn())
    for i, field_name in enumerate(fields):
        feature.SetField(field_name, f"value-{i}")
    geom = ogr.Geometry(geom_type)
    if geom_type == ogr.wkbPoint25D:
        geom.AddPoint(116, 39, 10)
    else:
        geom.AddPoint(116, 39)
    feature.SetGeometry(geom)
    assert layer.CreateFeature(feature) == 0
    ds = None
    return path


def _config(path: Path, **default) -> ImportConfig:
    values = {"srid": 4326, "launder_columns": False, "launder_tables": False}
    values.update(default)
    return ImportConfig.from_dict({
        "shp": str(path),
        "database": {"schema": "public"},
        "default": values,
    })


def test_task_registry_and_validation():
    task = get_task_type("shp_import")
    assert task is not None
    default_fields = task.form_schema["groups"][0]["fields"]
    assert not any(f["key"] == "fid_pk" for f in default_fields)
    default_pk = next(f for f in default_fields if f["key"] == "pk_field")
    assert default_pk["default"] == ""
    layer_group = next(g for g in task.form_schema["groups"] if g["key"] == "layers")
    layer_table = next(f for f in layer_group["fields"] if f.get("type") == "table")
    pk_column = next(c for c in layer_table["columns"] if c["key"] == "pk_field")
    assert pk_column["type"] == "field"
    assert "SHP 路径不能为空" in task.validate({"database": {}})
    with tempfile.TemporaryDirectory(prefix="g2p_shp_import_") as tmp:
        path = _make_shp(Path(tmp), "roads")
        assert task.validate({"shp": str(path), "database": {
            "host": "h", "dbname": "d", "user": "u"}}) == []
        assert task.validate({"shp": str(path.with_suffix(".txt")), "database": {
            "host": "h", "dbname": "d", "user": "u"}})


def test_plan_preserves_conflicting_attributes():
    with tempfile.TemporaryDirectory(prefix="g2p_shp_import_") as tmp:
        path = _make_shp(Path(tmp), "roads")
        plan = plan_all(_config(path), source_kind="shp")[0]
        assert plan.fid_pk is False
        assert plan.pk_source == "auto"
        assert plan.pk_column == "fid_2"
        assert plan.geometry_pg == "POINT"
        assert plan.geom_column == "shape_geom"
        assert not plan.errors
        targets = {src: dst for src, dst, _ in plan.columns}
        assert targets["OBJECTID"] == "OBJECTID"
        assert targets["shape"] == "shape"
        assert targets["fid"] == "fid"
        assert any("几何列" in issue for issue in plan.issues)
        assert any("自增主键列" in issue for issue in plan.issues)


def test_fieldless_and_pointz_plans():
    with tempfile.TemporaryDirectory(prefix="g2p_shp_import_") as tmp:
        root = Path(tmp)
        fieldless = _make_shp(root, "fieldless", fields=())
        plan = plan_all(_config(fieldless), source_kind="shp")[0]
        assert {src for src, _dst, _pg in plan.columns} == {"FID"}
        assert plan.geometry_pg == "POINT"
        assert not plan.errors

        pointz = _make_shp(root, "pointz", geom_type=ogr.wkbPoint25D,
                           fields=("name",))
        plan_z = plan_all(_config(pointz), source_kind="shp")[0]
        assert plan_z.geometry_pg == "POINTZ"
        assert not plan_z.errors


def test_shp_config_defaults_and_attribute_only_mode():
    with tempfile.TemporaryDirectory(prefix="g2p_shp_import_") as tmp:
        path = _make_shp(Path(tmp), "roads")
        cfg = ImportConfig.from_dict({
            "shp": str(path),
            "database": {"schema": "public"},
        })
        assert cfg.default.fid_pk is False
        assert cfg.default.launder_columns is False

        selected = ImportConfig.from_dict({
            "shp": str(path),
            "database": {"schema": "public"},
            "default": {"pk_field": "OBJECTID"},
        })
        plan = plan_all(selected, source_kind="shp")[0]
        assert plan.pk_source == "field"
        assert plan.pk_field == "OBJECTID"
        assert plan.pk_column == "OBJECTID"
        assert plan.fid_pk is False
        assert "OBJECTID" in {src for src, _dst, _pg in plan.columns}

        overridden = ImportConfig.from_dict({
            "shp": str(path),
            "database": {"schema": "public"},
            "default": {"pk_field": "OBJECTID"},
            "layers": [{"source": "roads", "pk_field": "fid"}],
        })
        plan = plan_all(overridden, source_kind="shp")[0]
        assert plan.pk_field == "fid"
        assert plan.pk_column == "fid"

        disabled = ImportConfig.from_dict({
            "shp": str(path),
            "database": {"schema": "public"},
            "default": {"launder_columns": True},
        })
        assert disabled.default.fid_pk is False
        assert disabled.default.launder_columns is True

        no_geometry = _config(path, geometries=False)
        plan = plan_all(no_geometry, source_kind="shp")[0]
        assert plan.geometry_pg is None
        assert {src for src, _dst, _pg in plan.columns} == {"OBJECTID", "shape", "fid"}
        assert not plan.errors


def test_web_shp_layers_summary_and_path_whitelist():
    with tempfile.TemporaryDirectory(prefix="g2p_shp_import_") as tmp:
        root = Path(tmp)
        path = _make_shp(root, "roads")
        web_config = WebConfig(allowed_base_dirs=[str(root)])
        app = SimpleNamespace(
            state=SimpleNamespace(web_config=web_config),
        )
        request = SimpleNamespace(app=app)

        result = api.shp_layers(ShpLayersRequest(shp=str(path)), request)
        assert result["ok"] is True
        assert result["data"]["shp"] == os.path.realpath(path)
        assert result["data"]["layers"][0]["source"] == "roads"

        try:
            api.shp_layers(ShpLayersRequest(shp="/etc/passwd"), request)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 403
        else:
            raise AssertionError("越界 SHP 路径应被拒绝")


def test_multilinestring_beyond_sample_window_promotes_column():
    """Shapefile 的 LineString 图层可在任意位置藏着多段要素。

    回归：曾固定只采样前 200 个要素，导致第 1561 行的 MultiLineString
    未被探测，建出严格 LINESTRING 列后 COPY 报
    "Geometry type (MultiLineString) does not match column type (LineString)"。
    """
    from gdb2pg.importer import _downgrade_curve_geometry, _scan_geometry_names
    from gdb2pg.model import Defaults, LayerRule
    from gdb2pg.schema_mapper import build_layer_plan

    with tempfile.TemporaryDirectory(prefix="g2p_shp_mls_") as tmp:
        path = Path(tmp) / "roads.shp"
        driver = ogr.GetDriverByName("ESRI Shapefile")
        ds = driver.CreateDataSource(str(path))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        layer = ds.CreateLayer("roads", srs=srs, geom_type=ogr.wkbLineString)
        layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))

        # 1560 条单段线，MultiLineString 落在第 1561 行（旧采样窗口之外）
        for i in range(1560):
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("name", f"r{i}")
            geom = ogr.Geometry(ogr.wkbLineString)
            geom.AddPoint_2D(116 + i * 1e-4, 39.0)
            geom.AddPoint_2D(116 + i * 1e-4, 39.01)
            feature.SetGeometry(geom)
            layer.CreateFeature(feature)

        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("name", "overpass")
        multi = ogr.Geometry(ogr.wkbMultiLineString)
        for dx in (0.0, 0.001):
            part = ogr.Geometry(ogr.wkbLineString)
            part.AddPoint_2D(117 + dx, 40.0)
            part.AddPoint_2D(117 + dx, 40.01)
            multi.AddGeometry(part)
        feature.SetGeometry(multi)
        layer.CreateFeature(feature)
        ds = None

        ds = shp_reader.open_shp(path)
        layer = ds.GetLayerByName("roads")
        meta = shp_reader.layer_meta(layer)
        defaults = Defaults(geometries=True, srid=4326, mode="create")
        plan = build_layer_plan("roads", LayerRule(source="roads"), meta,
                                defaults, "public", source_kind="shp")
        assert plan.geometry_pg == "LINESTRING", plan.geometry_pg

        # 全量扫描必须看到多段要素（旧的 200 行窗口看不到）
        assert _scan_geometry_names(layer, shp_reader, limit=200) == {"linestring"}
        assert _scan_geometry_names(layer, shp_reader) == {
            "linestring", "multilinestring"}

        _downgrade_curve_geometry(ds, "roads", plan, reader=shp_reader)
        assert plan.geometry_pg == "MULTILINESTRING", plan.geometry_pg
        assert any("提升" in issue for issue in plan.issues), plan.issues


def test_pure_single_part_layer_stays_linestring():
    """只有单段线时不应无谓地把列提升为 MULTILINESTRING。"""
    from gdb2pg.importer import _downgrade_curve_geometry
    from gdb2pg.model import Defaults, LayerRule
    from gdb2pg.schema_mapper import build_layer_plan

    with tempfile.TemporaryDirectory(prefix="g2p_shp_single_") as tmp:
        path = Path(tmp) / "roads.shp"
        driver = ogr.GetDriverByName("ESRI Shapefile")
        ds = driver.CreateDataSource(str(path))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        layer = ds.CreateLayer("roads", srs=srs, geom_type=ogr.wkbLineString)
        layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
        for i in range(5):
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("name", f"r{i}")
            geom = ogr.Geometry(ogr.wkbLineString)
            geom.AddPoint_2D(116 + i * 1e-4, 39.0)
            geom.AddPoint_2D(116 + i * 1e-4, 39.01)
            feature.SetGeometry(geom)
            layer.CreateFeature(feature)
        ds = None

        ds = shp_reader.open_shp(path)
        layer = ds.GetLayerByName("roads")
        meta = shp_reader.layer_meta(layer)
        defaults = Defaults(geometries=True, srid=4326, mode="create")
        plan = build_layer_plan("roads", LayerRule(source="roads"), meta,
                                defaults, "public", source_kind="shp")
        _downgrade_curve_geometry(ds, "roads", plan, reader=shp_reader)
        assert plan.geometry_pg == "LINESTRING", plan.geometry_pg


def test_multi_wkb_wraps_single_geometry_as_sub_geometry():
    """multi_wkb 必须把单值几何**嵌入**为唯一子几何，而不是只改类型码。

    回归：曾只把类型码 2 改成 5，保留了 [nPoints][point...] 负载。多值的
    负载应是 [nGeoms][完整子几何 WKB...]，PostGIS 于是把第一个点的字节
    当成子几何头解析，报 "Unknown WKB type (75990314)"（正是坐标字节）。
    """
    from gdb2pg.gdb_reader import multi_wkb, normalize_wkb_to, set_ewkb_srid

    line = ogr.Geometry(ogr.wkbLineString)
    line.AddPoint_2D(1.0, 2.0)
    line.AddPoint_2D(3.0, 4.0)
    wkb = line.ExportToWkb(ogr.wkbNDR)

    # 单值必须原样返回（列类型不是 Multi 时不做任何改动）
    assert normalize_wkb_to(wkb, "LINESTRING") == wkb

    wrapped = multi_wkb(wkb)
    assert len(wrapped) > len(wkb), "包装后必须变长（多了 nGeoms + 子几何头）"
    outer_type = int.from_bytes(wrapped[1:5], "little")
    assert outer_type & 0xFF == 5, outer_type          # MultiLineString
    assert int.from_bytes(wrapped[5:9], "little") == 1  # nGeoms == 1

    # 子几何紧跟其后，是一个完整 WKB（字节序 + 类型 + 负载）
    # 注意子几何自带字节序字节，故类型码位于 [10:14] 而非 [9:13]
    assert wrapped[9] == wrapped[0], "子几何字节序应与外层一致"
    sub_type = int.from_bytes(wrapped[10:14], "little")
    assert sub_type & 0xFF == 2, sub_type              # 子几何为 LineString
    sub = ogr.CreateGeometryFromWkb(wrapped[9:])
    assert sub.GetGeometryName().upper() == "LINESTRING"
    assert sub.GetPointCount() == 2

    # SRID 只出现在最外层，子几何不得重复携带
    with_srid = multi_wkb(set_ewkb_srid(wkb, 4490))
    assert int.from_bytes(with_srid[1:5], "little") & 0x20000000
    assert int.from_bytes(with_srid[5:9], "little") == 4490
    sub_type_srid = int.from_bytes(with_srid[10:14], "little")
    assert not (sub_type_srid & 0x20000000), "子几何不应再带 EWKB/SRID 标志"

    # 整体可被 OGR 解析回 MULTILINESTRING（用不带 SRID 的形态验证结构）
    parseable = multi_wkb(wkb)
    geom = ogr.CreateGeometryFromWkb(parseable)
    assert geom.GetGeometryName().upper() == "MULTILINESTRING"
    assert geom.GetGeometryCount() == 1
    assert geom.GetGeometryRef(0).GetPointCount() == 2

    # 已是 Multi 时保持幂等
    assert multi_wkb(parseable) == parseable


def test_srid_zero_is_accepted_by_validation():
    """srid=0（坐标系未知）在 default 与图层规则两级都必须被接受。

    回归：两级校验曾写成 ``srid > 0``，界面上填 0 保存后会被判非法；
    配合前端 el-input-number 的 :min=1 钳制，0 会被悄悄改成 1。
    """
    task = get_task_type("shp_import")
    with tempfile.TemporaryDirectory(prefix="g2p_srid0_") as tmp:
        path = _make_shp(Path(tmp), "roads")
        db = {"host": "h", "dbname": "d", "user": "u"}

        # default.srid = 0 合法
        cfg = {"shp": str(path), "database": db, "default": {"srid": 0}}
        assert [e for e in task.validate(cfg) if "srid" in e] == []

        # 图层规则 srid = 0 合法
        cfg = {"shp": str(path), "database": db,
               "layers": [{"source": "roads", "srid": 0}]}
        assert [e for e in task.validate(cfg) if "srid" in e] == []

        # 正常 SRID 仍然合法
        for good in (1, 4326, 4490):
            cfg = {"shp": str(path), "database": db,
                   "default": {"srid": good},
                   "layers": [{"source": "roads", "srid": good}]}
            assert [e for e in task.validate(cfg) if "srid" in e] == [], good

        # 负数与非整数仍必须被拒绝
        for bad in (-1, "0", 1.5):
            cfg = {"shp": str(path), "database": db, "default": {"srid": bad}}
            assert [e for e in task.validate(cfg) if "srid" in e], bad
            cfg = {"shp": str(path), "database": db,
                   "layers": [{"source": "roads", "srid": bad}]}
            assert [e for e in task.validate(cfg) if "srid" in e], bad


def main() -> int:
    tests = [test_task_registry_and_validation,
             test_plan_preserves_conflicting_attributes,
             test_fieldless_and_pointz_plans,
             test_shp_config_defaults_and_attribute_only_mode,
             test_web_shp_layers_summary_and_path_whitelist,
             test_multilinestring_beyond_sample_window_promotes_column,
             test_pure_single_part_layer_stays_linestring,
             test_multi_wkb_wraps_single_geometry_as_sub_geometry,
             test_srid_zero_is_accepted_by_validation]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("all SHP import tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
