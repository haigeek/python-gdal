# -*- coding: utf-8 -*-
"""Web API 请求/响应模型（pydantic v2）。

响应不强制建模：路由返回可 JSON 化的 dict（datetime 由 FastAPI 自动编码），
统一包装为 {ok: True, data: ...} / {ok: False, error: ...}。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    type: str = Field(..., description="任务类型标识，来自 /api/task-types")
    name: str = Field("", max_length=200)
    config: dict = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    config: Optional[dict] = None


class PreviewRequest(BaseModel):
    type: str
    config: dict = Field(default_factory=dict)


class DatabaseTestRequest(BaseModel):
    """目标数据库连接检测。

    database：与任务 config.database 同构的连接配置；
    task_id：可选。编辑场景下前端只有脱敏密码 "***"，传入任务 ID 由后端
    从库中取真实密码参与检测；不传则要求 database.password 为真实值。
    """

    database: dict
    task_id: Optional[int] = None


class DataSourceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="数据源名称（唯一）")
    config: dict = Field(default_factory=dict, description="连接字段平铺: host/port/dbname/user/password/password_env/schema/ssl")


class DataSourceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    config: Optional[dict] = None


class GdbLayersRequest(BaseModel):
    """读取 GDB 图层清单（用于新建任务自动填充「图层选择」）。"""

    gdb: str = Field(..., min_length=1)