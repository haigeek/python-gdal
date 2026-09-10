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


def main() -> int:
    tests = [test_task_registry_and_validation,
             test_plan_preserves_conflicting_attributes,
             test_fieldless_and_pointz_plans,
             test_shp_config_defaults_and_attribute_only_mode,
             test_web_shp_layers_summary_and_path_whitelist]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("all SHP import tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
