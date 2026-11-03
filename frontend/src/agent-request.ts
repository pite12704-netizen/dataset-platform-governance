/** 【学习标注：逻辑请求】断网重试保持原编号与原参数，刷新页面后按登录账号恢复。 */
import { ApiError } from './api';

export type ChatRequest = { message: string; intent: string; version_ids: string[];
  conversation_id: string | null; request_id: string };

export function newChatRequest(message: string, intent: string, versionIds: string[], conversation?: string): ChatRequest {
  return { message, intent, version_ids: [...versionIds], conversation_id: conversation || null, request_id: crypto.randomUUID() };
}
export function keepPending(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status >= 500 || [409, 429].includes(error.status);
}
export function savePending(userId: string, payload?: ChatRequest): void {
  try {
    const key = 'dp:pending-agent:' + userId;
    if (payload) sessionStorage.setItem(key, JSON.stringify(payload)); else sessionStorage.removeItem(key);
  } catch { /* 浏览器禁止存储时仍保留当前页面内的幂等重试。 */ }
}
export function readPending(userId: string): ChatRequest | undefined {
  try {
    const p = JSON.parse(sessionStorage.getItem('dp:pending-agent:' + userId) || 'null');
    if (p && typeof p.message === 'string' && p.message.length <= 2000 && typeof p.intent === 'string'
      && Array.isArray(p.version_ids) && p.version_ids.length <= 4 && p.version_ids.every((v: unknown) => typeof v === 'string' && /^[a-f0-9]{32}$/.test(v))
      && (p.conversation_id === null || /^[a-f0-9]{32}$/.test(p.conversation_id))
      && typeof p.request_id === 'string' && /^[a-zA-Z0-9_-]{8,64}$/.test(p.request_id)) return p;
  } catch { /* 无效记录不用于发送。 */ }
}
export function appendResult(history: Record<string, any>[], result: Record<string, any>): Record<string, any>[] {
  return [...history.filter(h => h.run_id !== result.run_id), result];
}
