/** 旧版静态页面的兼容修复：仅拦截本站预览 GET，不改变其他请求和身份。 */
import {PreviewQueue} from './preview-queue';
import {retryAfterSeconds} from './api';

export function installPreviewBackpressure(target:Pick<Window,'fetch'|'location'>,queue=new PreviewQueue()){
  const original=target.fetch.bind(target);let serial=0;
  const paths=[/^\/api\/v1\/assets\/[^/]+\/preview$/,
    /^\/api\/v1\/image-reviews\/[^/]+\/assets\/[^/]+\/preview$/,
    /^\/api\/v1\/governance\/reports\/[^/]+\/assets\/[^/]+\/preview$/];
  target.fetch=async(input:RequestInfo|URL,init?:RequestInit)=>{
    const request=input instanceof Request?input:undefined;
    const url=new URL(request?.url||String(input),target.location.href);
    if(url.origin!==target.location.origin||(init?.method||request?.method||'GET').toUpperCase()!=='GET'||!paths.some(p=>p.test(url.pathname)))
      return original(input,init);
    const signal=init?.signal||request?.signal;
    try{
      const response=await queue.load('preview:'+ ++serial,async abort=>{
        const raw=await original(input,{...init,signal:abort});
        const body=await raw.arrayBuffer();
        if(body.byteLength>4*1024**2)throw new Error('图像预览超过大小限制');
        const headers=new Headers(raw.headers);headers.delete('content-encoding');headers.set('content-length',String(body.byteLength));
        const buffered=new Response(body,{status:raw.status,statusText:raw.statusText,headers});
        if([429,502,503,504].includes(raw.status))throw Object.assign(new Error('预览暂时繁忙'),
          {status:raw.status,retryAfter:retryAfterSeconds(raw.headers.get('Retry-After')),response:buffered});
        return buffered;
      },signal||undefined);
      return response;
    }catch(error){
      // 保留服务器最后一次响应，旧 API 层继续按原协议显示错误。
      if((error as {response?:Response}).response)return (error as {response:Response}).response;
      throw error;
    }
  };
}

if(typeof window!=='undefined')installPreviewBackpressure(window);
