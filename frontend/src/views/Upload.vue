<template>
  <div class="rw-page">
    <WorkbenchTopbar :title="t('upload.title')" :meta="t('upload.recentMeta', { count: recentUploads.length })">
      <template #actions>
        <button type="button" class="rw-btn-secondary" @click="$router.push('/logs')">
          <el-icon><Document /></el-icon>
          <span>{{ t('upload.goToLogs') }}</span>
        </button>
      </template>
    </WorkbenchTopbar>

    <div class="rw-page-scroll">
      <div class="upload-page">
        <!-- 页面标题 -->
        <div class="page-header">
          <p class="page-kicker">LOG INGEST</p>
          <h1 class="text-2xl font-bold text-gray-900 mb-2">{{ t('upload.pageTitle') }}</h1>
          <p class="text-gray-600">{{ t('upload.pageDesc') }}</p>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <!-- 上传区域 -->
      <div class="upload-section">
        <el-card>
          <template #header>
            <div class="flex items-center space-x-2">
              <el-icon class="text-blue-600">
                <Upload />
              </el-icon>
              <span class="font-medium">{{ t('upload.cardUpload') }}</span>
            </div>
          </template>

          <div class="upload-area">
            <el-upload
              ref="uploadRef"
              :auto-upload="false"
              :on-change="handleFileChange"
              :on-remove="handleFileRemove"
              :before-upload="beforeUpload"
              :file-list="fileList"
              :accept="acceptedLogFileTypes"
              drag
              multiple
              class="upload-dragger"
            >
              <div class="upload-content">
                <el-icon class="upload-icon" size="48">
                  <UploadFilled />
                </el-icon>
                <div class="upload-text">
                  <p class="text-lg font-medium text-gray-700">
                    {{ t('upload.dragText') }}
                    <span class="text-blue-600">{{ t('upload.dragClickHint') }}</span>
                  </p>
                  <p class="text-sm text-gray-500 mt-2">
                    {{ t('upload.formatHint') }}
                  </p>
                </div>
              </div>
            </el-upload>

            <!-- 上传按钮 -->
            <div class="upload-actions mt-4">
              <el-button
                type="primary"
                size="large"
                :disabled="fileList.length === 0 || uploading"
                :loading="uploading"
                @click="handleUpload"
                class="w-full"
              >
                <el-icon class="mr-1" v-if="!uploading">
                  <Upload />
                </el-icon>
                {{ uploading ? t('upload.uploading') : t('upload.uploadBtn', { count: fileList.length }) }}
              </el-button>
            </div>
          </div>
        </el-card>

        <!-- 上传说明 -->
        <el-card class="mt-6">
          <template #header>
            <div class="flex items-center space-x-2">
              <el-icon class="text-green-600">
                <InfoFilled />
              </el-icon>
              <span class="font-medium">{{ t('upload.cardTips') }}</span>
            </div>
          </template>

          <div class="upload-tips">
            <ul class="space-y-2 text-sm text-gray-600">
              <li class="flex items-start space-x-2">
                <el-icon class="text-green-500 mt-0.5" size="14">
                  <Check />
                </el-icon>
                <span>{{ t('upload.tip1') }}</span>
              </li>
              <li class="flex items-start space-x-2">
                <el-icon class="text-green-500 mt-0.5" size="14">
                  <Check />
                </el-icon>
                <span>{{ t('upload.tip2') }}</span>
              </li>
              <li class="flex items-start space-x-2">
                <el-icon class="text-green-500 mt-0.5" size="14">
                  <Check />
                </el-icon>
                <span>{{ t('upload.tip3') }}</span>
              </li>
              <li class="flex items-start space-x-2">
                <el-icon class="text-green-500 mt-0.5" size="14">
                  <Check />
                </el-icon>
                <span>{{ t('upload.tip4') }}</span>
              </li>
            </ul>
          </div>
        </el-card>
      </div>

      <!-- 上传进度和历史 -->
      <div class="progress-section">
        <!-- 上传进度 -->
        <el-card v-if="uploadProgress.length > 0">
          <template #header>
            <div class="flex items-center space-x-2">
              <el-icon class="text-blue-600">
                <Loading />
              </el-icon>
              <span class="font-medium">{{ t('upload.cardProgress') }}</span>
            </div>
          </template>

          <div class="progress-list space-y-4">
            <div
              v-for="item in uploadProgress"
              :key="item.file.name"
              class="progress-item"
            >
              <div class="flex items-center justify-between mb-2">
                <span class="text-sm font-medium text-gray-700 truncate flex-1">
                  {{ item.file.name }}
                </span>
                <span class="text-sm text-gray-500 ml-2">
                  {{ item.progress }}%
                </span>
              </div>
              <el-progress
                :percentage="item.progress"
                :status="getProgressStatus(item.status)"
                :stroke-width="6"
              />
              <div v-if="item.error" class="text-xs text-red-500 mt-1">
                {{ item.error }}
              </div>
            </div>
          </div>
        </el-card>

        <!-- 最近上传 -->
        <el-card :class="{ 'mt-6': uploadProgress.length > 0 }">
          <template #header>
            <div class="flex items-center justify-between">
              <div class="flex items-center space-x-2">
                <el-icon class="text-green-600">
                  <Document />
                </el-icon>
                <span class="font-medium">{{ t('upload.cardRecent') }}</span>
              </div>
              <el-button
                type="text"
                size="small"
                @click="$router.push('/logs')"
              >
                {{ t('upload.viewAll') }}
              </el-button>
            </div>
          </template>

          <div v-if="recentUploads.length > 0" class="recent-uploads space-y-3">
            <div
              v-for="log in recentUploads"
              :key="log.id"
              class="recent-item flex items-center justify-between p-3 bg-gray-50 rounded-lg"
            >
              <div class="recent-item-main flex items-center space-x-3">
                <el-icon class="text-blue-600">
                  <Document />
                </el-icon>
                <div>
                  <p class="text-sm font-medium text-gray-900">
                    {{ log.filename }}
                  </p>
                  <p class="text-xs text-gray-500">
                    {{ formatDateTime(log.upload_time || log.created_at) }}
                  </p>
                </div>
              </div>
              <div class="recent-item-actions flex items-center space-x-2">
                <el-tag :type="getStatusColor(log.status)" size="small">
                  {{ getStatusText(log.status) }}
                </el-tag>
                <el-button
                  type="text"
                  size="small"
                  @click="$router.push(`/log/${log.id}`)"
                >
                  {{ t('upload.view') }}
                </el-button>
              </div>
            </div>
          </div>
          <div v-else class="text-center py-8 text-gray-500">
            <el-icon size="48" class="mb-2">
              <Document />
            </el-icon>
            <p>{{ t('upload.empty') }}</p>
          </div>
        </el-card>
      </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import type { UploadFile, UploadFiles, UploadInstance } from 'element-plus'
import { useLogStore } from '../stores/logs'
import { useAppStore } from '../stores/app'
import { formatDateTime, getStatusColor, getStatusText } from '../utils'
import type { UploadFile as CustomUploadFile } from '../types'
import WorkbenchTopbar from '@/layouts/WorkbenchTopbar.vue'

const { t } = useI18n()
import {
  Upload,
  UploadFilled,
  InfoFilled,
  Check,
  Loading,
  Document,
} from '@element-plus/icons-vue'

const logStore = useLogStore()
const appStore = useAppStore()

const uploadRef = ref<UploadInstance>()
const fileList = ref<UploadFile[]>([])
const uploading = ref(false)
const uploadProgress = ref<CustomUploadFile[]>([])
const supportedLogFileSuffixes = [
  '.log',
  '.txt',
  '.out',
  '.err',
  '.trace',
  '.json',
  '.xml',
  '.csv',
  '.tsv',
  '.zip',
  '.tar',
  '.tgz',
  '.tar.gz',
  '.tar.bz2',
  '.tar.xz',
  '.7z',
  '.rar',
  '.pcap',
  '.pcap0',
]
const acceptedLogFileTypes = supportedLogFileSuffixes.join(',')

const recentUploads = computed(() => {
  return logStore.logs.slice(0, 5)
})

// 文件变化处理
const handleFileChange = (_file: UploadFile, files: UploadFiles) => {
  fileList.value = files
}

// 文件移除处理
const handleFileRemove = (_file: UploadFile, files: UploadFiles) => {
  fileList.value = files
}

// 上传前验证
const beforeUpload = (file: File) => {
  const fileName = file.name.toLowerCase()

  // 检查是否为支持的格式
  const isValidType = supportedLogFileSuffixes.some(type => fileName.endsWith(type))

  if (!isValidType) {
    ElMessage.error(t('upload.unsupportedType'))
    return false
  }

  return true
}

// 获取进度状态
const getProgressStatus = (status: string) => {
  const statusMap: Record<string, any> = {
    pending: undefined,
    uploading: undefined,
    success: 'success',
    error: 'exception',
  }
  return statusMap[status]
}

// 处理上传
const handleUpload = async () => {
  if (fileList.value.length === 0) {
    ElMessage.warning(t('upload.selectFirst'))
    return
  }

  uploading.value = true
  uploadProgress.value = fileList.value.map((file: UploadFile) => ({
    file: file.raw!,
    progress: 0,
    status: 'pending',
  }))

  try {
    for (let i = 0; i < uploadProgress.value.length; i++) {
      const item = uploadProgress.value[i]
      item.status = 'uploading'

      try {
        await logStore.uploadLog(item.file, (progress: number) => {
          item.progress = progress
        })

        item.status = 'success'
        item.progress = 100

        appStore.showNotification({
          title: t('upload.uploadSuccess'),
          message: t('upload.uploadSuccessMsg', { filename: item.file.name }),
          type: 'success',
        })
      } catch (error) {
        item.status = 'error'
        item.error = t('upload.statusError')

        appStore.showNotification({
          title: t('upload.uploadFail'),
          message: t('upload.uploadFailMsg', { filename: item.file.name }),
          type: 'error',
        })
      }
    }

    // 清空文件列表
    fileList.value = []
    uploadRef.value?.clearFiles()

    // 延迟清空进度
    setTimeout(() => {
      uploadProgress.value = []
    }, 3000)

  } finally {
    uploading.value = false
  }
}

onMounted(() => {
  // 获取最近上传的文件
  logStore.fetchLogs({ page: 1, per_page: 5 })
})
</script>

<style scoped>
@reference "tailwindcss";

.rw-page {
  min-height: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--rw-canvas, #ffffff);
}

.rw-page-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 24px 28px 36px;
}

.upload-page {
  @apply space-y-6;
}

.page-header {
  @apply mb-8;
}

.page-kicker {
  margin: 0 0 0.35rem;
  color: var(--rw-muted, #999999);
  font-family: var(--rw-mono, monospace);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.rw-btn-secondary {
  height: 34px;
  border-radius: 8px;
  padding: 0 12px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
  background: var(--rw-canvas, #ffffff);
  color: var(--rw-ink, #171717);
  font-size: 13px;
  font-weight: 600;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}

.rw-btn-secondary:hover {
  background: var(--rw-surface-strong, #f0f0f3);
}

:deep(.el-card) {
  border-radius: 8px;
  border-color: var(--rw-hairline, #f0f0f3);
  box-shadow: none;
}

.upload-area {
  @apply space-y-4;
}

.upload-dragger {
  @apply w-full;
}

.upload-content {
  @apply text-center py-12;
}

.upload-icon {
  @apply text-gray-400 mb-4;
}

.upload-text {
  @apply space-y-2;
}

.upload-actions {
  @apply pt-4 border-t;
  border-color: var(--rw-hairline);
}

.upload-tips ul {
  @apply list-none;
}

.progress-item {
  @apply p-4 rounded-lg;
  background-color: var(--rw-canvas-soft);
}

.recent-item {
  @apply transition-colors;
}

.recent-item:hover {
  background-color: var(--rw-surface-strong);
}

@media (max-width: 768px) {
  .rw-page-scroll {
    padding: 18px 16px 28px;
  }

  .upload-content {
    @apply py-8;
  }

  .upload-icon {
    @apply mb-3;
  }

  .upload-text p:first-child {
    font-size: 1rem;
  }

  .recent-item {
    @apply flex-col items-start gap-3;
  }

  .recent-item-main,
  .recent-item-actions {
    width: 100%;
  }

  .recent-item-actions {
    justify-content: space-between;
  }
}
</style>
