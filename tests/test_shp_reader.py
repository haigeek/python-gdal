# -*- coding: utf-8 -*-
"""SHP 读取层单测（无需数据库）。

测试会用 GDAL 的 ESRI Shapefile 驱动生成临时 ``.shp`` 文件，并同时
覆盖直接打开文件和打开其所在目录两种数据源形式。
"""

from __future__ import annotations

import datetime as _dt
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osgeo import ogr, osr

from gdb2pg import shp_reader


def _create_points(directory: Path, name: str, count: int = 2) -> Path:
    """在 directory 中创建一个带投影和属性字段的点 Shapefile。"""
    path = directory / f"{name}.shp"
    driver = ogr.GetDriverByName("ESRI Shapefile")
    assert driver is not None, "GDAL 未提供 ESRI Shapefile 驱动"

    ds = driver.CreateDataSource(str(path))
    assert ds is not None
    srs = osr.SpatialReference()
    assert srs.ImportFromEPSG(4326) == 0
    lyr = ds.CreateLayer(name, srs=srs, geom_type=ogr.wkbPoint)
    lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("count", ogr.OFTInteger))
    lyr.CreateField(ogr.FieldDefn("when", ogr.OFTDate))

    for i in range(count):
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetField("name", f"point-{i}")
        feat.SetField("count", i)
        feat.SetField("when", f"2024-01-{i + 1:02d}")
        geom = ogr.Geometry(ogr.wkbPoint)
        geom.AddPoint(116.0 + i, 39.0 + i)
        feat.SetGeometry(geom)
        assert lyr.CreateFeature(feat) == 0
    ds = None
    return path


def test_open_file_and_read_features():
    """单个 .shp 文件应暴露与 gdb_reader 相同的读取契约。"""
    with tempfile.TemporaryDirectory(prefix="g2p_shp_test_") as tmp:
        shp = _create_points(Path(tmp), "roads", count=2)
        ds = shp_reader.open_shp(shp)
        assert shp_reader.list_layers(ds) == ["roads"]

        lyr = ds.GetLayerByName("roads")
        meta = shp_reader.layer_meta(lyr)
        assert meta["feature_count"] == 2
        assert meta["geom_ogrid"] == ogr.wkbPoint
        assert meta["geom_name"] == "Point"
        assert [f["name"] for f in meta["fields"]] == ["name", "count", "when"]
        assert meta["srs"] is not None

        rows = list(shp_reader.iter_features(lyr, meta["srs"]))
        assert len(rows) == 2
        attrs, wkb, fid = rows[0]
        assert attrs["name"] == "point-0"
        assert attrs["count"] == 0
        assert isinstance(attrs["when"], _dt.date)
        assert not isinstance(attrs["when"], _dt.datetime)
        assert wkb is not None and wkb[0] in (0, 1) and len(wkb) >= 5
        assert isinstance(fid, int)

        # SHP 的几何和 SRID 处理沿用 GDB 读取层实现。
        ewkb = shp_reader.set_ewkb_srid(bytes(wkb), 4326)
        assert struct.unpack("<I", ewkb[5:9])[0] == 4326
        assert shp_reader.pg_geom_type(ogr.wkbPoint) == "POINT"
        assert shp_reader.observed_geometry_names(lyr) == {"point"}


def test_open_directory_and_summary():
    """目录数据源应枚举目录内多个 Shapefile，并生成摘要。"""
    with tempfile.TemporaryDirectory(prefix="g2p_shp_test_") as tmp:
        directory = Path(tmp)
        _create_points(directory, "roads", count=2)
        _create_points(directory, "places", count=1)

        ds = shp_reader.open_shp(directory)
        assert sorted(shp_reader.list_layers(ds)) == ["places", "roads"]

        summary = shp_reader.shp_layers_summary(directory)
        assert {row["source"] for row in summary} == {"roads", "places"}
        by_name = {row["source"]: row for row in summary}
        assert by_name["roads"]["feature_count"] == 2
        assert by_name["places"]["feature_count"] == 1
        assert by_name["roads"]["geometry"] == "POINT"
        assert by_name["roads"]["srid"] == 4326
        assert by_name["roads"]["fields"] == ["name", "count", "when"]


def test_measured_geometry_dimensions_are_preserved():
    with tempfile.TemporaryDirectory(prefix="g2p_shp_test_") as tmp:
        path = Path(tmp) / "measured.shp"
        driver = ogr.GetDriverByName("ESRI Shapefile")
        ds = driver.CreateDataSource(str(path))
        layer = ds.CreateLayer("measured", geom_type=ogr.wkbPointZM)
        feature = ogr.Feature(layer.GetLayerDefn())
        geom = ogr.Geometry(ogr.wkbPointZM)
        geom.SetPointZM(0, 116.0, 39.0, 10.0, 5.0)
        feature.SetGeometry(geom)
        assert layer.CreateFeature(feature) == 0
        ds = None

        ds = shp_reader.open_shp(path)
        layer = ds.GetLayerByName("measured")
        assert shp_reader.observed_geometry_dimensions(layer) == {"pointzm"}
        rows = list(shp_reader.iter_features(layer))
        assert rows[0][1] is not None


def test_real_fid_field_is_not_filtered():
    with tempfile.TemporaryDirectory(prefix="g2p_shp_reader_") as tmp:
        root = Path(tmp)
        path = root / "real_fid.shp"
        driver = ogr.GetDriverByName("ESRI Shapefile")
        ds = driver.CreateDataSource(str(path))
        layer = ds.CreateLayer("real_fid", geom_type=ogr.wkbPoint)
        field = ogr.FieldDefn("FID", ogr.OFTInteger64)
        field.SetWidth(11)
        layer.CreateField(field)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("FID", 100)
        geom = ogr.Geometry(ogr.wkbPoint)
        geom.AddPoint(116, 39)
        feature.SetGeometry(geom)
        assert layer.CreateFeature(feature) == 0
        ds = None

        ds = shp_reader.open_shp(path)
        meta = shp_reader.layer_meta(ds.GetLayer(0))
        assert "FID" in [f["name"] for f in meta["fields"]]


def test_open_invalid_source_raises_runtime_error():
    with tempfile.TemporaryDirectory(prefix="g2p_shp_test_") as tmp:
        bogus = Path(tmp) / "not-a-shapefile.shp"
        bogus.write_text("not a valid shapefile", encoding="utf-8")
        try:
            shp_reader.open_shp(bogus)
        except RuntimeError as exc:
            assert "SHP" in str(exc) or "Shapefile" in str(exc)
        else:
            raise AssertionError("invalid SHP should raise RuntimeError")


if __name__ == "__main__":
    tests = [
        test_open_file_and_read_features,
        test_open_directory_and_summary,
        test_measured_geometry_dimensions_are_preserved,
        test_real_fid_field_is_not_filtered,
        test_open_invalid_source_raises_runtime_error,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("all SHP reader tests passed")
