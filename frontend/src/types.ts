// 与后端 /api 契约对应的类型（后端 form_schema/sanitize 等定义见 gdb2pg/tasks）

export type TaskStatus =
  | 'draft'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export type FieldType =
  | 'text'
  | 'int'
  | 'bool'
  | 'password'
  | 'enum'
  | 'table'
  | 'gdb_path'

export interface FieldDef {
  key: string
  label: string
  type: FieldType
  default?: unknown
  options?: string[]
  help?: string
  placeholder?: string
}

export interface TableColumnDef {
  key: string
  label: string
  type: 'text' | 'int' | 'enum'
  options?: string[]
}

export interface FormGroup {
  key: string
  label: string
  single?: boolean // true: 该组直接映射 config[key] 为单个值（如 gdb）
  test?: boolean // true: 组标题栏显示「检测」按钮（如目标数据库连接检测）
  actions?: { key: string; label: string }[] // 组标题栏自定义按钮（如「从数据源选择」）
  fields: Array<FieldDef & { columns?: TableColumnDef[] }>
}

export interface FormSchema {
  groups: FormGroup[]
}

export interface TaskTypeInfo {
  type: string
  label: string
  form_schema: FormSchema
}

export interface TaskProgress {
  layer_index?: number
  layer_total?: number
  source?: string
  rows?: number
}

export interface TaskResult {
  ok: { source: string; rows: number }[]
  skipped: { source: string; message: string }[]
  failed: { message: string }[]
}

export interface TaskItem {
  id: number
  type: string
  name: string
  status: TaskStatus
  config: Record<string, unknown>
  progress: TaskProgress | null
  result: TaskResult | null
  error: string | null
  created_at: string | null
  updated_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface LogEntry {
  id: number
  ts: string
  message: string
}

export interface PreviewLayer {
  source: string
  table: string
  schema: string
  mode: string
  feature_count: number
  geometry: string | null
  srid: number | null
  columns: { src: string; dst: string; pg: string }[]
  issues: string[]
  errors: string[]
}

export interface PreviewData {
  gdb: string
  layers: PreviewLayer[]
  postgis: string | null
  db_checks: string[]
  error: string | null
  db_error: string | null
}

export interface BrowseEntry {
  name: string
  is_dir: boolean
  is_gdb: boolean
  size?: number
}

export interface BrowseData {
  path: string
  parent: string | null
  entries: BrowseEntry[]
}

export interface UploadData {
  gdb_path: string
  layers: string[]
  note: string
  cached?: boolean // true: 相同 zip 已上传过，直接复用已有解压
}

export interface DatabaseTestResult {
  connected: boolean
  server_version: string
  postgis_version: string | null
  schema: string
  schema_exists: boolean
  latency_ms: number
}

// 数据源（可复用的目标数据库连接；config 为平铺连接字段，密码已脱敏）
export interface DataSourceItem {
  id: number
  name: string
  config: Record<string, unknown>
  created_at: string | null
  updated_at: string | null
}

// GDB 图层摘要（自动填充「图层选择」）
export interface GdbLayerInfo {
  source: string
  feature_count: number
  geometry: string | null
  srid: number | null
  fields?: string[]
}