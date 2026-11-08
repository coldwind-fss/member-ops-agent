<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElButton, ElTag } from 'element-plus'
import { api } from './api'
import { displayTime, type Items, useOps } from './ops'

interface Member { id: string; version: number; identities: string[]; groups: string[]; status: string; last_activity: string | null }
interface Memory { id: string; kind: string; topic: string; value: string | null; status: string; version: number; evidence: string | null; expires_at: string | null; matter_id: string | null }
interface Detail { member: Member; memories: Memory[]; timeline: { event_id: string; kind: string; topic: string | null; preview: string | null; occurred_at: string; matter_id: string | null }[] }
interface Match { id: string; identities: string[]; matched: boolean; reasons: string[] }
interface Preview extends Items<Match> { matched_count: number }
interface Audience { id: string; name: string; confirmed_member_ids: string[] }

const props = defineProps<{ csrf: string; initialMember?: string }>()
const emit = defineEmits<{ matter: [id: string] }>()
const { error, notice, busy, run, save } = useOps(() => props.csrf)
const rows = ref<Member[]>([])
const detail = ref<Detail | null>(null)
const filters = ref({ group_id: '', interest: '', tag: '', recent_days: '' })
const audience = ref<Preview | null>(null)
const chosen = ref<string[]>([])
const name = ref('')
const saved = ref<Audience[]>([])
const correction = ref<Record<string, string>>({})
const topic = (key: string) => ({ size: '尺码', color: '颜色', use_case: '运动偏好' }[key] ?? key)
const state = (key: string) => ({ candidate: '候选', confirmed: '已确认', deleted: '已删除' }[key] ?? key)

async function load() {
  rows.value = (await api<Items<Member>>('/members')).items
  saved.value = (await api<Items<Audience>>('/audiences')).items
}
async function open(id: string) {
  detail.value = await api<Detail>('/members/' + id)
  correction.value = {}
}
async function review(item: Memory, action: string) {
  const profile = detail.value?.member.id
  if (!profile) return
  await run(async () => {
    await save('/members/' + profile + '/memories/' + item.id, { action, expected_version: item.version,
      ...(action === 'correct' ? { value: correction.value[item.id] } : {}) })
    await open(profile)
    audience.value = null
  }, '记忆已更新。')
}
async function removeProfile() {
  const profile = detail.value?.member
  if (!profile || !window.confirm('确认删除该会员画像及记忆？')) return
  await run(async () => {
    await save('/members/' + profile.id + '/delete', { expected_version: profile.version })
    detail.value = null
    audience.value = null
    await load()
  }, '会员画像已删除。')
}
function selectedFilters() {
  return Object.fromEntries(Object.entries(filters.value).filter(([, value]) => value)
    .map(([key, value]) => [key, key === 'recent_days' ? Number(value) : value]))
}
async function preview() {
  const query = new URLSearchParams(Object.entries(selectedFilters()).map(([key, value]) => [key, String(value)]))
  audience.value = await api<Preview>('/audiences/preview?' + query)
  chosen.value = audience.value.items.filter(item => item.matched).map(item => item.id)
}
async function confirmAudience() {
  await run(async () => {
    await save('/audiences', { ...selectedFilters(), name: name.value, confirmed_member_ids: chosen.value })
    name.value = ''
    await load()
  }, '候选人群已保存。')
}
onMounted(() => run(async () => { await load(); if (props.initialMember) await open(props.initialMember) }))
</script>

<template>
  <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="notice" role="status" class="notice">{{ notice }}</p>
  <section class="panel" aria-label="会员列表">
    <div class="panel-head"><h2>会员列表</h2><ElButton :disabled="busy" @click="run(load)">刷新</ElButton></div>
    <table><thead><tr><th>会员</th><th>身份状态</th><th>有效群资格</th><th>最近互动</th></tr></thead><tbody>
      <tr v-for="row in rows" :key="row.id"><td><button class="text-button" @click="run(() => open(row.id))">{{ row.identities.join('、') || '已解绑会员' }}</button></td>
        <td><ElTag size="small">{{ row.status === 'verified' ? '已核验' : '已解绑' }}</ElTag></td><td>{{ row.groups.join('、') || '—' }}</td><td>{{ displayTime(row.last_activity) }}</td></tr>
      <tr v-if="!rows.length"><td colspan="4" class="empty">当前没有会员画像。</td></tr>
    </tbody></table>
  </section>
  <section v-if="detail" class="panel section" aria-label="会员详情">
    <div class="panel-head"><h2>会员记忆与标签</h2><ElButton text type="danger" @click="removeProfile">删除画像</ElButton></div>
    <table><thead><tr><th>主题</th><th>内容</th><th>状态</th><th>依据与有效期</th><th>审核</th></tr></thead><tbody>
      <tr v-for="item in detail.memories" :key="item.id">
        <td>{{ topic(item.topic) }}</td><td>{{ item.value ?? '—' }}</td><td>{{ state(item.status) }}</td>
        <td><button v-if="item.matter_id" class="text-button" @click="emit('matter', item.matter_id)">{{ item.evidence }}</button>
          <span v-else>{{ item.evidence || '原文不可回看' }}</span><p v-if="item.expires_at" class="muted small">有效至 {{ displayTime(item.expires_at) }}</p></td>
        <td v-if="item.status !== 'deleted'"><div class="inline-controls">
          <ElButton v-if="item.status === 'candidate'" size="small" @click="review(item, 'confirm')">确认标签</ElButton>
          <input v-model="correction[item.id]" :aria-label="'纠正' + topic(item.topic)" placeholder="纠正值" maxlength="100" />
          <ElButton size="small" :disabled="!correction[item.id]?.trim()" @click="review(item, 'correct')">更正</ElButton>
          <ElButton size="small" text type="danger" @click="review(item, 'delete')">删除</ElButton>
        </div></td><td v-else>—</td>
      </tr><tr v-if="!detail.memories.length"><td colspan="5" class="empty">暂无记忆与候选标签。</td></tr>
    </tbody></table>
    <h3 class="ops-heading">互动时间线</h3>
    <ul v-if="detail.timeline.length" class="messages">
      <li v-for="entry in detail.timeline" :key="entry.event_id + entry.kind"><small>{{ displayTime(entry.occurred_at) }} · {{ entry.topic || entry.kind }}</small>
        <p><button v-if="entry.matter_id" class="text-button" @click="emit('matter', entry.matter_id)">{{ entry.preview }}</button>
          <span v-else>{{ entry.preview || '原文不可回看' }}</span></p></li>
    </ul><p v-else class="muted">暂无互动记录。</p>
  </section>
  <section class="panel section" aria-label="候选人群">
    <h2>候选人群</h2>
    <form class="form-grid" @submit.prevent="run(preview)" @input="audience = null">
      <label>群标识<input v-model="filters.group_id" /></label>
      <label>候选或已确认意向<input v-model="filters.interest" /></label>
      <label>已确认标签<input v-model="filters.tag" /></label>
      <label>最近活跃天数<input v-model="filters.recent_days" type="number" min="1" max="365" /></label>
      <div class="actions"><ElButton type="primary" native-type="submit" :disabled="busy">预览范围</ElButton></div>
    </form>
    <div v-if="audience">
      <p>符合条件 {{ audience.matched_count }} 人</p>
      <table><thead><tr><th>纳入</th><th>会员</th><th>理由</th></tr></thead><tbody>
        <tr v-for="item in audience.items" :key="item.id"><td><input v-model="chosen" :value="item.id" type="checkbox" :disabled="!item.matched" aria-label="纳入候选范围" /></td><td>{{ item.identities.join('、') || '已解绑会员' }}</td><td>{{ item.reasons.join('、') }}</td></tr>
      </tbody></table>
      <form class="inline-controls" @submit.prevent="confirmAudience">
        <input v-model="name" required maxlength="100" placeholder="人群名称" aria-label="人群名称" />
        <ElButton native-type="submit" :disabled="!chosen.length || busy">保存确认范围</ElButton>
      </form>
    </div>
    <p v-for="item in saved" :key="item.id" class="muted">{{ item.name }} · 确认时 {{ item.confirmed_member_ids.length }} 人</p>
  </section>
</template>
