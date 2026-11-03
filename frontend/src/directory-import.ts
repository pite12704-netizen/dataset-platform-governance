/** 【学习标注：网页目录导入】文件清单与原图分离，固定清单后复用批次 API。 */
import {createSHA256} from 'hash-wasm';
import {Row} from './api';

export type DirectoryFile = {path: string; file: File};
export type DirectoryEntry = {path: string; external_id: string; filename: string; size: number; sha256: string;
  source_revision: string; source_group: string; source_annotation: Row; source: string; license: string};
export type DirectoryDraft = {ownerId: string; versionId: string; createKey: string; sourceId: string;
  source: string; license: string; entries: DirectoryEntry[]; bytes: number; fingerprint: string; batchId?: string};
export type DirectorySelection = {files: DirectoryFile[]; skipped: {path: string; reason: string}[];
  blocked: {path: string; reason: string}[]; bytes: number};
type Call = (path: string, init?: RequestInit) => Promise<Row>;
type Digest = (file: Blob, signal: AbortSignal) => Promise<string>;
export const DIRECTORY_PAGE_SIZE = 100;

export function checkDirectorySignal(signal: AbortSignal) {
  if(signal.aborted) throw new DOMException('已暂停，请重新选择原文件夹继续', 'AbortError');
}

export function selectDirectory(files: Iterable<File>, config: Row): DirectorySelection {
  const selected: DirectorySelection = {files: [], skipped: [], blocked: [], bytes: 0};
  const seen = new Set<string>();
  const limit = Math.min(20 * 1024**2, config.max_upload_bytes ?? config.max_file_bytes ?? 20 * 1024**2);
  for(const file of files) {
    const relative = file.webkitRelativePath;
    const parts = relative?.split('/') ?? [];
    const path = parts.slice(1).join('/'); // 不依赖用户电脑的盘符或根文件夹名称。
    let reason = '';
    if(parts.length < 2 || parts.some(p=>!p || p==='.' || p==='..') || /[\\:\x00-\x1f]/.test(relative))
      reason = '请选择本地文件夹；文件路径不符合要求';
    else if(seen.has(path)) reason = '文件夹内存在重复相对路径';
    seen.add(path);
    if(reason) { selected.blocked.push({path: relative || file.name, reason}); continue; }
    if(!/\.(jpe?g|png|webp)$/i.test(file.name)) {
      selected.skipped.push({path, reason: '仅接收 JPG/JPEG/PNG/WebP 图片'}); continue;
    }
    if(file.name.length>240) reason = '图片文件名超过 240 字符';
    else if(!file.size) reason = '图片为空';
    else if(file.size>limit) reason = '超过当前图片接收上限';
    if(reason) { selected.blocked.push({path,reason}); continue; }
    selected.files.push({path,file}); selected.bytes+=file.size;
  }
  if(selected.files.length>100000) throw new Error('单批最多 100000 张图片，请按来源目录分批接入');
  selected.files.sort((a,b)=>a.path<b.path?-1:a.path>b.path?1:0);
  return selected;
}

export async function prepareDirectory(selection: DirectorySelection, identity: Omit<DirectoryDraft,'entries'|'bytes'|'fingerprint'>,
  digest: Digest, signal: AbortSignal, onProgress: (count:number,total:number)=>void,
  previous?: DirectoryDraft): Promise<DirectoryDraft> {
  if(selection.blocked.length || !selection.files.length) throw new Error('请先处理不合格图片，并选择包含图片的文件夹');
  if(!identity.source.trim() || identity.source.length>2000 || !identity.license.trim() || identity.license.length>1000)
    throw new Error('请填写真实来源和使用许可');
  if(previous && (previous.ownerId!==identity.ownerId || previous.versionId!==identity.versionId ||
    previous.createKey!==identity.createKey || previous.source!==identity.source || previous.license!==identity.license))
    throw new Error('此续传记录的用户、版本或来源说明不匹配');
  const entries: DirectoryEntry[] = [], encoder = new TextEncoder(), manifest = await createSHA256(); manifest.init();
  for(const {path,file} of selection.files) {
    checkDirectorySignal(signal);
    const sha256 = await digest(file,signal);
    const pathHash = await digest(new Blob([encoder.encode(path)]),signal);
    const entry: DirectoryEntry = {path, external_id:'path-'+pathHash, filename:file.name, size:file.size, sha256,
      source_revision:'sha256:'+sha256, source_group:'', source_annotation:{}, source:identity.source, license:identity.license};
    entries.push(entry); manifest.update(encoder.encode(JSON.stringify(entry)+'\n'));
    if(entries.length%100===0 || entries.length===selection.files.length) onProgress(entries.length,selection.files.length);
  }
  checkDirectorySignal(signal);
  const fingerprint=manifest.digest();
  if(previous && (previous.fingerprint!==fingerprint || previous.entries.length!==entries.length || previous.bytes!==selection.bytes))
    throw new Error('文件夹内容与原批次不一致，请重新选择未修改的原图；不能向旧批次替换图片');
  return {...identity,entries,bytes:selection.bytes,fingerprint};
}

export function directoryManifestEntry(entry: DirectoryEntry) {
  const {path: _localPath,...remote}=entry;
  return remote;
}

async function sendDirectoryFile(entry: DirectoryEntry, file: File, itemId: string, batchId: string, call: Call,
  signal: AbortSignal) {
  checkDirectorySignal(signal);
  const upload=await call(`/imports/${batchId}/items/${itemId}/upload`,{method:'POST',body:'{}'});
  if(upload.status==='completed') return;
  if(upload.status!=='uploading') throw new Error('文件正在完成或回收，请稍后点击继续上传');
  if(!Number.isSafeInteger(upload.part_size) || upload.part_size<1 || upload.part_size>32*1024**2)
    throw new Error('服务器分片设置无效，已停止上传');
  const whole=await createSHA256(); whole.init();
  for(let position=0,number=1;position<file.size;position+=upload.part_size,number++) {
    checkDirectorySignal(signal);
    const block=new Uint8Array(await file.slice(position,Math.min(position+upload.part_size,file.size)).arrayBuffer());
    whole.update(block);
    const hash=await createSHA256(); hash.init(); hash.update(block);
    const stored=upload.parts?.[String(number)];
    if(stored?.size===block.length && stored.sha256===hash.digest()) continue;
    await call(`/uploads/${upload.id}/parts/${number}`,{method:'PUT',headers:{'Content-Type':'application/octet-stream'},
      body:block as BodyInit});
  }
  checkDirectorySignal(signal);
  if(file.size!==entry.size || whole.digest()!==entry.sha256) throw new Error('图片在上传期间发生变化，请恢复原图后续传');
  await call(`/uploads/${upload.id}/complete`,{method:'POST',body:'{}'});
}

export async function transferDirectory(draft: DirectoryDraft, selection: DirectorySelection, call: Call,
  save: (value:DirectoryDraft)=>Promise<void>, signal: AbortSignal,
  onProgress:(count:number,total:number,path:string)=>void): Promise<Row> {
  checkDirectorySignal(signal);
  const files=new Map(selection.files.map(item=>[item.path,item.file]));
  if(draft.entries.length!==files.size || draft.entries.some(entry=>files.get(entry.path)?.size!==entry.size))
    throw new Error('请重新选择原批次的完整文件夹');
  // 网络创建前已经保存 createKey；响应丢失时重用相同幂等键，不保存服务令牌。
  await save(draft); checkDirectorySignal(signal);
  if(!draft.batchId) {
    const batch=await call('/imports',{method:'POST',body:JSON.stringify({version_id:draft.versionId,source_id:draft.sourceId,
      expected_files:draft.entries.length,expected_bytes:draft.bytes,idempotency_key:draft.createKey})});
    draft={...draft,batchId:batch.id};
    await save(draft); // 保存失败时不发送原图；下一次仍能按 createKey 找回批次。
  }
  const batchId=draft.batchId!;
  const batch=await call(`/imports/${batchId}`);
  if(batch.version_id!==draft.versionId || batch.source_id!==draft.sourceId || batch.expected_files!==draft.entries.length ||
    batch.expected_bytes!==draft.bytes) throw new Error('服务器批次与本地清单不一致，已停止');
  if(batch.status==='cancelled') throw new Error('导入批次已取消，不能继续上传');
  if(batch.sealed) {onProgress(draft.entries.length,draft.entries.length,'文件已完整传输，查看后台检查结果');return batch;}
  let done=0;
  for(let offset=0;offset<draft.entries.length;offset+=DIRECTORY_PAGE_SIZE) {
    checkDirectorySignal(signal);
    const page=draft.entries.slice(offset,offset+DIRECTORY_PAGE_SIZE);
    const response=await call(`/imports/${batchId}/items`,{method:'POST',body:JSON.stringify({items:page.map(directoryManifestEntry)})});
    const registered=new Map<string,Row>((response.items??[]).map((item:Row)=>[item.external_id,item]));
    for(const entry of page) {
      checkDirectorySignal(signal);
      const item=registered.get(entry.external_id);
      if(!item || item.sha256!==entry.sha256 || item.size!==entry.size) throw new Error('服务器文件清单不匹配，已停止');
      onProgress(done,draft.entries.length,entry.path);
      await sendDirectoryFile(entry,files.get(entry.path)!,item.id,batchId,call,signal);
      done++;onProgress(done,draft.entries.length,entry.path);
    }
  }
  checkDirectorySignal(signal);
  return call(`/imports/${batchId}/finalize`,{method:'POST',body:'{}'});
}
