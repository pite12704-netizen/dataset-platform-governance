/** SSE resumes authoritative state snapshots; REST recovers after transport failures. */
import {api,ApiError,Row} from './api';
export function watchAction(actionId:string,onSnapshot:(value:Row|undefined)=>void,onState:(state:string)=>void){
  let active=true,source:EventSource|undefined,timer:ReturnType<typeof setTimeout>|undefined,inflight=false;
  const controller=new AbortController();
  const stop=()=>{active=false;source?.close();clearTimeout(timer);controller.abort();};
  const accept=(value:Row)=>{
    if(!active||value.action_id!==actionId||typeof value.progress!=='number')return;
    onSnapshot(value);onState('connected');
    if(['completed','failed','cancelled'].includes(value.status)){onState('finished');stop();}
  };
  const lost=()=>{onSnapshot(undefined);onState('forbidden');stop();};
  const check=async()=>{
    if(!active||inflight)return;inflight=true;let delay=5000;
    try{accept(await api(`/agent/actions/${actionId}/status`,{signal:controller.signal}));}
    catch(e){if(!active)return;if(e instanceof ApiError&&[401,403,404].includes(e.status)){lost();return;}
      if(e instanceof ApiError&&e.status===429)delay=Math.max(5000,e.retryAfter*1000);onState('reconnecting');}
    finally{inflight=false;if(active&&(!source||source.readyState===2))timer=setTimeout(check,delay);}
  };
  onState('connecting');
  if(typeof EventSource==='undefined'){void check();return stop;}
  source=new EventSource(`/api/v1/agent/actions/${actionId}/events`);
  source.addEventListener('snapshot',event=>{try{accept(JSON.parse((event as MessageEvent).data));}catch{onState('reconnecting');}});
  source.addEventListener('access_lost',lost);
  source.addEventListener('temporary_error',()=>onState('reconnecting'));
  source.onerror=()=>{if(active){onState('reconnecting');void check();}};
  return stop;
}
