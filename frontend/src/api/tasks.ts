import { api, uploadWithProgress } from './client'
import type {
  BrowseData,
  DatabaseTestResult,
  GdbLayerInfo,
  LogEntry,
  PreviewData,
  TaskItem,
  TaskStatus,
  TaskTypeInfo,
  UploadData,
} from '../types'

export const listTaskTypes = () => api.get<TaskTypeInfo[]>('/api/task-types')

export const createTask = (payload: {
  type: string
  name: string
  config: Record<string, unknown>
}) => api.post<TaskItem>('/api/tasks', payload)

export interface ListTasksParams {
  status?: TaskStatus | ''
  type?: string
  q?: string
  page: number
  pageSize: number
  sortBy?: string
  order?: 'asc' | 'desc'
}

export const listTasks = (params: ListTasksParams) => {
  const qs = new URLSearchParams()
  if (params.status) qs.set('status', params.status)
  if (params.type) qs.set('type', params.type)
  if (params.q) qs.set('q', params.q)
  qs.set('page', String(params.page))
  qs.set('page_size', String(params.pageSize))
  if (params.sortBy) qs.set('sort_by', params.sortBy)
  if (params.order) qs.set('order', params.order)
  return api.get<{ total: number; items: TaskItem[] }>(`/api/tasks?${qs.toString()}`)
}

export const getTask = (id: number) => api.get<TaskItem>(`/api/tasks/${id}`)

export const updateTask = (
  id: number,
  payload: { name?: string; config?: Record<string, unknown> },
) => api.put<TaskItem>(`/api/tasks/${id}`, payload)

export const deleteTask = (id: number) => api.del(`/api/tasks/${id}`)

export const runTask = (id: number) => api.post<TaskItem>(`/api/tasks/${id}/run`)

export const cancelTask = (id: number) => api.post<TaskItem>(`/api/tasks/${id}/cancel`)

export const getLogs = (id: number, afterId = 0) =>
  api.get<{ last_id: number; logs: LogEntry[] }>(
    `/api/tasks/${id}/logs?after_id=${afterId}`,
  )

export const previewTask = (payload: {
  type: string
  config: Record<string, unknown>
}) => api.post<PreviewData>('/api/tasks/preview', payload)

export const testDatabase = (payload: {
  database: Record<string, unknown>
  task_id?: number
}) => api.post<DatabaseTestResult>('/api/database/test', payload)

export const browseDir = (path?: string) =>
  api.get<BrowseData>(
    `/api/gdb/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`,
  )

export const fetchGdbLayers = (gdb: string) =>
  api.post<{ gdb: string; layers: GdbLayerInfo[] }>('/api/gdb/layers', { gdb })

export const uploadZip = (file: File, onProgress?: (pct: number) => void) =>
  uploadWithProgress<UploadData>('/api/uploads', file, onProgress)