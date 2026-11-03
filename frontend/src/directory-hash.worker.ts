/** 哈希在独立 Worker 中执行，每次最多读取 1MiB；不访问网络或业务凭据。 */
import {createSHA256} from 'hash-wasm';

globalThis.onmessage=async(event:MessageEvent<{file:Blob}>)=>{
  try {
    const hash=await createSHA256(); hash.init();
    const file=event.data.file;
    for(let position=0;position<file.size;position+=1024**2)
      hash.update(new Uint8Array(await file.slice(position,position+1024**2).arrayBuffer()));
    globalThis.postMessage({sha256:hash.digest()});
  }catch{globalThis.postMessage({error:'无法读取原图，请检查文件是否被修改或删除'});}
};
