import { api } from './client'
import type { DataSourceItem } from '../types'

export const listDataSources = () => api.get<DataSourceItem[]>('/api/datasources')

export const createDataSource = (payload: {
  name: string
  config: Record<string, unknown>
}) => api.post<DataSourceItem>('/api/datasources', payload)

export const getDataSource = (id: number) =>
  api.get<DataSourceItem>(`/api/datasources/${id}`)

export const updateDataSource = (
  id: number,
  payload: { name?: string; config?: Record<string, unknown> },
) => api.put<DataSourceItem>(`/api/datasources/${id}`, payload)

export const deleteDataSource = (id: number) =>
  api.del(`/api/datasources/${id}`)