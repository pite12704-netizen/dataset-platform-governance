/** 串行任务查询：同一时刻只有一个请求，故障退避且不触发清空成功数据。 */
import {ApiError} from './api';

type Options<T>={load:(signal:AbortSignal)=>Promise<T>;onValue:(value:T)=>void;onError:(error:unknown,delay:number)=>void;
  onBusy?:(busy:boolean)=>void;shouldPoll:(value:T)=>boolean;interval?:number;timeout?:number};

export function taskPolling<T>(options:Options<T>){
  let timer:ReturnType<typeof setTimeout>|undefined,controller:AbortController|undefined;
  let inFlight:Promise<void>|undefined,stopped=false,failures=0,nextAllowed=0;
  const interval=options.interval??4000;
  const schedule=(delay:number)=>{clearTimeout(timer);if(!stopped)timer=setTimeout(()=>{void refresh();},delay);};
  const refresh=():Promise<void>=>{
    if(stopped)return Promise.resolve();
    if(inFlight)return inFlight;
    if(Date.now()<nextAllowed){schedule(nextAllowed-Date.now());return Promise.resolve();}
    clearTimeout(timer);controller=new AbortController();const current=controller;
    options.onBusy?.(true);
    const deadline=setTimeout(()=>current.abort(),options.timeout??15000);
    inFlight=Promise.resolve().then(async()=>{
      let delay:number|undefined;
      try{
        const value=await options.load(current.signal);
        if(stopped)return;
        failures=0;nextAllowed=0;options.onValue(value);
        if(options.shouldPoll(value))delay=interval;
      }catch(error){
        if(stopped)return;
        failures++;
        const retryable=!(error instanceof ApiError)||![401,403].includes(error.status);
        delay=retryable?Math.min(60000,interval*2**Math.min(failures-1,4)):undefined;
        if(error instanceof ApiError&&[429,503].includes(error.status)&&delay!==undefined)
          delay=Math.max(delay,error.retryAfter*1000);
        nextAllowed=delay===undefined?0:Date.now()+delay;
        options.onError(error,delay??0);
      }finally{
        clearTimeout(deadline);inFlight=undefined;if(!stopped)options.onBusy?.(false);
        if(delay!==undefined)schedule(delay);
      }
    });
    return inFlight;
  };
  return {refresh,stop:()=>{stopped=true;clearTimeout(timer);controller?.abort();}};
}
