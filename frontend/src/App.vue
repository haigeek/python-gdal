<template>
  <el-container class="layout">
    <el-aside :width="collapsed ? '64px' : '200px'" class="app-aside">
      <div class="brand" :class="{ collapsed }" @click="$router.push('/tasks')">
        <span v-if="!collapsed" class="brand-title">gdb2pg</span>
        <span v-else class="brand-mini">g2p</span>
        <span v-if="!collapsed" class="brand-sub">GDB → PostGIS</span>
      </div>
      <el-menu
        :default-active="$route.path"
        router
        class="side-menu"
        :collapse="collapsed"
        :collapse-transition="false"
        :ellipsis="false"
      >
        <el-menu-item index="/tasks">
          <el-icon><List /></el-icon>
          <template #title>任务管理</template>
        </el-menu-item>
        <el-menu-item index="/datasources">
          <el-icon><Coin /></el-icon>
          <template #title>数据源管理</template>
        </el-menu-item>
      </el-menu>
      <div class="aside-footer">
        <el-button text class="collapse-btn" :title="collapsed ? '展开菜单' : '收起菜单'" @click="collapsed = !collapsed">
          <el-icon :size="18"><component :is="collapsed ? Expand : Fold" /></el-icon>
        </el-button>
      </div>
    </el-aside>
    <el-container>
      <el-header class="app-header">
        <div class="page-title">{{ pageTitle }}</div>
      </el-header>
      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Coin, Expand, Fold, List } from '@element-plus/icons-vue'

const route = useRoute()
const collapsed = ref(false)

const pageTitle = computed(() => {
  if (route.path.startsWith('/datasources')) return '数据源管理'
  if (route.path.startsWith('/tasks')) {
    if (route.path.endsWith('/new')) return '新建任务'
    if (route.path.endsWith('/edit')) return '编辑任务'
    if (/\/tasks\/\d+$/.test(route.path)) return '任务详情'
    return '任务管理'
  }
  return ''
})
</script>

<style>
/* 列表表格字号统一放大（任务列表 / 数据源管理） */
.el-table {
  --el-table-font-size: 15px;
}
.el-table .cell {
  padding-top: 6px;
  padding-bottom: 6px;
}
.layout {
  height: 100vh;
  overflow: hidden;
}
.app-aside {
  background: #1f2d3d;
  display: flex;
  flex-direction: column;
  transition: width 0.2s;
  overflow: hidden;
}
.brand {
  padding: 16px 16px 10px;
  color: #fff;
  cursor: pointer;
  user-select: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  white-space: nowrap;
}
.brand.collapsed {
  align-items: center;
  padding: 16px 0 10px;
}
.brand-title {
  font-size: 17px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.brand-mini {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.brand-sub {
  font-size: 11px;
  color: #8fa3b8;
}
.side-menu {
  --el-menu-bg-color: transparent;
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.1);
  --el-menu-text-color: #cfd8e3;
  --el-menu-active-color: #fff;
  border-right: none;
}
.side-menu :deep(.el-menu-item) {
  margin: 2px 8px;
  border-radius: 6px;
}
.side-menu :deep(.el-menu-item.is-active) {
  background: #3370ff;
  color: #fff;
}
.side-menu.collapse :deep(.el-menu-item) {
  margin: 2px 6px;
}
.aside-footer {
  margin-top: auto;
  padding: 8px;
}
.collapse-btn {
  /* 简单风格：无背景、无悬浮变色，固定浅灰图标 */
  --el-button-bg-color: transparent;
  --el-button-text-color: #8fa3b8;
  --el-button-border-color: transparent;
  --el-button-hover-bg-color: transparent;
  --el-button-hover-text-color: #8fa3b8;
  --el-button-hover-border-color: transparent;
  --el-button-active-bg-color: transparent;
  --el-button-active-text-color: #8fa3b8;
  --el-button-focus-box-shadow: none;
  color: #8fa3b8;
  width: 100%;
  justify-content: center;
  padding: 0;
}
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  flex-shrink: 0;
}
.page-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}
.app-main {
  background: #f5f7fa;
  overflow: auto;
  height: 0;
  flex: 1;
}
</style>