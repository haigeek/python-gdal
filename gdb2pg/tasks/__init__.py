# -*- coding: utf-8 -*-
"""gdb2pg 任务管理框架。

对外暴露：TaskType / TaskContext / TaskCancelled / TaskStore / TaskManager /
TASK_REGISTRY / register / get_task_type / registered_types。

内置 gdb_import / shp_import 在对应模块中，导入本包时自动注册。
"""

from __future__ import annotations

from .base import (TASK_REGISTRY, TaskCancelled, TaskContext, TaskType,
                   get_task_type, register, registered_types)
from .manager import TaskManager
from .store import (ALL_STATUS, EDITABLE, RUNNABLE, TERMINAL, STATUS_CANCELLED,
                    STATUS_DRAFT, STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING,
                    STATUS_SUCCEEDED, TaskStore)

# 注册内置任务类型（副作用：导入内置任务模块）
from . import gdb_import  # noqa: E402,F401
from . import shp_import  # noqa: E402,F401

__all__ = [
    "TASK_REGISTRY", "TaskCancelled", "TaskContext", "TaskType",
    "TaskStore", "TaskManager", "register", "get_task_type", "registered_types",
    "ALL_STATUS", "EDITABLE", "RUNNABLE", "TERMINAL",
    "STATUS_CANCELLED", "STATUS_DRAFT", "STATUS_FAILED", "STATUS_QUEUED",
    "STATUS_RUNNING", "STATUS_SUCCEEDED",
]
