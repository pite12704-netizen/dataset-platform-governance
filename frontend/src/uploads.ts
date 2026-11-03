/** 【学习标注：可靠上传】仅重试幂等上传操作；会话创建前保存幂等键，完成后保留记录。 */
import { createSHA256 } from 'hash-wasm';
import { api, ApiError, bytes, key, Row } from './api';

type Resume = { idempotencyKey: string; uploadId?: string; previewAllowed: boolean };
type Options = {
  userId: string; versionId: string; previewAllowed: boolean; config: Row;
  signal: AbortSignal; onStatus: (message: string) => void; onProgress: (ratio: number) => void;
};

export function fileProblem(file: Pick<File, 'name' | 'size'>, config: Row): string {
  const limit = config.max_upload_bytes ?? config.max_file_bytes;
  if (!file.size) return '不能上传空文件';
  if (file.name.length > 240) return '文件名不能超过 240 个字符';
  if (limit && file.size > limit) return `超过当前单文件上限 ${bytes(limit)}`;
  const extension = file.name.split('.').pop()?.toLowerCase();
  if (config.supported_formats && !config.supported_formats.includes(extension)) return '暂不支持此文件格式';
  if (config.dataset_modality && config.modality_formats?.[config.dataset_modality] &&
      !config.modality_formats[config.dataset_modality].includes(extension)) return '文件格式与此数据集类型不符，请选择匹配类型或多模态数据集';
  return '';
}

function checkPaused(signal: AbortSignal) {
  if (signal.aborted) throw new DOMException('上传已暂停', 'AbortError');
}

export function waitForRetry(milliseconds: number, signal: AbortSignal, onTick?: (seconds:number)=>void): Promise<void> {
  checkPaused(signal);
  return new Promise((resolve, reject) => {
    const until=Date.now()+milliseconds;
    onTick?.(Math.ceil(milliseconds/1000));
    const ticker=onTick?setInterval(()=>onTick(Math.max(0,Math.ceil((until-Date.now())/1000))),1000):undefined;
    const abort = () => {
      clearTimeout(timer);
      clearInterval(ticker);
      signal.removeEventListener('abort', abort);
      reject(new DOMException('上传已暂停', 'AbortError'));
    };
    const timer = setTimeout(() => { clearInterval(ticker);signal.removeEventListener('abort', abort); resolve(); }, milliseconds);
    signal.addEventListener('abort', abort, { once: true });
  });
}

export async function uploadRequest<T = Row>(path: string, init: RequestInit, signal: AbortSignal,
  onStatus: (message: string) => void): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    checkPaused(signal);
    const request = new AbortController();
    const abort = () => request.abort();
    signal.addEventListener('abort', abort, { once: true });
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; request.abort(); }, 90000);
    let failure: unknown;
    try {
      return await api<T>(path, { ...init, signal: request.signal });
    } catch (error) {
      failure = error;
    } finally {
      clearTimeout(timeout);
      signal.removeEventListener('abort', abort);
    }
    checkPaused(signal);
    const retryable = timedOut || failure instanceof TypeError || (failure instanceof ApiError &&
      [429, 500, 502, 503, 504].includes(failure.status));
    if (!retryable || attempt >= 8) throw failure;
    const seconds = failure instanceof ApiError && [429, 503].includes(failure.status)
      ? failure.retryAfter : Math.min(30, 2 ** attempt);
    onStatus(`${failure instanceof ApiError && failure.status === 429 ? '请求较多' : '连接暂不可用'}，${seconds} 秒后自动重试（${attempt + 1}/8），可暂停后续传。`);
    await waitForRetry(seconds * 1000, signal,remaining=>onStatus(
      `${failure instanceof ApiError && failure.status===429?'限流保护':'连接暂不可用'}，${remaining} 秒后自动重试（${attempt+1}/8），已上传内容会保留，可暂停。`));
  }
}

export async function uploadFile(file: File, options: Options): Promise<{ reused: boolean; assetId: string }> {
  const { userId, versionId, previewAllowed, config, signal, onStatus, onProgress } = options;
  const problem = fileProblem(file, config);
  if (problem) throw new Error(`${file.name}：${problem}`);
  checkPaused(signal);
  onStatus(`计算校验和：${file.name}`);
  const hash = await createSHA256();
  hash.init();
  for (let position = 0; position < file.size; position += 8 * 1024 ** 2) {
    checkPaused(signal);
    hash.update(new Uint8Array(await file.slice(position, position + 8 * 1024 ** 2).arrayBuffer()));
  }
  const digest = hash.digest();
  const resumeKey = `upload:${userId}:${versionId}:${digest}:${file.name}`;
  const call = (path: string, init: RequestInit = {}) => uploadRequest<Row>(path, init, signal, onStatus);
  const isActive = (upload: Row) => ['uploading', 'finalizing'].includes(upload.status) && new Date(
    /(?:Z|[+-]\d\d:\d\d)$/.test(upload.expires_at) ? upload.expires_at : upload.expires_at + 'Z') > new Date();
  const save = (resume: Resume) => {
    try { localStorage.setItem(resumeKey, JSON.stringify(resume)); }
    catch { throw new Error('浏览器无法保存续传记录，请允许本站使用本地存储或清理浏览器空间后重试。'); }
  };
  let resume: Resume | undefined;
  const saved = localStorage.getItem(resumeKey);
  if (saved) {
    try {
      const parsed = JSON.parse(saved);
      if (typeof parsed?.idempotencyKey === 'string') resume = parsed;
    } catch {
      // 兼容旧页面保存的纯上传 ID，后面读取服务端真实状态。
      resume = { idempotencyKey: '', uploadId: saved, previewAllowed };
    }
  }
  let upload: Row | undefined;
  if (resume?.uploadId) {
    try {
      const previous = await call(`/uploads/${resume.uploadId}`);
      if (previous.status === 'completed' && previous.asset_id) {
        onProgress(1);
        return { reused: true, assetId: previous.asset_id };
      }
      if (isActive(previous)) {
        upload = previous;
        resume = { idempotencyKey: previous.idempotency_key, uploadId: previous.id,
          previewAllowed: previous.preview_allowed };
        save(resume);
      } else {
        if (['uploading', 'finalizing', 'aborting'].includes(previous.status)) await call(`/uploads/${previous.id}`, { method: 'DELETE' });
        resume = undefined;
      }
    } catch (error) {
      // 权限、限流和网络失败不能被当成“会话不存在”，否则可能重复创建文件。
      if (error instanceof ApiError && error.status === 404) resume = undefined;
      else throw error;
    }
  }
  if (!upload) {
    resume ??= { idempotencyKey: key(), previewAllowed };
    for (let recovery = 0; ; recovery++) {
      save(resume); // POST 返回途中断网，也能用同一个幂等键找回会话。
      upload = await call(`/versions/${versionId}/uploads`, { method: 'POST', body: JSON.stringify({
        filename: file.name, size: file.size, sha256: digest,
        idempotency_key: resume.idempotencyKey, preview_allowed: resume.previewAllowed
      }) });
      if ((upload.status === 'completed' && upload.asset_id) || isActive(upload)) {
        resume.uploadId = upload.id;
        save(resume);
        break;
      }
      // 没收到创建响应的旧会话也可能已过期；先释放配额，再换幂等键创建。
      if (['uploading', 'finalizing', 'aborting'].includes(upload.status)) await call(`/uploads/${upload.id}`, { method: 'DELETE' });
      if (recovery >= 1) throw new Error('上传会话不可用，请刷新页面后重试。');
      resume = { idempotencyKey: key(), previewAllowed };
    }
  }
  if (upload.status === 'completed') {
    onProgress(1);
    return { reused: true, assetId: upload.asset_id };
  }
  const parts = Math.ceil(file.size / upload.part_size);
  for (let part = 1; part <= parts; part++) {
    checkPaused(signal);
    onStatus(`上传 ${file.name} · 分片 ${part}/${parts}`);
    if (!upload.parts[String(part)]) {
      await call(`/uploads/${upload.id}/parts/${part}`, { method: 'PUT',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: file.slice((part - 1) * upload.part_size, Math.min(part * upload.part_size, file.size)) });
    }
    onProgress(part / parts);
  }
  onStatus(`确认上传：${file.name}`);
  const result = await call(`/uploads/${upload.id}/complete`, { method: 'POST', body: '{}' });
  // 不删除本地记录；再次选择整批文件时查证服务端 completed 状态并跳过。
  return { reused: false, assetId: result.asset_id };
}
