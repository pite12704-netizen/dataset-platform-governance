/** 一个导入操作只持有一个 Worker；暂停/离开立即终止文件读取。 */
import {checkDirectorySignal} from './directory-import';

export function directoryHasher(signal:AbortSignal) {
  checkDirectorySignal(signal);
  const worker=new Worker(new URL('./directory-hash.worker.ts',import.meta.url),{type:'module'});
  const hash=(file:Blob):Promise<string>=>new Promise((resolve,reject)=>{
    checkDirectorySignal(signal);
    const cleanup=()=>{worker.onmessage=null;worker.onerror=null;signal.removeEventListener('abort',abort);};
    const abort=()=>{cleanup();worker.terminate();reject(new DOMException('已暂停','AbortError'));};
    worker.onmessage=event=>{cleanup();event.data.error?reject(new Error(event.data.error)):resolve(event.data.sha256);};
    worker.onerror=()=>{cleanup();reject(new Error('文件校验未完成，请检查浏览器是否支持目录上传'));};
    signal.addEventListener('abort',abort,{once:true});worker.postMessage({file});
  });
  return {hash,close:()=>worker.terminate()};
}
