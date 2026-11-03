/** 【学习标注：前端 API 层】统一拼接 /api/v1、携带 Cookie、处理 JSON 错误和常用格式化。 */
export type Row = Record<string, any>;
export class ApiError extends Error { constructor(message: string, public status: number, public retryAfter = 1) { super(message); } }
export function retryAfterSeconds(value: string | null): number {
  const seconds = Number(value);
  if (value && Number.isFinite(seconds) && seconds >= 0) return Math.max(1, Math.ceil(seconds));
  const until = value ? Date.parse(value) : NaN;
  return Number.isFinite(until) ? Math.max(1, Math.ceil((until - Date.now()) / 1000)) : 1;
}
export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  // 所有页面都从这里访问后端；写请求额外携带 X-Requested-With，供服务端做 CSRF 校验。
  const response = await fetch('/api/v1' + path, { ...init, credentials: 'include', headers: {
    'Content-Type': 'application/json', 'X-Requested-With': 'dataset-platform', ...init.headers
  }});
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(data.message || '请求未完成，请重试', response.status,
      retryAfterSeconds(response.headers.get('Retry-After')));
  }
  return response.json();
}
export const post = (path: string, data: any = {}) => api(path, { method: 'POST', body: JSON.stringify(data) });
export const put = (path: string, data: any) => api(path, { method: 'PUT', body: JSON.stringify(data) });
export const remove = (path: string) => api(path, { method: 'DELETE' });
export const key = () => crypto.randomUUID();
export const bytes = (n: number = 0): string => n >= 1024**3 ? `${(n / 1024**3).toFixed(1)} GB` : n >= 1024**2 ? `${(n / 1024**2).toFixed(1)} MB` : n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`;
export const date = (v?: string) => v ? new Date(v.endsWith('Z') ? v : v + 'Z').toLocaleString('zh-CN') : '—';
export const labels: Record<string, string> = { draft: '草稿', pending_review: '待发布审核', published: '已发布', rejected: '已退回', pending: '待处理', approved: '已通过', clean: '检查通过', quarantine: '检查中', queued: '排队中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消', active: '正常', unlisted: '已下架', blocked: '已封禁', uploading: '上传中', open: '开放下载', application: '申请获取', public: '公开', organization: '组织内', selected: '指定成员', private: '仅自己', text: '文本', image: '图像', audio: '音频', video: '视频', tabular: '表格', multimodal: '多模态', admin: '管理员', reviewer: '审核人', evaluator:'独立测评员', contributor: '贡献者', member: '成员', scan: '文件检查', quality: '质量检查', convert: '格式转换', package: '数据打包', vision: '人物识别', dedup: '重复检测', evaluate: '真值评测', split: '分组划分', curate: '生成去重版本', vision_plan:'自动视觉计划', redacted:'内容已清除', withdrawn:'已撤回', release: '正式训练交付', raw:'原始数据', curated:'已治理交付', certified:'高质量认证' };
