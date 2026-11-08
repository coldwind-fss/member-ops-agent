import { ref } from 'vue'
import { api } from './api'

export interface Items<T> { items: T[] }

export const localDateTime = (value: string) => value ? new Date(value).toISOString() : null
export const displayTime = (value: string | null) => value ? new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short',
}).format(new Date(value)) : '—'

export function useOps(csrf: () => string) {
  const error = ref('')
  const notice = ref('')
  const busy = ref(false)
  let pending: { signature: string; key: string } | null = null

  async function run(task: () => Promise<void>, success = '') {
    if (busy.value) return
    busy.value = true
    error.value = ''
    notice.value = ''
    try {
      await task()
      notice.value = success
    } catch (problem) {
      error.value = problem instanceof Error ? problem.message : '请求未完成。'
    } finally {
      busy.value = false
    }
  }

  async function save<T>(path: string, input: unknown) {
    const body = JSON.stringify(input)
    const signature = path + body
    if (!pending || pending.signature !== signature) pending = { signature, key: crypto.randomUUID() }
    const result = await api<T>(path, { method: 'POST', body,
      headers: { 'X-CSRF-Token': csrf(), 'Idempotency-Key': pending.key } })
    pending = null
    return result
  }

  return { error, notice, busy, run, save }
}
