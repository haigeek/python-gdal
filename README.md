# python-gdal · GDB 读取 + 导入 PostGIS

本工程两部分：
1. **demo_read_gdb.py** —— 用 Python 版 GDAL/OGR 读取 File Geodatabase (.gdb) 的示例；
2. **gdb2pg（gdb2pg/ 包）** —— 把 .gdb 一键导入 PostgreSQL/PostGIS：自动建表、自动写数据，
   全部由 JSON 配置驱动，为后续可视化配置预留契约。

> 背景：本机为 Intel Mac (x86_64)，Homebrew 的 `gdal` 只发布 arm64 bottle，
> PyPI 也**没有** macOS x86_64 的 gdal wheel，因此采用
> **micromamba + conda-forge** 提供预编译的 GDAL Python 绑定
> （环境同时装了 psycopg、postgresql、postgis 用于导入与本地验证）。

---

## Part 1 · demo_read_gdb.py（读取示例）

```bash
# 指定 GDB 路径，抽样打印前 5 条要素
.conda/bin/python demo_read_gdb.py /path/to/xxx.gdb

# 只列图层名 / 只读指定图层、每层打印 10 条
.conda/bin/python demo_read_gdb.py /path/to/xxx.gdb --list-only
.conda/bin/python demo_read_gdb.py /path/to/xxx.gdb --layers "layerA,layerB" --limit 10
```

演示内容：驱动探测（OpenFileGDB/FileGDB）、打开 GDB、图层枚举、图层详情
（要素数/几何类型/范围/字段结构）、要素属性与 WKT 几何抽样。关键 API 均来自 `osgeo.ogr`：

| 目的 | API |
| --- | --- |
| 打开 GDB | `ogr.Open(path, update=0)` |
| 取图层 | `ds.GetLayerByIndex(i)` / `ds.GetLayerByName(name)` |
| 要素数 | `layer.GetFeatureCount()` |
| 遍历要素 | `for feat in layer:` |
| 读属性 / 读几何 | `feat.GetField(i)` / `feat.GetGeometryRef().ExportToWkt()` |

---

## Part 2 · gdb2pg（导入 PostGIS）

> 设计说明：conda-forge osx-64 的 GDAL 未编原生 PostgreSQL 驱动（仅 PGDump），
> 故架构为 **GDAL/OGR 只读 GDB + psycopg 直写 PostGIS**：
> `gdb_reader.py`（读）→ `schema_mapper.py`（OGR→PG DDL/SRID/清洗）→
> `pg_writer.py`（建表/COPY/索引/回滚）→ `importer.py`（编排/dry-run/进度）。
> 全部行为由 JSON 配置驱动，未来 GUI 只需读写这份配置再调 `importer.run()`。

### 使用

```bash
# 预览（连库只读检查，不写任何数据）
.conda/bin/python -m gdb2pg import --config configs/example.json --dry-run

# 正式导入（自动建表 + COPY 写入 + GIST 索引 + 计数对账）
.conda/bin/python -m gdb2pg import --config configs/example.json

# 覆盖目标 schema / 只查看一个 GDB 的图层结构
.conda/bin/python -m gdb2pg import --config configs/example.json --schema myschema
.conda/bin/python -m gdb2pg inspect --gdb /path/to/xxx.gdb
```

### 配置（configs/template.json 为模板）

```json
{
  "gdb": "/path/to/xxx.gdb",
  "database": {
    "host": "10.x.x.x", "port": 5432, "dbname": "gis",
    "user": "reader", "password_env": "GDB2PG_PGPASSWORD",
    "schema": "public", "ssl": "prefer", "ensure_postgis": false
  },
  "default": {
    "srid": 4490, "mode": "create", "geometries": true,
    "create_spatial_index": true, "launder_columns": false, "on_error": "abort"
  },
  "selectors": { "include": ["*"], "exclude": [] },
  "layers": [ { "source": "bzxxm_ln", "table": "bzxxm_ln", "srid": null, "mode": null } ]
}
```

- `default.srid`：图层无 SRS 时的兜底 SRID（如 4326/4490/4547）；优先级为
  **图层规则 > 图层自带 EPSG > default.srid**，都没有则该层在 dry-run 报错、不导入；
- `mode`：`create`（表已存在则失败）| `overwrite`（重建）| `append`（追加）；
- `on_error`：`abort` | `skip`；`launder_columns`：列名转小写下划线（默认保真）；
- `layers` 支持通配符与 `columns` 列重命名；密码优先取环境变量（推荐），也可明文写 `password`。

### 能力与鲁棒性（已在真实数据验证）

| 场景 | 处理 |
| --- | --- |
| 中文图层名/字段名/属性、中文 GDB 路径 | 标识符双引号引用，UTF-8 无损 |
| MULTICURVE/CIRCULARSTRING 混在 MultiLineString 图层 | 探测实际几何，自动降级 `geometry(Geometry,srid)` |
| 3D 几何 | Z 变体写 `geometry(MULTILINESTRINGZ,...)` |
| NULL 几何 / 空图层 / 脏日期（m=0） | 分别落 NULL、建表不导数据、容错为 NULL |
| 表名超 63 字节 / 目标表冲突 | 截断+短哈希 / dry-run 报错 |
| DateTime 带时区 | 无损落 `timestamptz` |
| 大图层 | COPY 单趟批量写入 + 每万条进度 |
| 导入中途失败 | 单层事务整体回滚，不留残表 |

### 本地验证库（可选，无远程可写库时自检用）

```bash
bash scripts/dev_db.sh        # 项目内装 postgresql+postgis，起 127.0.0.1:5433，建 gdb2pg_test
.conda/bin/python -m gdb2pg import --config configs/dev_local.json
.conda/bin/python tests/test_import_e2e.py   # 全链路断言：计数/SRID/GIST索引/中文属性
```

远程库方式：把连接参数写进配置即可（或 `GDB2PG_PGURL=postgresql://...` 供 e2e 测试用）。

---

## 环境准备（已就绪，可跳过）

```bash
# 1. 下载 micromamba 到项目内 .tools/（已下载）
# 2. 创建 conda 环境（已创建于 .conda/，含 python 3.11 + gdal 3.13.3）
#    （把 HOME 指到项目内，避免向 ~/.cache 写缓存；权限不足时也会触发）
HOME=$PWD/.home MAMBA_ROOT_PREFIX=$PWD/.mamba \
  .tools/bin/micromamba create -p ./.conda -c conda-forge python=3.11 gdal -y
# 3. 导入工具依赖（已装）：psycopg；本地验证库另有 postgresql/postgis
#    HOME=$PWD/.home .tools/bin/micromamba install -p ./.conda -c conda-forge psycopg -y
```

验证：`.conda/bin/python -c "from osgeo import gdal, ogr; print(gdal.__version__)"`

## 磁盘占用与清理（整套约 900M，代码仅几十 KB）

| 路径 | 大小 | 是什么 | 处理 |
| --- | --- | --- | --- |
| `.mamba/pkgs/https` | ~673M | conda 包缓存（gdal/postgis/openblas 等的安装包归档与解压件） | 可清，不影响运行环境 |
| `.mamba/envs/.conda` | ~103M | 实际运行环境（python3.11+GDAL+psycopg+postgresql；与 pkgs 硬链接共享，单独 du 偏大） | 删了工具全废 |
| `pgdata` | ~97M | 本地验证库数据（pg_wal + 导入的表） | 验证完可停库删除 |
| `.tools` | ~16M | micromamba 二进制 | 需要，勿删 |
| `.home` | ~4.8M | conda repodata 索引缓存 | 可清，自动重建 |

清理命令：

```bash
# 1) 清 conda 包缓存（安全，环境照常可用，一般放出数百 MB）
HOME=$PWD/.home MAMBA_ROOT_PREFIX=$PWD/.mamba .tools/bin/micromamba clean --packages -y

# 2) 停掉并删除本地验证库（释放 ~97M，需先停服务）
.conda/bin/pg_ctl -D pgdata stop && rm -rf pgdata

# 3) 彻底卸掉整套工具链（释放 ~780M，项目代码不受影响）
rm -rf .mamba .tools .home .conda
```

> 说明：`.conda` 是指向 `.mamba/envs/.conda` 的符号链接；环境文件与包缓存是**硬链接**共享，
> 所以"环境占用"不能按 `du` 单目录值简单相加，实际总占用见上表。

## 注意事项

- `OpenFileGDB` 驱动**只读**，无需 ESRI 许可；若 GDB 用了较新的压缩/加密特性，个别图层可能打不开；
- 本机 conda 环境的 GDAL 无原生 PostgreSQL 驱动，这是采用 psycopg 直写的原因（见 Part 2 说明）；