<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElButton, ElTag } from 'element-plus'
import { api } from './api'
import { displayTime, localDateTime, type Items, useOps } from './ops'

interface Config { title: string; group_id: string | null; starts_at: string; ends_at: string; rule: string; checkin: boolean; question: string | null; allow_makeup: boolean; allow_edit: boolean }
interface Activity { id: string; title: string; version: number; effective_status: string; starts_at: string; ends_at: string; group_id: string | null; registrations: number; config: Config }
interface Detail { activity: Activity; progress: Record<string, number>; versions: { version: number; created_at: string; snapshot: Activity }[];
  participation: { id: string; profile_id: string | null; action: string; period: string; activity_version: number; created_at: string }[] }
const props = defineProps<{ csrf: string }>()
const { busy, error, notice, run, save } = useOps(() => props.csrf)
const rows = ref<Activity[]>([])
const detail = ref<Detail | null>(null)
const editing = ref<Activity | null>(null)
const blank = () => ({ title: '', group_id: '', starts_at: '', ends_at: '', rule: '', checkin: false, question: '', allow_makeup: false, allow_edit: false })
const form = ref(blank())
const started = () => editing.value !== null && editing.value.effective_status !== 'draft' && new Date(editing.value.starts_at) <= new Date()
const states: Record<string, string> = { draft: '草稿', scheduled: '待开始', open: '进行中', paused: '已暂停', ended: '已结束', cancelled: '已取消' }
const actions: Record<string, string> = { publish: '发布', pause: '暂停', resume: '恢复', cancel: '取消活动', register: '报名', checkin: '打卡', survey: '问卷' }
const available: Record<string, string[]> = { draft: ['publish', 'cancel'], scheduled: ['pause', 'cancel'], open: ['pause', 'cancel'], paused: ['resume', 'cancel'] }
const inputTime = (value: string) => {
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}
async function load() { rows.value = (await api<Items<Activity>>('/activities')).items }
async function open(id: string) { detail.value = await api<Detail>('/activities/' + id) }
function edit(activity: Activity) {
  editing.value = activity
  const config = activity.config
  form.value = { ...config, group_id: config.group_id ?? '', question: config.question ?? '',
    starts_at: inputTime(config.starts_at), ends_at: inputTime(config.ends_at) }
}
async function submit() {
  await run(async () => {
    const data = { ...form.value, group_id: form.value.group_id || null, question: form.value.question || null,
      starts_at: started() ? editing.value?.starts_at : localDateTime(form.value.starts_at), ends_at: localDateTime(form.value.ends_at) }
    const item = editing.value
    const saved = await save<Activity>(item ? '/activities/' + item.id : '/activities',
      item ? { action: 'edit', expected_version: item.version, config: data } : data)
    editing.value = null
    form.value = blank()
    await load()
    await open(saved.id)
  }, '活动规则已保存。')
}
async function change(item: Activity, action: string) {
  if (action === 'cancel' && !window.confirm('确认取消该活动？')) return
  await run(async () => {
    await save('/activities/' + item.id, { action, expected_version: item.version })
    await load()
    if (detail.value?.activity.id === item.id) await open(item.id)
  }, '活动状态已更新。')
}
onMounted(() => run(load))
</script>

<template>
  <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="notice" role="status" class="notice">{{ notice }}</p>
  <section class="panel">
    <header class="panel-head"><h2>活动列表</h2><ElButton @click="run(load)">刷新</ElButton></header>
    <table><thead><tr><th>活动</th><th>有效状态</th><th>时间（上海）</th><th>报名</th><th>操作</th></tr></thead><tbody>
      <tr v-for="item in rows" :key="item.id">
        <td><button class="text-button" @click="run(() => open(item.id))">{{ item.title }}</button><p v-if="item.group_id" class="muted small">{{ item.group_id }}</p></td>
        <td><ElTag size="small">{{ states[item.effective_status] }}</ElTag></td>
        <td>{{ displayTime(item.starts_at) }} 至 {{ displayTime(item.ends_at) }}</td><td>{{ item.registrations }}</td>
        <td><div class="inline-controls"><ElButton v-if="available[item.effective_status]" size="small" @click="edit(item)">编辑规则</ElButton>
          <ElButton v-for="action in available[item.effective_status]" :key="action" size="small" :disabled="busy" @click="change(item, action)">{{ actions[action] }}</ElButton></div></td>
      </tr><tr v-if="!rows.length"><td colspan="5" class="empty">暂无活动，可先建立草稿。</td></tr>
    </tbody></table>
  </section>
  <section class="panel section">
    <h2>{{ editing ? '编辑活动规则' : '新建活动草稿' }}</h2>
    <form class="form-grid" @submit.prevent="submit">
      <label>活动名称<input v-model="form.title" required maxlength="80" /></label>
      <label>限定群标识（可选）<input v-model="form.group_id" maxlength="255" /></label>
      <label>开始时间（本机时区）<input v-model="form.starts_at" type="datetime-local" :disabled="started()" required /></label>
      <label>结束时间（本机时区）<input v-model="form.ends_at" type="datetime-local" required /></label>
      <label>活动规则<textarea v-model="form.rule" required rows="3" maxlength="2000" /></label>
      <label>问卷问题（可选）<textarea v-model="form.question" rows="3" maxlength="500" /></label>
      <div class="inline-controls form-wide">
        <label class="check"><input v-model="form.checkin" type="checkbox" /> 开放打卡</label>
        <label class="check"><input v-model="form.allow_makeup" type="checkbox" :disabled="started() || !form.checkin" /> 允许补打卡</label>
        <label class="check"><input v-model="form.allow_edit" type="checkbox" :disabled="started() || !form.question" /> 允许修改问卷</label>
      </div>
      <div class="actions"><ElButton type="primary" native-type="submit" :loading="busy">保存规则</ElButton>
        <ElButton v-if="editing" @click="editing = null; form = blank()">取消编辑</ElButton></div>
    </form>
  </section>
  <section v-if="detail" class="panel section" aria-label="活动详情">
    <h2>{{ detail.activity.title }} · 参与进度</h2>
    <p>{{ detail.activity.config.rule }}</p>
    <p>报名 {{ detail.progress.register }} · 打卡 {{ detail.progress.checkin }} · 问卷 {{ detail.progress.survey }}</p>
    <table><thead><tr><th>会员</th><th>动作</th><th>周期</th><th>活动版本</th><th>提交时间</th></tr></thead><tbody>
      <tr v-for="record in detail.participation" :key="record.id"><td>{{ record.profile_id || '已删除画像' }}</td>
        <td>{{ actions[record.action] }}</td><td>{{ record.period || '—' }}</td><td>v{{ record.activity_version }}</td><td>{{ displayTime(record.created_at) }}</td></tr>
      <tr v-if="!detail.participation.length"><td colspan="5" class="empty">暂无参与记录。</td></tr>
    </tbody></table>
    <h3 class="ops-heading">规则版本</h3>
    <details v-for="version in detail.versions" :key="version.version">
      <summary>v{{ version.version }} · {{ displayTime(version.created_at) }} · {{ states[version.snapshot.effective_status] }}</summary>
      <p>{{ version.snapshot.config.rule }}</p>
    </details>
  </section>
</template>
