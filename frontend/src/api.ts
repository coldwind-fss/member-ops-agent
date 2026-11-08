export interface Session {
  operator_id: string
  display_name: string
  csrf_token: string
}

export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export interface Matter {
  id: string
  version: number
  status: string
  conversation_kind: string
  conversation_external_id: string
  participant_label: string
  subject: string
  updated_at: string
}

export interface MatterDetail {
  matter: Matter
  messages: { id: string; kind: string; preview: string; occurred_at: string; delivery_state: string | null }[]
  evidence: { knowledge_id: string; version: number; title: string }[]
  runs: { id: string; intents: string[]; outcome: string; error_code: string | null; recorded_at: string }[]
  handoff_id: string | null
}

export interface Handoff {
  id: string
  matter_id: string
  version: number
  status: 'pending' | 'resolved'
  summary: string
  reason: string
  notification_state: string | null
  resolution: string | null
  created_at: string
  updated_at: string
}

export interface CommerceCapabilities {
  mode: 'fixture' | 'unconfigured'
  source: string
  available: string[]
  official_service_configured: boolean
}

export interface MemberBinding {
  id: string
  connection_id: string
  sender_external_id: string
  status: 'verified' | 'unbound'
  private_available: boolean
  updated_at: string
}

export interface CommerceCheck {
  id: string
  operation: string
  result_code: string
  source: string
  public_reference: string | null
  checked_at: string
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body) headers.set('Content-Type', 'application/json')
  const response = await fetch('/api/v1' + path, { ...options, headers, cache: 'no-store' })
  if (response.status === 204) return undefined as T
  const data = await response.json().catch(() => null)
  if (!response.ok) throw new ApiError(response.status, data?.message ?? '服务暂时不可用。')
  return data as T
}
