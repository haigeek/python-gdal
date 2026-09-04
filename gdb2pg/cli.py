# -*- coding: utf-8 -*-
"""命令行入口：python -m gdb2pg ..."""

from __future__ import annotations

import argparse
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