"""自动、会话内需求记忆：只记用户要求，不把历史回答或授权当作事实。

有界结构化折叠保存在已有AgentRun输出，无新增表/模型依赖。规则未覆盖的条件保留
原文，不声称已理解任意自然语言；原始消息仍在会话记录中。
"""
import json
import re
from sqlalchemy import select
from .models import AgentRun

CONCEPTS = {
    'male': ('男人', '男性', '男的', 'male', 'man'),
    'female': ('女人', '女性', '女的', 'female', 'woman'),
    'no_hat': ('不戴帽子', '不要戴帽子', '没戴帽子', '未戴帽子', '不带帽子', '没有帽子', '无帽', '不戴帽', 'without a hat', 'no hat'),
    'elderly': ('老人', '老年人', '老年', 'elderly', 'senior'),
    'child': ('儿童', '小孩', '孩子', 'child'),
    'hat': ('戴帽', '帽子', 'hat'),
    'short_sleeve': ('短袖', 'short sleeve'),
    'long_sleeve': ('长袖', 'long sleeve'),
    'short_pants': ('短裤', 'short pants'),
    'long_pants': ('长裤', 'long pants'),
}
LABELS = {'male':'男性','female':'女性','no_hat':'未戴帽子','elderly':'老人或老年人','child':'儿童','hat':'戴帽子','short_sleeve':'短袖',
          'long_sleeve':'长袖','short_pants':'短裤','long_pants':'长裤'}


def refine(previous, message, kind):
    followup = bool(re.search(r'^(那|再|还|只|不要|不需要|不要求|不限|去掉|改成|换成|继续|其中|这些|这个)|另外|而且', message.strip()))
    reset = any(word in message for word in ('重新找', '换个话题', '重新开始', '清空条件'))
    state = dict(previous) if previous and followup and not reset else {}
    concepts = list(state.get('concepts', []))
    unresolved = list(state.get('unresolved', []))
    if kind == 'catalog':
        cancel_hat = any(word in message for word in ('不要求帽子', '不要求戴帽', '不限帽子', '去掉帽子', '取消帽子', '帽子不限', '帽子不要求', '帽子没有要求'))
        no_hat = any(word in message.lower() for word in CONCEPTS['no_hat'])
        for key, aliases in CONCEPTS.items():
            if key in ('hat','no_hat') and cancel_hat:
                concepts = [c for c in concepts if c not in ('hat','no_hat')]
                unresolved = []
                continue
            if key=='hat' and no_hat:
                concepts = [c for c in concepts if c!='hat']
                continue
            if not any(re.search(r'(?<![a-z])'+re.escape(word)+r'(?![a-z])',message.lower()) if word.isascii() else word in message for word in aliases):
                continue
            if key == 'hat' and any(word in message for word in ('不要求帽子', '不要求戴帽', '不限帽子', '去掉帽子', '取消帽子', '帽子不限', '帽子不要求')):
                concepts = [c for c in concepts if c != key]
                unresolved = []
                continue
            if key != 'no_hat' and any(prefix+word in message.lower() for word in aliases for prefix in ('不要', '排除', '不需要', '不戴', '没有')):
                # 未支持的否定不伪装成已实现的精确筛选，保留原文供界面展示。
                concepts = [c for c in concepts if c != key]
                unresolved = [message[:200]]
                continue
            if key=='no_hat':
                concepts = [c for c in concepts if c!='hat']
                unresolved=[]
            elif key=='hat':
                concepts = [c for c in concepts if c!='no_hat']
            if key in ('male','female'):
                concepts = [c for c in concepts if c not in ('male','female')]
            if key in ('elderly','child') and any(w in message for w in ('换成','改成')):
                concepts = [c for c in concepts if c not in ('elderly','child')]
            if key in ('short_sleeve','long_sleeve') and any(w in message for w in ('换成','改成')):
                concepts = [c for c in concepts if c not in ('short_sleeve','long_sleeve')]
            if key in ('short_pants','long_pants') and any(w in message for w in ('换成','改成')):
                concepts = [c for c in concepts if c not in ('short_pants','long_pants')]
            if key not in concepts:
                concepts.append(key)
    modality = state.get('modality')
    excluded = list(state.get('excluded_modalities', []))
    for value, words in {'image':('图片','图像'),'text':('文本','语料'),'video':('视频',),'audio':('音频','语音'),'tabular':('表格',)}.items():
        if any(p+w in message for w in words for p in ('不要','排除','不是')):
            if value not in excluded:excluded.append(value)
            if modality == value:modality=None
        elif any(w in message for w in words):
            modality = value
            excluded = [v for v in excluded if v!=value]
    clauses = [*state.get('recent_requests', []), message[:120]][-4:]
    pending = list(state.get('additional_requirements', []))
    generic = bool(re.fullmatch(r'(继续|继续找|再找找|再找|这些|其中)[。！!？?]*',message.strip()))
    recognized = any(alias in message.lower() for aliases in CONCEPTS.values() for alias in aliases) or any(
        word in message for word in ('图片','图像','文本','语料','视频','音频','语音','表格'))
    overflow = bool(state.get('needs_history_clarification', False)) or len(message)>500
    if state and followup and not generic and not recognized and message[:100] not in pending:
        if len(pending) >= 4:overflow=True
        else:pending.append(message[:100])
    result = {'version':2,'kind':kind,'base_query':state.get('base_query', message[:350]),
        'concepts':concepts[:12], 'modality':modality,'excluded_modalities':excluded,'recent_requests':clauses,'unresolved':unresolved,
        'turns_folded':int(state.get('turns_folded',0))+1,'automatic':True,
        'policy':'user requirements only; permissions and business facts must be queried again'}
    result.update(additional_requirements=pending,needs_history_clarification=overflow)
    result['candidate_versions'] = list(state.get('candidate_versions', []))[:8]
    # 删除条件之后不用旧base_query再次触发已取消的语义条件。
    if kind == 'catalog' and result['concepts']:
        effective = '寻找'+('、'.join(LABELS[c] for c in result['concepts']))+'相关数据集'
        if modality:effective += '，数据模态为'+modality
    elif kind=='catalog' and state.get('concepts') and not concepts:
        effective = '寻找数据集'
    else:
        effective = '；'.join([result['base_query'], *clauses[-2:]])[:500]
    result['effective_query'] = effective[:500]
    if pending:result['effective_query']=(effective+'；'+'；'.join(pending))[:500]
    assert len(json.dumps(result,ensure_ascii=False).encode()) < 8192
    return result


def prepare(db, user, conversation_id, message, kind, exclude_run_id):
    previous = None
    if conversation_id:
        rows = db.scalars(select(AgentRun).where(AgentRun.conversation_id==conversation_id,
            AgentRun.user_id==user.id,AgentRun.id!=exclude_run_id,
            AgentRun.tool==('knowledge' if kind=='knowledge' else 'semantic_search'),
            AgentRun.status.in_(['completed','degraded'])).order_by(AgentRun.created_at.desc(),AgentRun.id.desc()).limit(64)).all()
        if rows:
            latest = rows[0]
            previous = latest.outputs.get('query_memory')
            if previous and previous.get('version',1)<2:
                for old_request in previous.get('unresolved',[]):
                    previous=refine(previous,'还要'+old_request,kind)
            if not previous:
                for row in reversed(rows):
                    previous = refine(previous, str(row.inputs.get('message',''))[:500], kind)
    return refine(previous, message, kind)


def evidence_matches(text, concepts):
    """只承认元数据中明确提及的需求；向量接近不证明包含所需人群/服饰。"""
    text = text.lower()
    for key in concepts:
        aliases = CONCEPTS.get(key, ())
        if key=='hat' and any(word in text for word in CONCEPTS['no_hat']):
            return False
        if key=='no_hat':
            if not any(word in text for word in aliases):return False
            continue
        if not any((bool(re.search(r'(?<![a-z])'+re.escape(word)+r'(?![a-z])',text)) if word.isascii() else word in text) and not any(prefix+word in text for prefix in ('不包含','没有','不含','排除','未','不')) for word in aliases):
            return False
    return True
