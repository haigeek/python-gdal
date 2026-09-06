import { createRouter, createWebHashHistory } from 'vue-router'
import TaskListView from '../views/TaskListView.vue'
import TaskFormView from '../views/TaskFormView.vue'
import TaskDetailView from '../views/TaskDetailView.vue'
import DataSourceView from '../views/DataSourceView.vue'

// hash 路由：static 托管无需后端 rewrite
export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/tasks' },
    { path: '/tasks', component: TaskListView },
    { path: '/tasks/new', component: TaskFormView },
    { path: '/tasks/:id(\\d+)', component: TaskDetailView },
    { path: '/tasks/:id(\\d+)/edit', component: TaskFormView },
    { path: '/datasources', component: DataSourceView },
  ],
})