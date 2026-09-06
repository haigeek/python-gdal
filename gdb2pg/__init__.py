# -*- coding: utf-8 -*-
"""gdb2pg —— 把 File Geodatabase (.gdb) 导入 PostgreSQL/PostGIS。

读取走 GDAL/OGR（OpenFileGDB 驱动），写入走 psycopg（直连 PostGIS）。
一切行为由一份 JSON 配置驱动，方便后续做可视化配置。
"""

__version__ = "0.2.0"