<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElButton, ElTag } from 'element-plus'
import { api } from './api'
import { displayTime, type Items, useOps } from './ops'

interface Group { connection_id: string; group_id: string; enabled: boolean }
interface Signal { id: string; category: string; status: string; version: number; topic: string; occurred_at: string; matter_id: string | null; preview: string | null; profile_id: string | null }
interface Report { day: string; group_id: string; enabled: boolean; coverage_since: string | null; unresolved: number;
  daily_snapshot: { generated_at: string; summary: { topics: Record<string, number> } } | null;
  service_metrics: { consultations: number; answered: number; handoffs: number; pending: number; mean_resolution_seconds: number | null };
  delivery_counts: { state: string; count: number }[];
  items: { topic: string; count: number; signals: Signal[] }[]; activity_progress: { title: string; action: string; count: number }[] }
const props = defineProps<{ csrf: string }>()
const emit = defineEmits<{ matter: [id: string]; member: [id: string]; correction: [signal: Signal] }>()
const { busy, error, notice, run, save } = useOps(() => props.csrf)
const groups = ref<Group[]>([])
const selected = ref('')
const day = ref(new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date()))
const report = ref<Report | null>(null)
const config = ref({ connection_id: 'fixture-channel', group_id: '', enabled: true })
const changes = ref<Record<string, string>>({})
const groupKey = (group: Group) => JSON.stringify([group.connection_id, group.group_id])
const statuses: Record<string, string> = { open: '待核对', confirmed: '已确认', closed: '已关闭', question: '咨询', complaint: '投诉', interest: '购买信号', register: '报名', checkin: '打卡', survey: '问卷' }

async function load() {
  groups.value = (await api<Items<Group>>('/insights/groups')).items
  if (!selected.value && groups.value[0]) selected.value = groupKey(groups.value[0])
  if (selected.value) await loadReport()
}
async function loadReport() {
  const group = groups.value.find(item => groupKey(item) === selected.value)
  if (!group) return
  const query = new URLSearchParams({ group_id: group.group_id, connection_id: group.connection_id, day: day.value })
  report.value = await api<Report>('/insights?' + query)
}
async function configure(group: Group) {
  await run(async () => {
    await save('/insights/groups', group)
    selected.value = groupKey(group)
    await load()
  }, '群分析许可已更新。')
}
async function review(signal: Signal, action: string) {
  await run(async () => {
    const topic = changes.value[signal.id]
    await save('/insights/signals/' + signal.id, { action, expected_version: signal.version,
      ...(topic && ['correct', 'candidate'].includes(action) ? { topic } : {}) })
    await loadReport()
  }, '信号已处理。')
}
onMounted(() => run(load))
</script>

<template>
  <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="notice" role="status" class="notice">{{ notice }}</p>
  <section class="panel section">
    <h2>群分析范围</h2>
    <form class="form-grid" @submit.prevent="configure(config)">
      <label>渠道<input v-model="config.connection_id" required maxlength="255" /></label>
      <label>群标识<input v-model="config.group_id" required maxlength="255" /></label>
      <label class="check"><input v-model="config.enabled" type="checkbox" /> 允许分析后续收到的消息</label>
      <div class="actions"><ElButton native-type="submit" type="primary" :disabled="busy">保存许可</ElButton></div>
    </form>
  </section>
  <section class="panel section" aria-label="群日报">
    <h2>群日报</h2>
    <form class="form-grid" @submit.prevent="run(loadReport)">
      <label>群<select v-model="selected" required><option value="" disabled>请选择群</option>
        <option v-for="group in groups" :key="groupKey(group)" :value="groupKey(group)">{{ group.group_id }} · {{ group.enabled ? '已启用' : '未启用' }}</option></select></label>
      <label>日期（上海）<input v-model="day" type="date" required /></label>
      <div class="actions"><ElButton native-type="submit" :disabled="busy || !selected">查看日报</ElButton></div>
    </form>
    <template v-if="report">
      <p>{{ report.group_id }} · {{ report.day }} · {{ report.enabled ? '分析已启用' : '当前未启用分析' }} · 待补充或人工事项 {{ report.unresolved }}</p>
      <p class="muted">已知范围 {{ report.coverage_since ? '自 ' + displayTime(report.coverage_since) : '仅显示已有记录' }}，按实际接收的消息汇总。</p>
      <p>咨询事项 {{ report.service_metrics.consultations }} · 已发答复 {{ report.service_metrics.answered }} · 转人工 {{ report.service_metrics.handoffs }} · 人工待跟进 {{ report.service_metrics.pending }}
        <span v-if="report.service_metrics.mean_resolution_seconds !== null"> · 平均结案 {{ Math.round(report.service_metrics.mean_resolution_seconds / 60) }} 分钟</span></p>
      <p v-if="report.delivery_counts.length" class="muted">触达 <span v-for="entry in report.delivery_counts" :key="entry.state">{{ ({ accepted: '已受理', sent: '已发送', delivered: '已送达', pending: '待发送', manual: '待人工', unknown: '待核实', failed: '失败', expired: '过期', cancelled: '取消' } as Record<string, string>)[entry.state] || entry.state }} {{ entry.count }}　</span></p>
      <p v-if="report.daily_snapshot" class="muted">18:00 日报 · {{ Object.entries(report.daily_snapshot.summary.topics).map(([topic, count]) => topic + ' ' + count).join('、') || '当日暂无反馈' }}</p>
      <p v-if="report.activity_progress.length">活动进度 <span v-for="entry in report.activity_progress" :key="entry.title + entry.action">{{ entry.title }} {{ statuses[entry.action] }} {{ entry.count }}　</span></p>
      <details v-for="bucket in report.items" :key="bucket.topic" class="signal-bucket" open>
        <summary>{{ bucket.topic }} · {{ bucket.count }} 条</summary>
        <ol class="messages">
          <li v-for="signal in bucket.signals" :key="signal.id">
            <small>{{ displayTime(signal.occurred_at) }} · {{ statuses[signal.category] }} · <ElTag size="small">{{ statuses[signal.status] }}</ElTag></small>
            <p>{{ signal.preview || '原文已到期或不可用' }}</p>
            <div class="inline-controls">
              <ElButton v-if="signal.matter_id" text @click="emit('matter', signal.matter_id)">查看事项</ElButton>
              <ElButton v-if="signal.profile_id" text @click="emit('member', signal.profile_id)">查看会员</ElButton>
              <ElButton text @click="emit('correction', signal)">建立纠错草稿</ElButton>
              <template v-if="signal.status !== 'closed'">
                <ElButton size="small" @click="review(signal, 'confirm')">确认</ElButton>
                <input v-model="changes[signal.id]" aria-label="纠正主题" placeholder="纠正主题或候选标签" maxlength="100" />
                <ElButton size="small" :disabled="!changes[signal.id]?.trim()" @click="review(signal, 'correct')">纠正</ElButton>
                <ElButton v-if="signal.category === 'interest' && signal.profile_id && signal.preview" size="small" @click="review(signal, 'candidate')">送入候选</ElButton>
                <ElButton size="small" text @click="review(signal, 'close')">关闭</ElButton>
              </template>
            </div>
          </li>
        </ol>
      </details>
      <p v-if="!report.items.length" class="muted">此范围暂无群反馈。</p>
    </template>
  </section>
</template>
