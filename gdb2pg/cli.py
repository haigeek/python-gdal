# -*- coding: utf-8 -*-
"""命令行入口：python -m gdb2pg ..."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .importer import dry_run, run
from .model import ImportConfig


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gdb2pg",
        description="把 File Geodatabase (.gdb) 导入 PostgreSQL/PostGIS（自动建表+写数据）",
    )
    parser.add_argument("--version", action="version", version=f"gdb2pg {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser("import", help="导入（或 --dry-run 预览）")
    p_import.add_argument("--config", required=True, help="JSON 配置文件路径")
    p_import.add_argument("--dry-run", action="store_true", help="只输出计划与检查，不写库")
    p_import.add_argument("--schema", default=None, help="覆盖配置中的目标 schema")

    p_inspect = sub.add_parser("inspect", help="仅查看 GDB 图层结构（沿用 demo 逻辑）")
    p_inspect.add_argument("--gdb", required=True, help="GDB 路径")
    p_inspect.add_argument("--list-only", action="store_true", help="只列图层名")
    p_inspect.add_argument("--limit", type=int, default=3, help="每层抽样条数")

    p_web = sub.add_parser("web", help="启动 Web 任务管理服务（FastAPI）")
    p_web.add_argument("--config", default=None, help="Web 配置文件（configs/web.json）")
    p_web.add_argument("--host", default=None, help="监听地址（覆盖配置/env）")
    p_web.add_argument("--port", type=int, default=None, help="监听端口（覆盖配置/env）")

    args = parser.parse_args(argv)

    if args.cmd == "import":
        cfg = ImportConfig.from_json(args.config)
        if args.schema:
            cfg.database.schema = args.schema
        if args.dry_run:
            dry_run(cfg)
            return 0
        return _run_cli(cfg)

    if args.cmd == "inspect":
        from . import gdb_reader
        ds = gdb_reader.open_gdb(args.gdb)
        names = gdb_reader.list_layers(ds)
        print(f"图层数量: {len(names)}")
        for i, n in enumerate(names):
            print(f"  [{i}] {n}")
        if args.list_only:
            return 0
        for n in names[:5]:
            meta = gdb_reader.layer_meta(ds.GetLayerByName(n))
            print(f"\n【图层】{n}  type={meta['geom_name']}  count={meta['feature_count']}")
            for f in meta["fields"]:
                print(f"    {f['name']:<28} {f['type_name']} 宽={f['width']}")
        return 0

    if args.cmd == "web":
        from .web.app import create_app
        from .web.config import WebConfig
        import uvicorn

        # 配置文件优先级：CLI --config > 环境变量 G2P_WEB_CFG > 纯环境变量
        cfg = WebConfig.load(args.config or os.environ.get("G2P_WEB_CFG") or None)
        if args.host:
            cfg.host = args.host
        if args.port:
            cfg.port = args.port
        print(f"[web] 业务库 {cfg.management.host}:{cfg.management.port}/{cfg.management.dbname}"
              f" schema={cfg.management.schema}  workers={cfg.workers}")
        uvicorn.run(create_app(config=cfg), host=cfg.host, port=cfg.port, log_level="info")
        return 0

    parser.print_help()
    return 1


def _run_cli(cfg):
    try:
        run(cfg)
        return 0
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())