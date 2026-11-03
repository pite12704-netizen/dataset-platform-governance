/** 浏览器仅缓存清单和批次身份；不缓存原图字节或导入令牌。 */
import {DirectoryDraft} from './directory-import';

async function database(): Promise<IDBDatabase> {
  if(!globalThis.indexedDB) throw new Error('浏览器不支持续传存储，请使用支持文件夹上传的桌面浏览器');
  return new Promise((resolve,reject)=>{
    const request=indexedDB.open('dataset-directory-imports',1);
    request.onupgradeneeded=()=>request.result.createObjectStore('drafts');
    request.onsuccess=()=>resolve(request.result);
    request.onerror=()=>reject(new Error('无法打开续传存储，请检查浏览器存储权限'));
    request.onblocked=()=>reject(new Error('续传存储被其他页面占用，请关闭旧页面后重试'));
  });
}
function scope(ownerId:string,versionId:string) {return `${ownerId}:${versionId}`;}

export async function readDirectoryDraft(ownerId:string,versionId:string):Promise<DirectoryDraft|undefined> {
  const db=await database();
  try {return await new Promise((resolve,reject)=>{
    const transaction=db.transaction('drafts','readonly');
    const request=transaction.objectStore('drafts').get(scope(ownerId,versionId));
    transaction.oncomplete=()=>{
      const value=request.result;
      if(value && (value.ownerId!==ownerId || value.versionId!==versionId)) {reject(new Error('续传身份不匹配'));return;}
      resolve(value);
    };
    transaction.onerror=()=>reject(new Error('读取续传记录失败'));
    transaction.onabort=()=>reject(new Error('读取续传记录已取消'));
  });}finally{db.close();}
}

export async function saveDirectoryDraft(value:DirectoryDraft):Promise<void> {
  const db=await database();
  try {await new Promise<void>((resolve,reject)=>{
    const transaction=db.transaction('drafts','readwrite');
    transaction.objectStore('drafts').put(value,scope(value.ownerId,value.versionId));
    transaction.oncomplete=()=>resolve();
    transaction.onerror=()=>reject(new Error('无法保存续传记录，浏览器空间可能不足；上传已停止'));
    transaction.onabort=()=>reject(new Error('续传记录保存失败，上传已停止'));
  });}finally{db.close();}
}

export async function clearDirectoryDraft(ownerId:string,versionId:string):Promise<void> {
  const db=await database();
  try {await new Promise<void>((resolve,reject)=>{
    const transaction=db.transaction('drafts','readwrite');
    transaction.objectStore('drafts').delete(scope(ownerId,versionId));
    transaction.oncomplete=()=>resolve();
    transaction.onerror=()=>reject(new Error('清除本机续传记录失败'));
    transaction.onabort=()=>reject(new Error('清除本机续传记录已取消'));
  });}finally{db.close();}
}
