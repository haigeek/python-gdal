# -*- coding: utf-8 -*-
"""Web 服务配置：环境变量（G2P_WEB_*）> JSON 配置文件 > 默认值。"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional


def _parse_pg_url(url: str) -> dict:
    """postgresql://user:pass@host:port/dbname 解析为连接字段 dict。"""
    p = urllib.parse.urlparse(url)
    return {
        "host": p.hostname or "127.0.0.1",
        "port": p.port or 5432,
        "dbname": p.path.lstrip("/") or "postgres",
        "user": p.username or "postgres",
        "password": p.password,
    }


@dataclass
class ManagementConfig:
    """业务库连接（任务元数据/日志/进度，纯 Postgres，不要求 PostGIS）。

    与导入目标库（DatabaseConfig，见 model.py）解耦：字段同构但独立，
    避免两个"schema"混用。schema 指业务表所在 schema（默认 g2p）。
    """

    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "postgres"
    user: str = "postgres"
    password: Optional[str] = None
    password_env: Optional[str] = None
    schema: str = "g2p"
    ssl: str = "prefer"

    def resolved_password(self) -> Optional[str]:
        if self.password_env:
            val = os.environ.get(self.password_env)
            if val:
                return val
        return self.password

    def dsn(self) -> str:
        """psycopg 连接串（与 model.DatabaseConfig.dsn 同构）。"""
        pwd = self.resolved_password()
        kv = [
            f"host={self.host}",
            f"port={self.port}",
            f"dbname={self.dbname}",
            f"user={self.user}",
            f"sslmode={self.ssl}",
        ]
        if pwd:
            kv.insert(1, f"password={pwd}")
        return " ".join(kv)


@dataclass
class WebConfig:
    """Web 服务配置。

    - management：业务库连接 + 业务表 schema（schema 默认 g2p）；
    - uploads_dir：zip 上传解压目录（也自动加入目录浏览白名单）；
    - allowed_base_dirs：服务器目录浏览白名单（GDB 本地路径选择）；
    - workers：后台任务 worker 线程数（并发导入数）；
    - upload_ttl_days：uploads 清理 TTL（天），0 = 禁用；超过该时限
      且未被任务引用的上传由后台线程清理。
    """

    management: ManagementConfig = field(default_factory=ManagementConfig)
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1
    uploads_dir: str = "uploads"
    allowed_base_dirs: list = field(default_factory=list)
    max_upload_mb: int = 2048
    upload_ttl_days: int = 7

    # ------------------------------------------------------------ 路径

    def uploads_abs(self) -> str:
        return os.path.realpath(self.uploads_dir)

    def allowed_dirs_abs(self) -> list:
        """目录浏览白名单：uploads_dir + allowed_base_dirs（去重）。"""
        seen, out = set(), []
        for d in [self.uploads_abs()] + [os.path.realpath(x) for x in self.allowed_base_dirs]:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out

    # ------------------------------------------------------------ 加载

    @classmethod
    def from_dict(cls, d: dict) -> "WebConfig":
        cfg = cls()
        if d.get("management"):
            m = dict(d["management"])
            # 兼容旧写法：顶层 management_schema 回填（management.schema 优先）
            if "schema" not in m and d.get("management_schema"):
                m["schema"] = d["management_schema"]
            cfg.management = ManagementConfig(**m)
        for key in ("host", "port", "workers", "uploads_dir", "max_upload_mb",
                    "upload_ttl_days"):
            if d.get(key) is not None:
                setattr(cfg, key, d[key])
        if d.get("allowed_base_dirs") is not None:
            cfg.allowed_base_dirs = list(d["allowed_base_dirs"])
        return cfg

    @classmethod
    def from_file(cls, path: str) -> "WebConfig":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    @classmethod
    def from_env(cls, base: Optional["WebConfig"] = None) -> "WebConfig":
        """环境变量覆盖（G2P_MGMT_* / G2P_WEB_*）。"""
        cfg = base or cls()
        url = os.environ.get("G2P_MGMT_PGURL")
        if url:
            cfg.management = ManagementConfig(**_parse_pg_url(url))
        mgmt = {}
        if os.environ.get("G2P_MGMT_HOST"): mgmt["host"] = os.environ["G2P_MGMT_HOST"]
        if os.environ.get("G2P_MGMT_PORT"): mgmt["port"] = int(os.environ["G2P_MGMT_PORT"])
        if os.environ.get("G2P_MGMT_DBNAME"): mgmt["dbname"] = os.environ["G2P_MGMT_DBNAME"]
        if os.environ.get("G2P_MGMT_USER"): mgmt["user"] = os.environ["G2P_MGMT_USER"]
        if os.environ.get("G2P_MGMT_PASSWORD"): mgmt["password"] = os.environ["G2P_MGMT_PASSWORD"]
        if os.environ.get("G2P_MGMT_PASSWORD_ENV"): mgmt["password_env"] = os.environ["G2P_MGMT_PASSWORD_ENV"]
        if os.environ.get("G2P_MGMT_SCHEMA"): mgmt["schema"] = os.environ["G2P_MGMT_SCHEMA"]
        if mgmt:
            base_db = cfg.management
            cfg.management = ManagementConfig(**{**base_db.__dict__, **mgmt})
        if os.environ.get("G2P_WEB_HOST"): cfg.host = os.environ["G2P_WEB_HOST"]
        if os.environ.get("G2P_WEB_PORT"): cfg.port = int(os.environ["G2P_WEB_PORT"])
        if os.environ.get("G2P_WEB_WORKERS"): cfg.workers = int(os.environ["G2P_WEB_WORKERS"])
        if os.environ.get("G2P_WEB_UPLOADS_DIR"): cfg.uploads_dir = os.environ["G2P_WEB_UPLOADS_DIR"]
        if os.environ.get("G2P_WEB_MAX_UPLOAD_MB"): cfg.max_upload_mb = int(os.environ["G2P_WEB_MAX_UPLOAD_MB"])
        if os.environ.get("G2P_WEB_ALLOWED_BASE_DIRS"):
            cfg.allowed_base_dirs = [x.strip() for x in
                                     os.environ["G2P_WEB_ALLOWED_BASE_DIRS"].split(",") if x.strip()]
        if os.environ.get("G2P_WEB_UPLOADS_TTL_DAYS"):
            cfg.upload_ttl_days = int(os.environ["G2P_WEB_UPLOADS_TTL_DAYS"])
        return cfg

    @classmethod
    def load(cls, path: Optional[str] = None) -> "WebConfig":
        """加载顺序：JSON 文件（若有）-> 环境变量覆盖。"""
        cfg = cls.from_file(path) if path else cls()
        return cls.from_env(cfg)