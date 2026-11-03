/** 任务页仅轮询摘要，不在每次刷新时请求完整任务文件清单。 */
import {useCallback,useEffect,useRef,useState} from 'react';
import {api,Row} from '../api';
import {taskPolling} from '../task-polling';

export function useJobRows(page:number){
  const [rows,setRows]=useState<Row[]>([]),[loading,setLoading]=useState(true),[refreshing,setRefreshing]=useState(false);
  const [error,setError]=useState(''),[hasMore,setHasMore]=useState(false),[updated,setUpdated]=useState<number>();
  const poll=useRef<ReturnType<typeof taskPolling<Row>>|undefined>(undefined);
  useEffect(()=>{
    setRows([]);setLoading(true);setError('');setHasMore(false);setUpdated(undefined);
    const current=taskPolling<Row>({
      load:signal=>api(`/jobs?compact=true&limit=50&offset=${page*50}`,{signal}),
      onValue:value=>{setRows(value.items);setHasMore(value.has_more);setError('');setUpdated(Date.now());},
      onError:(err,delay)=>{setError(`${err instanceof Error?err.message:'任务查询失败'}。${delay?'已保留上次列表，稍后自动重试。':'请检查登录或访问权限后重试。'}`);},
      onBusy:value=>{setRefreshing(value);if(!value)setLoading(false);},
      shouldPoll:value=>value.items.some((row:Row)=>['queued','running'].includes(row.status))});
    poll.current=current;void current.refresh();
    return()=>{current.stop();if(poll.current===current)poll.current=undefined;};
  },[page]);
  const refresh=useCallback(()=>poll.current?.refresh(),[]);
  return {rows,loading,refreshing,error,hasMore,refresh,updated};
}
