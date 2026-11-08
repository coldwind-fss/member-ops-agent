<script setup lang="ts">
import { ElButton, ElTag } from 'element-plus'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import OpsActivities from './OpsActivities.vue'
import OpsInsights from './OpsInsights.vue'
import OpsKnowledge from './OpsKnowledge.vue'
import OpsMembers from './OpsMembers.vue'
import OpsOutreach from './OpsOutreach.vue'
import { api, ApiError, type CommerceCapabilities, type CommerceCheck, type Handoff, type Matter,
  type MatterDetail, type MemberBinding, type Page, type Session } from './api'

const session = ref<Session | null>(null)
const checking = ref(true)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const username = ref('')
const password = ref('')
type View = 'matters' | 'handoffs' | 'commerce' | 'members' | 'activities' | 'insights' | 'knowledge' | 'outreach'
const view = ref<View>('matters')
const selectedMember = ref('')
const correctionSource = ref<{ matter_id?: string; signal_id?: string; knowledge_id?: string; question?: string }>()
const matters = ref<Page<Matter>>({ items: [], next_cursor: null })
const handoffs = ref<Page<Handoff>>({ items: [], next_cursor: null })
const bindings = ref<Page<MemberBinding>>({ items: [], next_cursor: null })
const capabilities = ref<CommerceCapabilities | null>(null)
const commerceChecks = ref<Page<CommerceCheck>>({ items: [], next_cursor: null })
const selected = ref('')
const detail = ref<MatterDetail | null>(null)
const editing = ref<Handoff | null>(null)
const resolution = ref('')
const resume = ref(false)
const filter = ref<'pending' | 'resolved' | ''>('pending')
const apiHealth = ref<boolean | null>(null)
const databaseHealth = ref<boolean | null>(null)
const linkedMatter = new URLSearchParams(location.search).get('matter')
let timer: number | undefined
let resolveKey: { payload: string; key: string } | undefined

const descriptions: Record<string, string> = {
  open: '会话中', waiting_member: '等会员补充', human_handling: '人工处理', closed: '已结束',
  pending: '待处理', resolved: '已处理', accepted: '已受理', sent: '已发送',
  delivered: '已送达', failed: '发送失败', unknown: '待核实', cancelled: '已取消',
  inbound: '会员消息', assistant: '助手答复', answered: '已答复', clarification: '追问',
  handoff: '转人工', unavailable: '暂不可用', ignored: '已跳过',
  public_knowledge: '品牌知识', public_after_sale_policy: '售后规则', human_handoff: '人工请求',
  personal_query: '个人查询', CAPABILITY_UNAVAILABLE: '业务接口未开放',
  MODEL_OUTPUT_UNAVAILABLE: '模型输出不可用', MODEL_NOT_CONFIGURED: '模型未配置',
  AMBIGUOUS_PRODUCT: '商品不明确', MISSING_PRODUCT: '缺少款式', KNOWLEDGE_NOT_FOUND: '缺少资料',
  KNOWLEDGE_CONFLICT: '资料冲突', HUMAN_REQUEST_OR_COMPLAINT: '人工请求或投诉',
  APPENDED_TO_HANDOFF: '补充到已有待办', UNVERIFIED_CONDITION: '转人工条件待核实',
  inventory_query: '商品库存', public_promotion: '公开活动', product_advice: '选购建议',
  identity: '身份核验', catalog: '商品与标价', inventory: '规格库存', order: '订单物流',
  promotion: '活动规则', eligibility: '参与资格', verified: '已核验', unbound: '未绑定',
  OK: '已查到', VERIFIED: '核验通过', UNBOUND: '未绑定', PRIVATE_CHANNEL_REQUIRED: '需使用核验后的私密会话',
  FORBIDDEN: '无访问权限', NOT_FOUND: '未查到', UNAVAILABLE: '接口未接入',
  TEMPORARY_FAILURE: '接口暂时失败', CONFLICT: '状态冲突', EXPIRED: '已结束', INELIGIBLE: '不符合资格',
  BINDING_CHANGED: '绑定已变更', ORDER_SELECTION_REQUIRED: '等待选择本人订单',
  CONFIRMED_ORDER_MISMATCH: '已核实订单异常', CONDITION_NOT_MET: '转人工条件未满足',
  'fixture-commerce': '商家测试数据', application: '应用规则', not_configured: '来源未接入',
}
const describe = (value: string | null) => value ? descriptions[value] ?? value : '—'
const date = (value: string) => new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short',
}).format(new Date(value))
const tabTitles: Record<View, string> = { matters: '会话事项', handoffs: '人工待办', commerce: '业务连接',
  members: '会员经营', activities: '活动管理', insights: '群复盘', knowledge: '知识管理', outreach: '运营触达' }
const currentPage = computed(() => {
  const tab = view.value
  return tab === 'matters' ? matters.value : tab === 'handoffs' ? handoffs.value : tab === 'commerce' ? bindings.value : null
})

function report(problem: unknown) {
  if (problem instanceof ApiError && problem.status === 401) {
    session.value = null
    selected.value = ''
    detail.value = null
    editing.value = null
    matters.value = { items: [], next_cursor: null }
    handoffs.value = { items: [], next_cursor: null }
    bindings.value = { items: [], next_cursor: null }
    capabilities.value = null
    commerceChecks.value = { items: [], next_cursor: null }
  }
  error.value = problem instanceof Error ? problem.message : '请求未完成。'
}

async function health() {
  const check = (path: string) => api(path).then(() => true, () => false)
  const [live, ready] = await Promise.all([check('/health/live'), check('/health/ready')])
  apiHealth.value = live
  databaseHealth.value = ready
}

async function loadList(append = false) {
  if (!session.value) return
  const tab = view.value
  const page = currentPage.value
  if (!page) return
  const query = new URLSearchParams({ limit: '20' })
  if (tab === 'handoffs' && filter.value) query.set('status', filter.value)
  if (append && page.next_cursor) query.set('cursor', page.next_cursor)
  if (tab === 'matters') {
    const result = await api<Page<Matter>>('/matters?' + query)
    matters.value = { ...result, items: append ? [...matters.value.items, ...result.items] : result.items }
  } else if (tab === 'handoffs') {
    const result = await api<Page<Handoff>>('/handoffs?' + query)
    handoffs.value = { ...result, items: append ? [...handoffs.value.items, ...result.items] : result.items }
  } else {
    const [result, config] = await Promise.all([
      api<Page<MemberBinding>>('/commerce/bindings?' + query),
      api<CommerceCapabilities>('/commerce/capabilities'),
    ])
    bindings.value = { ...result, items: append ? [...bindings.value.items, ...result.items] : result.items }
    capabilities.value = config
  }
}

async function refresh() {
  error.value = ''
  try {
    await Promise.all([health(), loadList()])
    if (selected.value) await openMatter(selected.value)
  } catch (problem) { report(problem) }
}

async function login() {
  busy.value = true
  error.value = ''
  try {
    session.value = await api<Session>('/auth/login', {
      method: 'POST', body: JSON.stringify({ username: username.value, password: password.value }),
    })
    await refresh()
    if (linkedMatter) await openMatter(linkedMatter)
  } catch (problem) { report(problem) }
  finally { password.value = ''; busy.value = false }
}

async function logout() {
  if (!session.value) return
  try {
    await api('/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': session.value.csrf_token } })
    session.value = null
    selected.value = ''
    detail.value = null
    editing.value = null
    matters.value = { items: [], next_cursor: null }
    handoffs.value = { items: [], next_cursor: null }
    bindings.value = { items: [], next_cursor: null }
    capabilities.value = null
    commerceChecks.value = { items: [], next_cursor: null }
    notice.value = ''
    error.value = ''
  } catch (problem) { report(problem) }
}

async function openMatter(id: string) {
  if (selected.value !== id) detail.value = null
  selected.value = id
  try {
    const [result, checks] = await Promise.all([
      api<MatterDetail>('/matters/' + id), api<Page<CommerceCheck>>(`/matters/${id}/commerce-checks`),
    ])
    if (selected.value === id) {
      detail.value = result
      commerceChecks.value = checks
    }
  } catch (problem) { report(problem) }
}

async function moreChecks() {
  const id = selected.value
  const cursor = commerceChecks.value.next_cursor
  if (!id || !cursor) return
  const page = await api<Page<CommerceCheck>>(`/matters/${id}/commerce-checks?` + new URLSearchParams({ cursor }))
  if (selected.value === id && commerceChecks.value.next_cursor === cursor) {
    commerceChecks.value = { ...page, items: [...commerceChecks.value.items, ...page.items] }
  }
}

async function changeTab(tab: typeof view.value) {
  view.value = tab
  selected.value = ''
  detail.value = null
  editing.value = null
  notice.value = ''
  selectedMember.value = ''
  correctionSource.value = undefined
  await refresh()
}

async function showMatter(id: string) {
  await changeTab('matters')
  await openMatter(id)
}

async function showMember(id: string) {
  selectedMember.value = id
  view.value = 'members'
}

function correctSignal(signal: { id: string; matter_id: string | null; preview: string | null }) {
  correctionSource.value = { signal_id: signal.id, matter_id: signal.matter_id ?? undefined, question: signal.preview ?? undefined }
  detail.value = null
  view.value = 'knowledge'
}

function correctMatter(item: MatterDetail) {
  correctionSource.value = { matter_id: item.matter.id, knowledge_id: item.evidence[0]?.knowledge_id,
    question: item.matter.conversation_kind === 'group' ? item.messages.find(message => message.kind === 'inbound')?.preview : undefined }
  detail.value = null
  view.value = 'knowledge'
}

function edit(item: Handoff) {
  editing.value = item
  resolution.value = ''
  resume.value = false
  resolveKey = undefined
}

async function resolveHandoff() {
  if (!session.value || !editing.value || !resolution.value.trim()) return
  const item = editing.value
  const body = JSON.stringify({ expected_version: item.version, resolution: resolution.value.trim(), resume_agent: resume.value })
  if (!resolveKey || resolveKey.payload !== body) resolveKey = { payload: body, key: crypto.randomUUID() }
  busy.value = true
  error.value = ''
  try {
    await api<Handoff>('/handoffs/' + item.id + '/resolve', {
      method: 'POST', body, headers: { 'X-CSRF-Token': session.value.csrf_token, 'Idempotency-Key': resolveKey.key },
    })
    editing.value = null
    notice.value = resume.value ? '处理结果已记录，已安排助手续办。' : '待办已处理。'
    await loadList()
    if (selected.value === item.matter_id) await openMatter(item.matter_id)
  } catch (problem) {
    report(problem)
    if (problem instanceof ApiError && problem.status === 409) {
      editing.value = null
      await loadList().catch(report)
    }
  } finally { busy.value = false }
}

onMounted(async () => {
  void health()
  try {
    session.value = await api<Session>('/auth/session')
    await loadList()
    if (linkedMatter) await openMatter(linkedMatter)
  } catch (problem) {
    if (!(problem instanceof ApiError && problem.status === 401)) report(problem)
  } finally { checking.value = false }
  timer = window.setInterval(() => {
    if (session.value && !editing.value && !busy.value && currentPage.value &&
        currentPage.value.items.length <= 20) {
      void loadList().catch(report)
    }
  }, 8000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <div v-if="checking" class="login-page"><p>正在打开工作台…</p></div>
  <main v-else-if="!session" class="login-page">
    <form class="login-card" @submit.prevent="login">
      <p class="brand">私域运营助手</p>
      <h1>运营登录</h1>
      <p class="muted">登录后查看会话、知识依据与人工待办。</p>
      <label>运营账号<input v-model="username" name="username" required autocomplete="username" maxlength="128" /></label>
      <label>密码<input v-model="password" name="password" type="password" required autocomplete="current-password" maxlength="1024" /></label>
      <p v-if="error" role="alert" class="error">{{ error }}</p>
      <ElButton type="primary" native-type="submit" :loading="busy">登录工作台</ElButton>
      <p class="muted small">API {{ apiHealth === null ? '检查中' : apiHealth ? '正常' : '不可用' }} · 数据库 {{ databaseHealth === null ? '检查中' : databaseHealth ? '就绪' : '不可用' }}</p>
    </form>
  </main>

  <div v-else class="layout">
    <aside class="sidebar">
      <div class="sidebar-brand"><span class="mark">私</span><div><strong>私域运营助手</strong><small>品牌运营后台</small></div></div>
      <nav aria-label="运营导航">
        <button v-for="(title, tab) in tabTitles" :key="tab" :class="{ active: view === tab }" @click="changeTab(tab)">{{ title }}</button>
      </nav>
      <div class="sidebar-footer">{{ session.display_name }}<button @click="logout">退出登录</button></div>
    </aside>

    <main class="workspace">
      <header class="workspace-header">
        <div><p class="brand">运营工作台</p><h1>{{ tabTitles[view] }}</h1>
          <p class="muted">核对会员问题、助手答复和后续处理。</p>
        </div>
        <ElButton v-if="currentPage" @click="refresh">刷新</ElButton>
      </header>
      <p v-if="error" role="alert" class="error banner">{{ error }}</p>
      <p v-if="notice" role="status" class="notice">{{ notice }}</p>
      <section v-if="view === 'matters'" class="panel" aria-label="会话列表">
        <table>
          <thead><tr><th>事项</th><th>会员</th><th>会话</th><th>状态</th><th>更新</th></tr></thead>
          <tbody>
            <tr v-for="item in matters.items" :key="item.id">
              <td><button class="text-button" @click="openMatter(item.id)">{{ item.subject }}</button></td>
              <td>{{ item.participant_label }}</td><td>{{ item.conversation_external_id }}</td>
              <td><ElTag size="small">{{ describe(item.status) }}</ElTag></td><td>{{ date(item.updated_at) }}</td>
            </tr>
            <tr v-if="!matters.items.length"><td colspan="5" class="empty">当前没有会话事项。</td></tr>
          </tbody>
        </table>
        <ElButton v-if="matters.next_cursor" class="more" @click="loadList(true).catch(report)">加载更多</ElButton>
      </section>

      <section v-else-if="view === 'handoffs'" class="panel" aria-label="待办列表">
        <div class="panel-head"><label>状态 <select v-model="filter" @change="refresh">
          <option value="pending">待跟进</option><option value="resolved">已处理</option><option value="">全部</option>
        </select></label></div>
        <table>
          <thead><tr><th>会员问题</th><th>提醒状态</th><th>处理结果</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="item in handoffs.items" :key="item.id">
              <td><button class="text-button" @click="openMatter(item.matter_id)">{{ item.summary }}</button>
                <p class="muted small">{{ describe(item.reason) }} · {{ date(item.created_at) }}</p></td>
              <td>{{ describe(item.notification_state) }}</td>
              <td>{{ item.resolution ?? '待跟进' }}</td>
              <td><ElButton v-if="item.status === 'pending'" size="small" @click="edit(item)">登记结果</ElButton></td>
            </tr>
            <tr v-if="!handoffs.items.length"><td colspan="4" class="empty">当前没有符合条件的人工待办。</td></tr>
          </tbody>
        </table>
        <ElButton v-if="handoffs.next_cursor" class="more" @click="loadList(true).catch(report)">加载更多</ElButton>
      </section>

      <section v-else-if="view === 'commerce'" class="panel" aria-label="业务连接与会员核验">
        <div v-if="capabilities" class="panel-head">
          <div><h2>{{ describe(capabilities.source) }}</h2>
            <p class="muted">{{ capabilities.available.map(describe).join('、') || '当前没有可用的商家查询接口。' }}</p></div>
          <ElTag size="small" type="info">官方入口{{ capabilities.official_service_configured ? '已配置' : '未配置' }}</ElTag>
        </div>
        <table>
          <thead><tr><th>渠道</th><th>会员渠道标识</th><th>核验状态</th><th>私密查询入口</th><th>更新</th></tr></thead>
          <tbody>
            <tr v-for="binding in bindings.items" :key="binding.id">
              <td>{{ binding.connection_id }}</td><td>{{ binding.sender_external_id }}</td>
              <td><ElTag size="small" :type="binding.status === 'verified' ? 'success' : 'info'">{{ describe(binding.status) }}</ElTag></td>
              <td>{{ binding.private_available ? '可用' : '未关联' }}</td><td>{{ date(binding.updated_at) }}</td>
            </tr>
            <tr v-if="!bindings.items.length"><td colspan="5" class="empty">当前没有会员核验记录。</td></tr>
          </tbody>
        </table>
        <ElButton v-if="bindings.next_cursor" class="more" @click="loadList(true).catch(report)">加载更多</ElButton>
      </section>
      <OpsMembers v-else-if="view === 'members'" :csrf="session.csrf_token" :initial-member="selectedMember" @matter="showMatter" />
      <OpsActivities v-else-if="view === 'activities'" :csrf="session.csrf_token" />
      <OpsInsights v-else-if="view === 'insights'" :csrf="session.csrf_token" @matter="showMatter" @member="showMember" @correction="correctSignal" />
      <OpsOutreach v-else-if="view === 'outreach'" :csrf="session.csrf_token" @matter="showMatter" @member="showMember" />
      <OpsKnowledge v-else :csrf="session.csrf_token" :source="correctionSource" @matter="showMatter" />

      <form v-if="editing" class="panel section" aria-label="处理待办" @submit.prevent="resolveHandoff">
        <h2>登记人工处理结果</h2><p class="muted">{{ editing.summary }}</p>
        <label>处理结果<textarea v-model="resolution" required maxlength="4000" rows="4" /></label>
        <label class="check"><input v-model="resume" type="checkbox" /> 将确认结果交回助手，按原会话可见范围回复</label>
        <div class="actions"><ElButton type="primary" native-type="submit" :loading="busy" :disabled="!resolution.trim()">保存结果</ElButton>
          <ElButton @click="editing = null">取消</ElButton></div>
      </form>

      <section v-if="detail && currentPage" class="panel section" aria-label="会话详情">
        <header class="panel-head"><h2>{{ detail.matter.subject }}</h2><div class="inline-controls">
          <ElButton text @click="correctMatter(detail)">建立纠错草稿</ElButton><ElButton text @click="selected = ''; detail = null">关闭详情</ElButton>
        </div></header>
        <ol class="messages">
          <li v-for="message in detail.messages" :key="message.id">
            <small>{{ describe(message.kind) }} · {{ date(message.occurred_at) }} <span v-if="message.delivery_state"> · {{ describe(message.delivery_state) }}</span></small>
            <p>{{ message.preview }}</p>
          </li>
        </ol>
        <div class="detail-grid">
          <div><h3>知识依据</h3><ul v-if="detail.evidence.length">
            <li v-for="source in detail.evidence" :key="source.knowledge_id + ':' + source.version">{{ source.title }} · v{{ source.version }}</li>
          </ul><p v-else class="muted">本事项暂无知识引用。</p></div>
          <div><h3>处理轨迹</h3><ul><li v-for="run in detail.runs" :key="run.id">
            {{ run.intents.map(describe).join('、') }} · {{ describe(run.outcome) }}<span v-if="run.error_code"> · {{ describe(run.error_code) }}</span>
          </li></ul></div>
          <div><h3>业务查询依据</h3><ul v-if="commerceChecks.items.length">
            <li v-for="check in commerceChecks.items" :key="check.id">
              {{ describe(check.operation) }} · {{ describe(check.result_code) }} · {{ describe(check.source) }} · {{ date(check.checked_at) }}
              <span v-if="check.public_reference"> · {{ check.public_reference }}</span>
            </li>
          </ul><p v-else class="muted">本事项暂无业务查询记录。</p>
            <ElButton v-if="commerceChecks.next_cursor" text @click="moreChecks().catch(report)">加载更多依据</ElButton>
          </div>
        </div>
      </section>
      <footer class="muted small status">本机 API {{ apiHealth === null ? '检查中' : apiHealth ? '正常' : '不可用' }} · 数据库 {{ databaseHealth === null ? '检查中' : databaseHealth ? '就绪' : '不可用' }}</footer>
    </main>
  </div>
</template>

<style src="./workspace.css"></style>
