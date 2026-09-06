# python-gdal · GDB 读取 + 导入 PostGIS + Web 任务管理

本工程三部分：
1. **demo_read_gdb.py** —— 用 Python 版 GDAL/OGR 读取 File Geodatabase (.gdb) 的示例；
2. **gdb2pg（gdb2pg/ 包）** —— 把 .gdb 一键导入 PostgreSQL/PostGIS：自动建表、自动写数据，
   全部由 JSON 配置驱动；
3. **Web 可视化任务管理（v0.2.0）** —— FastAPI + Vue3：浏览器新建/查看/增删改查导入任务、
   实时进度日志、dry-run 预览、GDB 本地路径或 zip 上传；抽象任务框架支持未来扩展其他任务类型
   （见 [Part 3](#part-3--web-可视化任务管理v020)）。

---

## Part 1 · demo_read_gdb.py（读取示例）

```bash
# 指定 GDB 路径，抽样打印前 5 条要素
python demo_read_gdb.py /path/to/xxx.gdb

# 只列图层名 / 只读指定图层、每层打印 10 条
python demo_read_gdb.py /path/to/xxx.gdb --list-only
python demo_read_gdb.py /path/to/xxx.gdb --layers "layerA,layerB" --limit 10
```
> 以下命令均在**已激活的项目 Python 环境**（conda 环境或项目 venv）中执行。

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

> 设计说明：**GDAL/OGR 只读 GDB + psycopg 直写 PostGIS**：
> `gdb_reader.py`（读）→ `schema_mapper.py`（OGR→PG DDL/SRID/清洗）→
> `pg_writer.py`（建表/COPY/索引/回滚）→ `importer.py`（编排/dry-run/进度）。
> 全部行为由 JSON 配置驱动，Web 任务框架直接读写这份配置再调 `importer.run()`。

### 使用

```bash
# 预览（连库只读检查，不写任何数据）
python -m gdb2pg import --config configs/example.json --dry-run

# 正式导入（自动建表 + COPY 写入 + GIST 索引 + 计数对账）
python -m gdb2pg import --config configs/example.json

# 覆盖目标 schema / 只查看一个 GDB 的图层结构
python -m gdb2pg import --config configs/example.json --schema myschema
python -m gdb2pg inspect --gdb /path/to/xxx.gdb
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
python -m gdb2pg import --config configs/dev_local.json
python tests/test_import_e2e.py   # 全链路断言：计数/SRID/GIST索引/中文属性
```

远程库方式：把连接参数写进配置即可（或 `GDB2PG_PGURL=postgresql://...` 供 e2e 测试用）。

---

## Part 3 · Web 可视化任务管理（v0.2.0）

浏览器操作：**新建导入任务（本地路径或上传 zip）→ 预览 → 运行 → 实时看进度与日志 → 查看逐层结果**。
任务元数据/日志/进度持久化在**业务库**（纯 Postgres，不要求 PostGIS）；导入目标库由每个任务自己的配置决定，可多实例。

### 架构

```
浏览器 (Vue3 + Element Plus, Vite 构建 → gdb2pg/web/static)
   │  /api/*  (FastAPI)
   ▼
tasks/  抽象任务框架 ── TaskStore（业务库 g2p.task / task_log）
         TaskType 注册表（首个类型 gdb_import）
         TaskManager（worker 线程队列 + 协作式取消）
   │
   ▼
importer.run() → gdb_reader → schema_mapper → pg_writer（导入目标库）
```

- **任务类型抽象**：新类型只需实现 `gdb2pg/tasks/base.py` 的 `TaskType`（`type/label/form_schema + validate/preview/run/sanitize`）并用 `@register` 装饰；前端按 `form_schema` **自动渲染表单**，无需改动前端。
- **业务库与目标库分离**：业务数据（任务记录/日志）在 Web 配置 `management` 段指定的库；每个任务 `config.database` 是导入目标。

### 快速开始

```bash
# 1) 确保 Python 环境就绪：见文末「环境与依赖」（需要 gdal + psycopg +
#    fastapi/uvicorn/python-multipart；httpx 仅测试用）

# 2) 前端构建（Node ≥ 18）：产物输出到 gdb2pg/web/static
cd frontend && npm install && npm run build && cd ..

# 3) 起本地业务库（5433）并启动 Web（configs/web.dev.json）
bash scripts/dev_web.sh          # 或手动：python -m gdb2pg web --config configs/web.dev.json
# 打开 http://127.0.0.1:8000
```

生产部署：把 `configs/web.json.example` 复制为 `configs/web.json`，填业务库连接与目录白名单：

```json
{
  "management": { "host": "...", "port": 5432, "dbname": "g2p_meta",
                  "user": "...", "password_env": "G2P_MGMT_PGPASSWORD",
                  "schema": "g2p" },
  "host": "0.0.0.0", "port": 8000, "workers": 1,
  "uploads_dir": "uploads",
  "allowed_base_dirs": ["/srv/gisdata"],
  "max_upload_mb": 2048,
  "upload_ttl_days": 7
}
```

`management` 为业务库连接，其中 `schema` 是任务元数据表所在 schema（默认 `g2p`，首次启动自动建表）。

### Docker 部署

镜像内置前端构建产物与 GDAL（OpenFileGDB）。业务库（Postgres/PostGIS）**外置**，compose 只起 web 服务，**全部配置走配置文件**：

```bash
# 1) 复制配置模板并填写业务库连接（含密码；web.json 已 gitignore）
cp configs/web.json.example configs/web.json
#    management.host 填业务库地址：库跑在本机用 host.docker.internal，
#    远程库填其 IP/域名；密码可写 password 字段或 password_env
docker compose up -d --build        # 打开 http://127.0.0.1:8000
docker compose logs -f web          # 看 web 日志
docker compose down                 # 停止（uploads 数据在命名卷，不丢）
docker compose down -v              # 停止并清空数据卷
```

- `./configs` 已 bind 到容器 `/app/configs`：之后改 `configs/web.json`（业务库地址、白名单目录、`max_upload_mb`、`upload_ttl_days` 等）**只需 `docker compose restart web`，无需重建镜像**
- 业务库 schema `g2p` 首次启动自动建表（任务元数据）；导入目标库可以是同一 PostgreSQL/PostGIS（不同 schema/库），主机名直接填外部库地址
- `uploads_dir` 默认相对路径 `uploads` = 容器 `/app/uploads`（命名卷持久化）；需要浏览宿主机目录时把目录加进 `allowed_base_dirs` 并给 `web` 加挂载
- 构建基础镜像可用 `.env` 覆盖：`GDAL_IMAGE=<你的基础镜像>`（默认官方 `ghcr.io/osgeo/gdal:ubuntu-small-3.10.2`）

手动镜像构建（buildx；默认构建当前平台，基础镜像可用 `GDAL_IMAGE` 覆盖）：

```bash
# 1) 构建当前平台镜像（docker compose up --build 内部即此命令）
docker buildx build -t python-gdal-web:latest .

# 2) 指定自定义 GDAL 基础镜像
docker buildx build -t python-gdal-web:latest \
  --build-arg GDAL_IMAGE=<你的基础镜像> .

# 3) 多平台构建并推送（linux/amd64 + linux/arm64，需已建多架构 buildx 构建器）
#    docker buildx create --name multi --use   # 首次：多架构构建器（跨平台需模拟）
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t <registry>/python-gdal-web:latest --push .

# 验证产物
docker run --rm -it python-gdal-web:latest python -m gdb2pg web --help
```

> 说明：`.dockerignore` 已排除源码无关文件（含 `frontend` 构建中间产物、`.env`、上传目录），但保留 `gdb2pg/web/static`（内置前端构建产物）。改了前端需先本地 `npm run build` 再构建镜像。
- 除 `G2P_WEB_CFG`（配置文件路径）外，本地开发也可用环境变量覆盖：`G2P_MGMT_PGURL`（或 `G2P_MGMT_HOST/PORT/DBNAME/USER/PASSWORD/PASSWORD_ENV/SCHEMA`）、`G2P_WEB_HOST/PORT/WORKERS/UPLOADS_DIR/MAX_UPLOAD_MB/ALLOWED_BASE_DIRS`。

### Web 能力清单

| 能力 | 说明 |
| --- | --- |
| 任务 CRUD | 列表（状态/类型/名称筛选+分页）、详情、编辑（仅非运行态）、删除 |
| 运行/取消 | 后台 worker 队列执行；取消为协作式（COPY 循环内响应，单层事务回滚不留残表） |
| 实时进度 | 详情页 1.5s 轮询：状态、图层进度条、逐层日志 |
| dry-run 预览 | 打开 GDB 生成分层计划 + 目标库 PostGIS/表冲突只读检查 |
| 图层选择 | 填写 GDB 路径后自动读取全部图层填入规则表：删行 = 不导入，逐层设导入模式（create/overwrite/append）、目标表名、SRID |
| 数据库检测 | 任务表单「目标数据库」组一键检测：连通性 / PostgreSQL 与 PostGIS 版本 / schema 是否存在 / 延迟；编辑态自动用已保存密码 |
| 数据源管理 | 可复用的目标数据库连接（名称唯一、密码脱敏）；任务导入时「从数据源选择」自动填入并微调，保存为独立副本，改数据源不影响已保存任务 |
| GDB 来源 | 服务器本地路径（目录浏览，限白名单）或上传 zip（自动解压定位 `.gdb`，zip-slip 防护 + 大小限制） |
| 上传去重与清理 | zip 按内容 MD5 去重（重复上传直接复用已有解压）；超过 TTL 且未被任务引用的上传由后台线程自动清理（`upload_ttl_days`，0=禁用） |
| 密码处理 | 推荐 `password_env` 环境变量；接口一律脱敏为 `***`，编辑时哨兵保留旧值 |

### Web API 摘要

- `GET /api/task-types` —— 已注册类型 + `form_schema`（驱动动态表单）
- `POST/GET/PUT/DELETE /api/tasks[/{id}]` —— 任务 CRUD；`POST /api/tasks/{id}/run|cancel`
- `GET /api/tasks/{id}/logs?after_id=N` —— 增量日志
- `POST /api/tasks/preview` `{type, config}` —— dry-run 预览（类型分派）
- `POST /api/database/test` `{database, task_id?}` —— 目标数据库连接检测
- `GET/POST /api/datasources`、`GET/PUT/DELETE /api/datasources/{id}` —— 数据源管理（密码脱敏，`"***"` 哨兵保留旧值）
- `POST /api/gdb/layers` `{gdb}` —— 读取 GDB 图层清单（自动填充图层选择）
- `GET /api/gdb/browse?path=` / `POST /api/uploads` —— 目录浏览 / zip 上传（MD5 去重，返回 `cached` 标记）

### 测试（需本地业务库，scripts/dev_db.sh）

```bash
python tests/test_gdb_reader.py                # GDB 读取层：打开/元数据/要素迭代/几何探测/SRID 注入
python tests/test_gdb_reader.py --sample       # 无 GDB 时自动生成样例 GDB 跑完整用例
G2P_TEST_GDB=/path/to/xxx.gdb python tests/test_gdb_reader.py   # 或指定真实 GDB
python tests/test_schema_mapper.py             # 模式映射/清洗（纯函数，不连库）
python tests/test_task_store.py                # 业务库存取层
python tests/test_tasks_api.py                 # API 全链路（假类型）
G2P_TEST_GDB=/path/to/xxx.gdb python tests/test_web_e2e.py  # 真实 GDB 端到端
```

> 前端类型检查/构建：`cd frontend && npm run build`（内含 `vue-tsc --noEmit`）；开发模式 `npm run dev`（5173，`/api` 已代理到 8000）。

---

## 环境与依赖（换机开发指南）

代码不依赖任何固定路径/架构，只需准备以下运行时：

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| Python | 3.11（3.10+ 均可） | 运行环境 |
| GDAL（`osgeo`，含 OGR） | ≥ 3.6 | 读取 GDB |
| psycopg (v3) | ≥ 3.1 | 连接 PostgreSQL/PostGIS |
| fastapi / uvicorn / python-multipart | 最新 | Web 服务（Part 3，可选） |
| httpx | 最新 | Web API 测试（可选） |
| Node.js | ≥ 18 | 前端构建（仅改前端时需要） |
| PostgreSQL + PostGIS 服务 | 任意 | 本地验证库（`scripts/dev_db.sh`） |

**安装方式：conda-forge（推荐，唯一保证 GDAL 全平台可用的方式）**

> 注意：PyPI 的 gdal **没有 Apple Silicon (arm64) 的 wheel**，M 系列 Mac 上不要用 pip 装 GDAL。

```bash
# 1) 安装 Miniconda（任一平台）：https://docs.conda.io/en/latest/miniconda.html
#    （国内网络不佳时可配置 conda 镜像或 HTTP 代理）

# 2) 创建项目环境（conda-forge 通道，路径固定为项目内 .conda，不入库）
conda create -p ./.conda -c conda-forge --override-channels -y \
  python=3.11 gdal "psycopg>=3" fastapi uvicorn python-multipart httpx

# 3) 本地验证库所需（可选，Part 2 的 dev_db.sh 需要）
conda install -p ./.conda -c conda-forge --override-channels -y postgresql postgis

# 4) 使用（两种方式等价）
conda activate ./.conda && python -m gdb2pg --version
# 或直接调用：.conda/bin/python -m gdb2pg --version
```

验证环境：`python -c "from osgeo import gdal, ogr; print(gdal.__version__)"`

### VSCode 开发（.vscode/ 已随仓库提供）

- 安装提示的扩展（Python / debugpy / Vue-official）后，**F5 即启动 Web 服务调试**（调试器内可打断点看任务执行）；
  另有「导入 dry-run / 正式导入 / 调试当前测试文件」三个调试配置。
- 调试启动前确保业务库可连：连接改 `configs/web.dev.json` 的 `management` 段；
  密码填在项目根 `.env`（`G2P_MGMT_PGPASSWORD=xxx`，该文件已 gitignore 不入库）。
- 「任务：运行任务」里有 `dev_web`（起本地库+Web）、前端构建/开发服务器入口；
  解释器已固定为 `.conda/bin/python`，无需手动激活环境。

> 前端构建产物（`gdb2pg/web/static/`）与 Python/Node 环境均不入库（见 `.gitignore`），换机后按本节重装即可，无需改动任何代码。

## 注意事项

- `OpenFileGDB` 驱动**只读**，无需 ESRI 许可；若 GDB 用了较新的压缩/加密特性，个别图层可能打不开；