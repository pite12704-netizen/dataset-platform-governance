"""授权语料内的字段加权BM25；不把SQL候选子集冒充完整语料统计。

中文使用字符二元词，英文/标识符按词；查询与文档采用相同分词版本。
CPU实现有显式语料预算，超出要求缩小范围；无外部推理或额外搜索服务。
"""
import math
import re
import unicodedata
from collections import Counter

TOKENIZER_VERSION = 'nfkc-cjk-bigram-latin-v1'


def tokenize(text):
    text = unicodedata.normalize('NFKC', text).lower()
    tokens=[]
    for word in re.findall(r'[a-z0-9]+(?:[_-][a-z0-9]+)*|[\u4e00-\u9fff]+', text):
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            tokens.extend(word[i:i+2] for i in range(len(word)-1))
            if len(word)==1:tokens.append(word)
        else:tokens.append(word)
    return tokens


def field_scores(query, texts, k1=1.2, b=.75):
    terms=set(tokenize(query))
    if not terms or not texts:return [0.0]*len(texts)
    counts=[Counter(tokenize(text)) for text in texts]
    lengths=[sum(c.values()) for c in counts]
    average=sum(lengths)/len(lengths) or 1
    frequencies={term:sum(term in c for c in counts) for term in terms}
    scores=[];n=len(counts)
    for count,length in zip(counts,lengths):
        score=0.0
        for term in terms:
            tf=count.get(term,0)
            if not tf:continue
            df=frequencies[term]
            idf=math.log1p((n-df+.5)/(df+.5))
            score+=idf*tf*(k1+1)/(tf+k1*(1-b+b*length/average))
        scores.append(score)
    return scores


def rank(query, rows, limit=120):
    """每字段独立BM25后加权：标题3，正文1。返回稳定ID与分值，非概率。"""
    titles=field_scores(query,[r.title for r in rows])
    bodies=field_scores(query,[r.body for r in rows])
    scored=[(r.id,3*title+body) for r,title,body in zip(rows,titles,bodies) if title>0 or body>0]
    return sorted(scored,key=lambda item:(-item[1],item[0]))[:limit]
