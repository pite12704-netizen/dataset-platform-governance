/** 预览请求背压：有限并发、仅合并进行中的请求、取消及临时失败退避。 */
type Failure = Error & {status?: number; retryAfter?: number};
type Waiter = {start:()=>void; cancel:()=>void};
type Entry = {controller:AbortController; listeners:Set<object>; promise:Promise<unknown>};
const reason=(signal:AbortSignal)=>signal.reason||new DOMException('预览已取消','AbortError');

export function previewDelay(milliseconds:number,signal:AbortSignal):Promise<void>{
  return new Promise((resolve,reject)=>{
    if(signal.aborted){reject(reason(signal));return;}
    const abort=()=>{clearTimeout(timer);signal.removeEventListener('abort',abort);reject(reason(signal));};
    const timer=setTimeout(()=>{signal.removeEventListener('abort',abort);resolve();},milliseconds);
    signal.addEventListener('abort',abort,{once:true});
  });
}

export class PreviewQueue {
  private active=0;
  private waiters:Waiter[]=[];
  private pending=new Map<string,Entry>();
  constructor(private concurrency=2,private maxAttempts=5,private timeoutMs=45000,
              private sleep=previewDelay,private random=Math.random){
    if(concurrency<1||maxAttempts<1)throw new Error('预览队列配置无效');
  }
  private pump(){
    while(this.active<this.concurrency&&this.waiters.length)this.waiters.shift()!.start();
  }
  private acquire(signal:AbortSignal):Promise<()=>void>{
    return new Promise((resolve,reject)=>{
      if(signal.aborted){reject(reason(signal));return;}
      const waiter:Waiter={
        start:()=>{signal.removeEventListener('abort',waiter.cancel);this.active++;
          let released=false;resolve(()=>{if(!released){released=true;this.active--;this.pump();}});},
        cancel:()=>{this.waiters=this.waiters.filter(w=>w!==waiter);signal.removeEventListener('abort',waiter.cancel);reject(reason(signal));}
      };
      signal.addEventListener('abort',waiter.cancel,{once:true});this.waiters.push(waiter);this.pump();
    });
  }
  private async execute<T>(run:(signal:AbortSignal)=>Promise<T>,signal:AbortSignal):Promise<T>{
    const started=Date.now();
    for(let attempt=0;;attempt++){
      const release=await this.acquire(signal);
      let failure:Failure;
      try{const value=await run(signal);if(signal.aborted)throw reason(signal);return value;}catch(error){failure=error as Failure;}
      finally{release();}
      if(signal.aborted)throw reason(signal);
      const retry=[429,502,503,504].includes(failure.status||0)||failure instanceof TypeError;
      if(!retry||attempt+1>=this.maxAttempts)throw failure;
      const delay=Math.max((failure.retryAfter||0)*1000,250*2**attempt)+Math.floor(this.random()*150);
      if(Date.now()-started+delay>=this.timeoutMs)throw failure;
      // 退避不占用网络名额，其他图片可以继续读取。
      await this.sleep(delay,signal);
    }
  }
  load<T>(key:string,run:(signal:AbortSignal)=>Promise<T>,signal?:AbortSignal):Promise<T>{
    if(signal?.aborted)return Promise.reject(reason(signal));
    let entry=this.pending.get(key);
    if(!entry||entry.controller.signal.aborted){
      const controller=new AbortController();
      entry={controller,listeners:new Set(),promise:Promise.resolve()};
      const current=entry;
      const timer=setTimeout(()=>controller.abort(new DOMException('预览等待超时，请稍后刷新','TimeoutError')),this.timeoutMs);
      current.promise=this.execute(run,controller.signal).finally(()=>{
        clearTimeout(timer);if(this.pending.get(key)===current)this.pending.delete(key);
      });
      this.pending.set(key,current);
    }
    const current=entry,listener={};current.listeners.add(listener);
    return new Promise<T>((resolve,reject)=>{
      let settled=false;
      const clean=()=>{signal?.removeEventListener('abort',abort);current.listeners.delete(listener);};
      const abort=()=>{if(settled)return;settled=true;clean();reject(reason(signal!));
        if(!current.listeners.size)current.controller.abort();};
      signal?.addEventListener('abort',abort,{once:true});
      current.promise.then(value=>{if(!settled){settled=true;clean();resolve(value as T);}},
        error=>{if(!settled){settled=true;clean();reject(error);}});
    });
  }
}

export const previewQueue=new PreviewQueue();
