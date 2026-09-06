-- gdb2pg Web 业务库 DDL（幂等，可重复执行）
-- schema 名由 Web 配置指定（默认 g2p），此处用 __SCHEMA__ 占位符，
-- web/db.py 启动时读取并替换后逐条执行。
--
-- 说明：业务库存任务元数据/日志/进度，是纯 Postgres，不要求 PostGIS；
-- 导入目标库由每个任务自己的 config.database 决定，与本库解耦。

CREATE SCHEMA IF NOT EXISTS __SCHEMA__;

CREATE TABLE IF NOT EXISTS __SCHEMA__.task (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type        text NOT NULL,                        -- 任务类型标识，如 'gdb_import'
    name        text NOT NULL,                        -- 任务名称
    status      text NOT NULL DEFAULT 'draft',        -- draft|queued|running|succeeded|failed|cancelled
    config      jsonb NOT NULL DEFAULT '{}',          -- 类型私有的完整配置（如导入的 ImportConfig dict）
    progress    jsonb,                                -- 轻量进度 {current,total,source,rows}
    result      jsonb,                                -- 成功结果（如导入逐层统计）
    error       text,                                 -- 失败原因
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz,
    started_at  timestamptz,
    finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_task_status
    ON __SCHEMA__.task (status, created_at DESC);

CREATE TABLE IF NOT EXISTS __SCHEMA__.task_log (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id bigint NOT NULL REFERENCES __SCHEMA__.task (id) ON DELETE CASCADE,
    ts      timestamptz NOT NULL DEFAULT now(),
    message text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_log_task
    ON __SCHEMA__.task_log (task_id, id);

-- 数据源管理：可复用的目标数据库连接（名称唯一）。
-- 任务 config.database 可携带 datasource_id 引用某个数据源，保存时后端
-- 用数据源的真实密码补齐（"***" 哨兵替换），并保留引用便于审计；
-- 之后修改数据源不影响已保存任务（任务已持有独立配置）。
CREATE TABLE IF NOT EXISTS __SCHEMA__.datasource (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL UNIQUE,                   -- 数据源名称（唯一）
    config      jsonb NOT NULL DEFAULT '{}',            -- 连接字段平铺: host/port/dbname/user/password/password_env/schema/ssl
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz
);

-- 上传记录（zip MD5 去重 + 定时清理）：
-- 相同内容的 zip 直接复用已有解压（gdb_path），避免重复占用空间；
-- 超过 TTL 且未被任何任务引用的记录由清理线程删除 gdb 目录与本行。
CREATE TABLE IF NOT EXISTS __SCHEMA__.upload (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    md5          char(32) NOT NULL UNIQUE,              -- zip 内容 MD5
    gdb_path     text NOT NULL,                         -- 已解压的 .gdb 绝对路径
    size_bytes   bigint NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz NOT NULL DEFAULT now()
);