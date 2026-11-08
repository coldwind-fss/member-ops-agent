<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElButton, ElTag } from 'element-plus'
import { api } from './api'
import { displayTime, localDateTime, type Items, useOps } from './ops'

interface Group { connection_id: string; group_id: string; reply_enabled: boolean; analysis_enabled: boolean; outreach_enabled: boolean; welcome_text: string | null; care_text: string | null }
interface PlanConfig { title: string; body: string; connection_id: string; group_id: string; mode: string; scheduled_at: string; expires_at: string; audience_id: string | null; activity_id: string | null; daily_limit: number }
interface Plan { id: string; title: string; version: number; status: string; config: PlanConfig; approved_members: string[] }
interface RecordRow { id: string; operation_key: string; kind: string; group_id: string | null; state: string; reason: string | null; version: number;
  attempts: number; evidence: string | null; provider_ref: string | null; scheduled_at: string; sender_external_id: string | null }
interface Subscription { id: string; profile_id: string; product_name: string; color: string; size: string; status: string; version: number; expires_at: string }
interface Suggestion { id: string; profile_id: string; topic: string; status: string; version: number; result: string | null; preview: string | null; matter_id: string | null }
interface Target { id: string; identities: string[]; matched: boolean; reasons: string[] }
const props = defineProps<{ csrf: string }>()
const emit = defineEmits<{ matter: [id: string]; member: [id: string] }>()
const { busy, error, notice, run, save } = useOps(() => props.csrf)
type Section = 'plans' | 'groups' | 'subscriptions' | 'suggestions' | 'records'
const section = ref<Section>('plans')
const groups = ref<Group[]>([])
const plans = ref<Plan[]>([])
const audiences = ref<{ id: string; name: string }[]>([])
const activities = ref<{ id: string; title: string }[]>([])
const knowledge = ref<{ id: string; title: string; version: number }[]>([])
const subscriptions = ref<Subscription[]>([])
const suggestions = ref<Suggestion[]>([])
const records = ref<RecordRow[]>([])
const cursor = ref<string | null>(null)
const recordState = ref('')
const selected = ref<Plan | null>(null)
const targets = ref<Target[]>([])
const chosen = ref<string[]>([])
const previewed = ref(false)
const editing = ref<Plan | null>(null)
const draftKnowledge = ref('')
const sources = ref<{ kind: string; id: string; version: number }[]>([])
const editingRecord = ref<RecordRow | null>(null)
const resolution = ref({ state: 'sent', evidence: '', provider_ref: '' })
const following = ref<Suggestion | null>(null)
const follow = ref({ group_id: '', connection_id: 'fixture-channel', body: '', reason: 'resolved' })
const care = ref({ group_id: '', connection_id: 'fixture-channel', profile_id: '', order_id: '', body: '' })
const blankPlan = () => ({ title: '', body: '', connection_id: 'fixture-channel', group_id: '', mode: 'group', scheduled_at: '',
  expires_at: '', audience_id: '', activity_id: '', daily_limit: 1 })
const plan = ref(blankPlan())
const group = ref<Group>({ connection_id: 'fixture-channel', group_id: '', reply_enabled: true, analysis_enabled: false,
  outreach_enabled: false, welcome_text: '', care_text: '' })
const sections: Record<Section, string> = { plans: '运营计划', groups: '群与模板', subscriptions: '补货订阅', suggestions: '意向跟进', records: '发送记录' }
const labels: Record<string, string> = { draft: '待确认', approved: '已批准', cancelled: '已取消', open: '待审核', closed: '已结束',
  pending: '待发送', sending: '交付中', accepted: '供应商已受理', sent: '已发送', delivered: '已送达', manual: '人工待办',
  unknown: '待核实', failed: '失败', expired: '已过期', invalid: '身份失效', fulfilled: '已完成', active: '有效',
  welcome: '新人欢迎', restock: '补货', care: '购后关怀', followup: '意向跟进', plan: '运营计划',
  PLAN_CHANGED: '计划已变更', SCHEDULE_EXPIRED: '排期已过', ACTIVITY_UNAVAILABLE: '活动当前不可用',
  BINDING_CHANGED: '绑定已变化', MEMBER_LEFT: '已退出群', NO_LONGER_ELIGIBLE: '资格变化',
  SUBSCRIPTION_ENDED: '订阅已结束', STOCK_UNAVAILABLE: '当前无库存', CARE_OPT_OUT: '会员已关闭购后关怀',
  ORDER_NOT_ELIGIBLE: '订单状态已变更', INTEREST_ENDED: '跟进已结束', WELCOME_FREQUENCY: '24 小时内已欢迎',
  CHANNEL_UNAVAILABLE: '发送渠道不可用', DELIVERY_NEEDS_RECONCILIATION: '交付结果需核实', TEMPORARY_FAILURE: '接口暂时失败',
  DAILY_FREQUENCY: '已达每日触达上限', ACTIVITY_RULE_CHANGED: '活动规则已变更，需重新确认' }
const label = (key: string | null) => key ? labels[key] ?? key : '—'
const inputTime = (value: string) => {
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}

async function loadRecords(append = false) {
  const query = new URLSearchParams()
  if (recordState.value) query.set('state', recordState.value)
  if (append && cursor.value) query.set('cursor', cursor.value)
  const page = await api<{ items: RecordRow[]; next_cursor: string | null }>('/outreach/records?' + query)
  records.value = append ? [...records.value, ...page.items] : page.items
  cursor.value = page.next_cursor
}
async function load() {
  const tab = section.value
  if (tab === 'records') return loadRecords()
  if (tab === 'groups') {
    groups.value = (await api<Items<Group>>('/outreach/groups')).items
    return
  }
  if (tab === 'subscriptions') {
    subscriptions.value = (await api<Items<Subscription>>('/outreach/subscriptions')).items
    return
  }
  if (tab === 'suggestions') {
    suggestions.value = (await api<Items<Suggestion>>('/outreach/suggestions')).items
    return
  }
  const [planRows, audienceRows, activityRows, knowledgeRows] = await Promise.all([
    api<Items<Plan>>('/outreach/plans'), api<Items<{ id: string; name: string }>>('/audiences'),
    api<Items<{ id: string; title: string }>>('/activities'),
    api<Items<{ id: string; title: string; version: number }>>('/knowledge'),
  ])
  plans.value = planRows.items
  audiences.value = audienceRows.items
  activities.value = activityRows.items
  const versions = new Map<string, { id: string; title: string; version: number }>()
  for (const row of knowledgeRows.items) {
    if ((versions.get(row.id)?.version ?? 0) < row.version) versions.set(row.id, row)
  }
  knowledge.value = [...versions.values()]
}
function tab(key: Section) {
  section.value = key
  void run(load)
}
async function saveGroup() {
  await run(async () => {
    await save('/outreach/groups', { ...group.value, welcome_text: group.value.welcome_text?.trim() || null,
      care_text: group.value.care_text?.trim() || null })
    await load()
  }, '群设置已保存。')
}
function edit(item: Plan) {
  editing.value = item
  const value = item.config
  plan.value = { ...value, scheduled_at: inputTime(value.scheduled_at), expires_at: inputTime(value.expires_at),
    audience_id: value.audience_id ?? '', activity_id: value.activity_id ?? '' }
}
async function savePlan() {
  await run(async () => {
    const config = { ...plan.value, scheduled_at: localDateTime(plan.value.scheduled_at),
      expires_at: localDateTime(plan.value.expires_at), audience_id: plan.value.mode === 'personal' ? plan.value.audience_id : null,
      activity_id: plan.value.activity_id || null, daily_limit: plan.value.activity_id ? plan.value.daily_limit : 1 }
    const item = editing.value
    const row = await save<Plan>(item ? '/outreach/plans/' + item.id : '/outreach/plans',
      item ? { action: 'edit', expected_version: item.version, config } : config)
    editing.value = null
    plan.value = blankPlan()
    selected.value = row
    previewed.value = false
    await load()
  }, '草稿已保存，待确认范围与排期。')
}
async function draft() {
  await run(async () => {
    const result = await save<{ body: string; sources: { kind: string; id: string; version: number }[] }>('/outreach/draft',
      { topic: plan.value.title, activity_id: plan.value.activity_id || null, knowledge_id: draftKnowledge.value || null })
    plan.value.body = result.body
    sources.value = result.sources
  }, '文案已生成，请核对后保存草稿。')
}
async function preview(item: Plan) {
  await run(async () => {
    const result = await api<Items<Target>>('/outreach/plans/' + item.id + '/preview')
    selected.value = item
    targets.value = result.items
    chosen.value = targets.value.filter(row => row.matched).map(row => row.id)
    previewed.value = true
  })
}
async function change(item: Plan, action: string) {
  await run(async () => {
    selected.value = await save<Plan>('/outreach/plans/' + item.id,
      { action, expected_version: item.version, member_ids: action === 'approve' ? chosen.value : [] })
    previewed.value = false
    await load()
  }, action === 'approve' ? '名单与排期已确认。' : '计划已取消。')
}
async function cancelSubscription(item: Subscription) {
  await run(async () => {
    await save('/outreach/subscriptions/' + item.id + '/cancel', { expected_version: item.version })
    await load()
  }, '订阅已取消。')
}
async function handleSuggestion(action: string) {
  const item = following.value
  if (!item) return
  await run(async () => {
    await save('/outreach/suggestions/' + item.id, { action, expected_version: item.version,
      ...(action === 'approve' ? { group_id: follow.value.group_id, connection_id: follow.value.connection_id, body: follow.value.body }
        : { reason: follow.value.reason }) })
    following.value = null
    await load()
  }, '已记录处理结果。')
}
async function sendCare() {
  await run(async () => {
    await save('/outreach/care', care.value)
    care.value = { ...care.value, order_id: '', body: '' }
  }, '签收已核实，关怀任务已登记。')
}
async function resolve() {
  const item = editingRecord.value
  if (!item) return
  await run(async () => {
    await save('/outreach/records/' + item.id + '/resolve', { ...resolution.value,
      expected_version: item.version, provider_ref: resolution.value.provider_ref || null })
    editingRecord.value = null
    await loadRecords()
  }, '核实结果已登记。')
}
onMounted(() => run(load))
</script>

<template>
  <div class="inline-controls ops-tabs"><ElButton v-for="(title, key) in sections" :key="key" :type="section === key ? 'primary' : 'default'" :disabled="busy" @click="tab(key)">{{ title }}</ElButton>
    <ElButton :disabled="busy" @click="run(load)">刷新</ElButton></div>
  <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="notice" role="status" class="notice">{{ notice }}</p>

  <template v-if="section === 'groups'">
    <section class="panel"><div class="panel-head"><h2>群级运行开关</h2></div>
      <table><thead><tr><th>渠道 / 群</th><th>自动应答</th><th>群分析</th><th>运营触达</th></tr></thead><tbody>
        <tr v-for="row in groups" :key="row.connection_id + row.group_id"><td><button class="text-button" @click="group = { ...row }">{{ row.connection_id }} / {{ row.group_id }}</button></td>
          <td>{{ row.reply_enabled ? '开启' : '暂停' }}</td><td>{{ row.analysis_enabled ? '开启' : '关闭' }}</td><td>{{ row.outreach_enabled ? '开启' : '暂停' }}</td></tr>
        <tr v-if="!groups.length"><td colspan="4" class="empty">暂无配置，可在下方登记群。</td></tr>
      </tbody></table>
    </section>
    <section class="panel section"><h2>配置群与确认文案</h2>
      <form class="form-grid" @submit.prevent="saveGroup">
        <label>渠道<input v-model="group.connection_id" required /></label><label>群标识<input v-model="group.group_id" required /></label>
        <div class="inline-controls form-wide"><label class="check"><input v-model="group.reply_enabled" type="checkbox" /> 自动应答</label>
          <label class="check"><input v-model="group.analysis_enabled" type="checkbox" /> 群分析</label><label class="check"><input v-model="group.outreach_enabled" type="checkbox" /> 运营触达</label></div>
        <label>欢迎文案<textarea v-model="group.welcome_text" rows="3" maxlength="2000" /></label>
        <label>关怀文案<textarea v-model="group.care_text" rows="3" maxlength="2000" /></label>
        <div class="actions"><ElButton type="primary" native-type="submit" :loading="busy">保存设置</ElButton></div>
      </form>
    </section>
  </template>
  <template v-else-if="section === 'plans'">
    <section class="panel"><div class="panel-head"><h2>排期与批准状态</h2></div>
      <table><thead><tr><th>计划</th><th>目标群</th><th>排期</th><th>状态</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in plans" :key="item.id"><td>{{ item.title }}</td><td>{{ item.config.group_id }} · {{ item.config.mode === 'group' ? '群公告' : '个人触达' }}</td>
          <td>{{ displayTime(item.config.scheduled_at) }}</td><td><ElTag size="small">{{ label(item.status) }} v{{ item.version }}</ElTag></td>
          <td><div class="inline-controls"><ElButton size="small" @click="preview(item)">查看目标</ElButton><ElButton size="small" @click="edit(item)">编辑与重排</ElButton>
            <ElButton v-if="item.status !== 'cancelled'" size="small" text type="danger" @click="change(item, 'cancel')">取消计划</ElButton></div></td></tr>
        <tr v-if="!plans.length"><td colspan="5" class="empty">暂无运营计划。</td></tr>
      </tbody></table>
    </section>
    <section v-if="selected && previewed" class="panel section" aria-label="目标确认">
      <h2>{{ selected.title }} · 目标确认</h2><p>{{ selected.config.body }}</p>
      <p v-if="selected.config.mode === 'group'" class="muted">在 {{ selected.config.group_id }} 群发布一条公告。</p>
      <table v-else><thead><tr><th>纳入</th><th>会员</th><th>当前资格</th><th>理由</th></tr></thead><tbody>
        <tr v-for="row in targets" :key="row.id"><td><input v-model="chosen" type="checkbox" :value="row.id" :disabled="!row.matched || selected.status !== 'draft'" aria-label="纳入计划" /></td>
          <td>{{ row.identities.join('、') || row.id }}</td><td>{{ row.matched ? '符合' : '排除' }}</td><td>{{ row.reasons.join('、') }}</td></tr>
      </tbody></table>
      <p class="muted" v-if="selected.status === 'approved'">批准时确认 {{ selected.approved_members.length }} 位会员。实际发送前再次核对。</p>
      <div class="actions"><ElButton v-if="selected.status === 'draft'" type="primary" :disabled="busy || (selected.config.mode === 'personal' && !chosen.length)" @click="change(selected, 'approve')">确认并排期</ElButton></div>
    </section>
    <section class="panel section"><h2>{{ editing ? '编辑运营计划' : '新建运营草稿' }}</h2>
      <form class="form-grid" @submit.prevent="savePlan">
        <label>名称<input v-model="plan.title" required maxlength="120" /></label><label>目标群<input v-model="plan.group_id" required /></label>
        <label>渠道<input v-model="plan.connection_id" required /></label>
        <label>发送范围<select v-model="plan.mode"><option value="group">群公告</option><option value="personal">个人触达</option></select></label>
        <label v-if="plan.mode === 'personal'">已确认人群<select v-model="plan.audience_id" required><option value="">请选择</option><option v-for="row in audiences" :key="row.id" :value="row.id">{{ row.name }}</option></select></label>
        <label>关联活动<select v-model="plan.activity_id"><option value="">无</option><option v-for="row in activities" :key="row.id" :value="row.id">{{ row.title }}</option></select></label>
        <label v-if="plan.activity_id">每日触达上限<input v-model.number="plan.daily_limit" type="number" min="1" max="10" required /></label>
        <label>参考知识<select v-model="draftKnowledge"><option value="">无</option><option v-for="row in knowledge" :key="row.id" :value="row.id">{{ row.title }}</option></select></label>
        <label>计划时间（本机时区）<input v-model="plan.scheduled_at" type="datetime-local" required /></label>
        <label>有效截止（本机时区）<input v-model="plan.expires_at" type="datetime-local" required /></label>
        <label class="form-wide">发送正文<textarea v-model="plan.body" rows="4" required maxlength="4000" /></label>
        <p v-if="sources.length" class="muted form-wide">草稿依据 {{ sources.map(source => source.kind + ' v' + source.version).join('、') }}</p>
        <div class="actions"><ElButton :disabled="!plan.title || busy" @click="draft">按资料生成文案</ElButton>
          <ElButton type="primary" native-type="submit" :loading="busy">保存计划草稿</ElButton></div>
      </form>
    </section>
  </template>
  <section v-else-if="section === 'subscriptions'" class="panel">
    <div class="panel-head"><h2>规格级补货订阅</h2></div>
    <table><thead><tr><th>商品规格</th><th>会员</th><th>状态</th><th>到期</th><th>操作</th></tr></thead><tbody>
      <tr v-for="item in subscriptions" :key="item.id"><td>{{ item.product_name }} {{ item.color }} {{ item.size }}</td>
        <td><button class="text-button" @click="emit('member', item.profile_id)">{{ item.profile_id }}</button></td>
        <td>{{ label(item.status) }}</td><td>{{ displayTime(item.expires_at) }}</td>
        <td><ElButton v-if="item.status === 'active'" size="small" text type="danger" @click="cancelSubscription(item)">取消订阅</ElButton></td></tr>
      <tr v-if="!subscriptions.length"><td colspan="5" class="empty">暂无会员主动订阅。</td></tr>
    </tbody></table>
  </section>
  <template v-else-if="section === 'suggestions'">
    <section class="panel"><div class="panel-head"><h2>意向线索</h2></div>
      <table><thead><tr><th>主题</th><th>原始依据</th><th>状态</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in suggestions" :key="item.id"><td>{{ item.topic }}</td>
          <td><button v-if="item.matter_id" class="text-button" @click="emit('matter', item.matter_id)">{{ item.preview }}</button><span v-else>{{ item.preview || '原文不可回看' }}</span></td>
          <td>{{ label(item.status) }} <small v-if="item.result">{{ item.result }}</small></td>
          <td><ElButton v-if="item.status !== 'closed'" size="small" @click="following = item">处理</ElButton></td></tr>
        <tr v-if="!suggestions.length"><td colspan="4" class="empty">暂无有依据的个人线索。</td></tr>
      </tbody></table>
    </section>
    <section v-if="following" class="panel section"><h2>审核 {{ following.topic }}</h2>
      <form class="form-grid" @submit.prevent="handleSuggestion('approve')">
        <label>目标群<input v-model="follow.group_id" required /></label><label>渠道<input v-model="follow.connection_id" required /></label>
        <label>确认的跟进正文<textarea v-model="follow.body" rows="3" required maxlength="2000" /></label>
        <label>结束原因<select v-model="follow.reason"><option value="resolved">已解决</option><option value="declined">已拒绝</option><option value="purchased">已购</option><option value="expired">已失效</option></select></label>
        <div class="actions"><ElButton v-if="following.status === 'open'" native-type="submit" type="primary" :disabled="busy">确认跟进</ElButton><ElButton @click="handleSuggestion('close')">结束线索</ElButton></div>
      </form>
    </section>
    <section class="panel section"><h2>手动安排购后关怀</h2>
      <form class="form-grid" @submit.prevent="sendCare">
        <label>会员档案标识<input v-model="care.profile_id" required /></label><label>订单标识<input v-model="care.order_id" required /></label>
        <label>渠道<input v-model="care.connection_id" required /></label><label>目标群<input v-model="care.group_id" required /></label>
        <label class="form-wide">确认的关怀文案<textarea v-model="care.body" rows="3" required maxlength="4000" /></label>
        <div class="actions"><ElButton type="primary" native-type="submit" :disabled="busy">核实签收并登记</ElButton></div>
      </form>
    </section>
  </template>
  <template v-else>
    <section class="panel"><div class="panel-head"><h2>触达记录与人工待办</h2>
      <label>状态 <select v-model="recordState" @change="run(() => loadRecords())"><option value="">全部</option>
        <option v-for="status in ['pending','accepted','sent','delivered','manual','unknown','failed','cancelled','expired']" :key="status" :value="status">{{ label(status) }}</option>
      </select></label></div>
      <table><thead><tr><th>用途与目标</th><th>状态</th><th>原因或证据</th><th>尝试次数</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in records" :key="item.id"><td>{{ label(item.kind) }} · {{ item.group_id || '私密渠道' }}<p class="muted small">{{ item.sender_external_id || displayTime(item.scheduled_at) }}</p></td>
          <td><ElTag size="small">{{ label(item.state) }}</ElTag></td><td>{{ label(item.reason) }}<p v-if="item.provider_ref || item.evidence" class="muted small">{{ item.provider_ref }} {{ item.evidence }}</p></td>
          <td>{{ item.attempts }}</td><td><ElButton v-if="['manual','unknown','accepted'].includes(item.state)" size="small" @click="editingRecord = item; resolution = { state: 'sent', evidence: '', provider_ref: item.provider_ref ?? '' }">登记核实</ElButton></td></tr>
        <tr v-if="!records.length"><td colspan="5" class="empty">暂无该状态的触达记录。</td></tr>
      </tbody></table>
      <ElButton v-if="cursor" class="more" @click="run(() => loadRecords(true))">加载更多</ElButton>
    </section>
    <section v-if="editingRecord" class="panel section"><h2>核实 {{ label(editingRecord.kind) }}结果</h2>
      <form class="form-grid" @submit.prevent="resolve">
        <label>最终状态<select v-model="resolution.state"><option v-for="status in ['accepted','sent','delivered','failed','cancelled']" :key="status" :value="status">{{ label(status) }}</option></select></label>
        <label>供应商回执<input v-model="resolution.provider_ref" /></label>
        <label class="form-wide">实际依据<textarea v-model="resolution.evidence" required minlength="4" maxlength="1000" rows="3" /></label>
        <div class="actions"><ElButton type="primary" native-type="submit" :disabled="busy">保存核实</ElButton></div>
      </form>
    </section>
  </template>
</template>
