<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElButton, ElTag } from 'element-plus'
import { api } from './api'
import { displayTime, localDateTime, type Items, useOps } from './ops'

interface Knowledge { id: string; version: number; title: string; body: string; topic: string; product: string;
  aliases: string[]; keywords: string[]; valid_from: string; valid_until: string | null }
interface Draft { id: string; expected_version: number; knowledge_id: string; published_version: number | null;
  config: Pick<Knowledge, 'title' | 'body'>; matter_id: string | null; signal_id: string | null; category: string }
interface Case { question: string; answer: string | null; reason: string | null; sources: { id: string; version: number; title: string }[]; passed: boolean }
interface Verification { id: string; cases: Case[]; created_at: string }
interface Catalogue extends Items<Knowledge> { drafts: Draft[]; verifications: Verification[] }

const props = defineProps<{ csrf: string; source?: { matter_id?: string; signal_id?: string; knowledge_id?: string; question?: string } }>()
const emit = defineEmits<{ matter: [id: string] }>()
const { busy, error, notice, run, save } = useOps(() => props.csrf)
const data = ref<Catalogue>({ items: [], drafts: [], verifications: [] })
const categories: Record<string, string> = { missing: '资料缺失', outdated: '资料过期', intent: '理解错误', query: '查询失败', permission: '权限或流程错误' }
const blank = () => ({ title: '', body: '', topic: 'care', product: '', aliases: '', keywords: '',
  valid_from: '', valid_until: '', knowledge_id: '', category: 'missing' })
const form = ref(blank())
const verify = ref({ draft_id: '', questions: props.source?.question ?? '', expected_text: '', intent: 'public_knowledge' })
const words = (value: string) => value.split(/[、,，\n]/).map(term => term.trim()).filter(Boolean)

function fill(item: Knowledge) {
  form.value = { ...blank(), knowledge_id: item.id, title: item.title, body: item.body, topic: item.topic, product: item.product,
    aliases: item.aliases.join('、'), keywords: item.keywords.join('、'), category: 'outdated' }
}
async function load() { data.value = await api<Catalogue>('/knowledge') }
async function createDraft() {
  await run(async () => {
    const formData = form.value
    await save('/knowledge/drafts', { ...formData, knowledge_id: formData.knowledge_id || null,
      matter_id: props.source?.matter_id ?? null, signal_id: props.source?.signal_id ?? null,
      keywords: words(formData.keywords), aliases: words(formData.aliases),
      valid_from: localDateTime(formData.valid_from), valid_until: localDateTime(formData.valid_until) })
    await load()
  }, '知识草稿已保存。')
}
async function publish(draft: Draft) {
  await run(async () => {
    await save('/knowledge/drafts/' + draft.id + '/publish', { expected_version: draft.expected_version })
    verify.value.draft_id = draft.id
    await load()
  }, '知识版本已发布。')
}
async function runCases() {
  await run(async () => {
    await save('/knowledge/verify', { ...verify.value, questions: verify.value.questions.split(/\r?\n/).map(question => question.trim()).filter(Boolean) })
    await load()
  }, '验证结果已保存。')
}
onMounted(() => run(async () => {
  await load()
  const source = data.value.items.filter(item => item.id === props.source?.knowledge_id).sort((a, b) => b.version - a.version)[0]
  if (source) fill(source)
}))
</script>

<template>
  <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="notice" role="status" class="notice">{{ notice }}</p>
  <section class="panel">
    <div class="panel-head"><h2>品牌知识与版本</h2><ElButton @click="run(load)">刷新</ElButton></div>
    <table><thead><tr><th>标题</th><th>商品与主题</th><th>生效范围（上海）</th><th>操作</th></tr></thead><tbody>
      <tr v-for="item in data.items" :key="item.id + ':' + item.version">
        <td><details><summary>{{ item.title }} · v{{ item.version }}</summary><p>{{ item.body }}</p></details></td>
        <td>{{ item.product || '通用' }} · {{ item.topic }}</td><td>{{ displayTime(item.valid_from) }} 至 {{ item.valid_until ? displayTime(item.valid_until) : '长期' }}</td>
        <td><ElButton size="small" @click="fill(item)">基于此条修订</ElButton></td>
      </tr><tr v-if="!data.items.length"><td colspan="4" class="empty">暂无已发布资料。</td></tr>
    </tbody></table>
  </section>
  <section class="panel section" aria-label="知识草稿">
    <h2>{{ form.knowledge_id ? '修订知识' : '新增知识草稿' }}</h2>
    <p v-if="source" class="muted">此草稿关联当前纠错来源。</p>
    <form class="form-grid" @submit.prevent="createDraft">
      <label>标题<input v-model="form.title" required maxlength="120" /></label>
      <label>纠错类别<select v-model="form.category"><option v-for="(label, key) in categories" :key="key" :value="key">{{ label }}</option></select></label>
      <label>主题<select v-model="form.topic"><option value="care">洗护</option><option value="brand">品牌</option><option value="after_sale">售后</option><option value="product">商品</option><option value="shipping">配送</option><option value="activity">活动</option></select></label>
      <label>适用商品（留空为通用）<input v-model="form.product" maxlength="120" /></label>
      <label>商品别名（以顿号分隔）<input v-model="form.aliases" /></label>
      <label>检索关键词（以顿号分隔）<input v-model="form.keywords" required /></label>
      <label>生效时间（本机时区，留空即时）<input v-model="form.valid_from" type="datetime-local" /></label>
      <label>截止时间（本机时区，可选）<input v-model="form.valid_until" type="datetime-local" /></label>
      <label class="form-wide">确认后的口径<textarea v-model="form.body" required rows="4" maxlength="6000" /></label>
      <div class="actions"><ElButton native-type="submit" type="primary" :loading="busy">保存草稿</ElButton><ElButton @click="form = blank()">清空表单</ElButton></div>
    </form>
    <h3 class="ops-heading">草稿与发布记录</h3>
    <table><thead><tr><th>草稿</th><th>类别与依据</th><th>状态</th><th>操作</th></tr></thead><tbody>
      <tr v-for="draft in data.drafts" :key="draft.id">
        <td><details><summary>{{ draft.config.title }}</summary><p>{{ draft.config.body }}</p></details></td>
        <td>{{ categories[draft.category] }} <button v-if="draft.matter_id" class="text-button" @click="emit('matter', draft.matter_id)">原事项</button><span v-else-if="draft.signal_id"> · 来自群复盘</span></td>
        <td><ElTag size="small">{{ draft.published_version ? '已发布 v' + draft.published_version : '待发布' }}</ElTag></td>
        <td><ElButton v-if="!draft.published_version" size="small" :disabled="busy" @click="publish(draft)">确认发布</ElButton></td>
      </tr><tr v-if="!data.drafts.length"><td colspan="4" class="empty">暂无知识草稿。</td></tr>
    </tbody></table>
  </section>
  <section class="panel section" aria-label="答复验证">
    <h2>答复验证</h2>
    <form class="form-grid" @submit.prevent="runCases">
      <label>已发布草稿<select v-model="verify.draft_id" required><option value="" disabled>请选择版本</option>
        <option v-for="item in data.drafts.filter(draft => draft.published_version)" :key="item.id" :value="item.id">{{ item.config.title }} · v{{ item.published_version }}</option></select></label>
      <label>问题类型<select v-model="verify.intent"><option value="public_knowledge">公开知识</option><option value="public_after_sale_policy">售后规则</option></select></label>
      <label>原问题与典型改写（每行一个，最多四条）<textarea v-model="verify.questions" required rows="4" /></label>
      <label>预期答复包含<input v-model="verify.expected_text" required maxlength="200" /></label>
      <div class="actions"><ElButton type="primary" native-type="submit" :loading="busy">运行验证</ElButton></div>
    </form>
    <details v-for="item in data.verifications" :key="item.id" class="signal-bucket">
      <summary>{{ displayTime(item.created_at) }} · {{ item.cases.filter(entry => entry.passed).length }} / {{ item.cases.length }} 通过</summary>
      <ul class="messages"><li v-for="entry in item.cases" :key="entry.question"><strong>{{ entry.question }}</strong>
        <p>{{ entry.answer || entry.reason }}</p><small>{{ entry.sources.map(source => source.title + ' v' + source.version).join('、') }}</small></li></ul>
    </details>
  </section>
</template>
