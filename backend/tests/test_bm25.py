from types import SimpleNamespace
from app.bm25 import rank, tokenize, field_scores
from test_retrieval import retrieval, doc, drain


def test_chinese_english_and_identifier_tokens_are_consistent():
    assert '老人' in tokenize('老人图像')
    assert tokenize('COCO gender_id')==['coco','gender_id']
    assert tokenize('ＡＢＣ')==['abc']


def test_bm25_length_normalization_and_title_boost():
    scores=field_scores('license',['license','license '+'common '*100])
    assert scores[0]>scores[1]>0
    rows=[SimpleNamespace(id='title',title='license',body=''),SimpleNamespace(id='body',title='',body='license')]
    assert rank('license',rows)[0][0]=='title'
    assert rank('absent',rows)==[]


def test_term_frequency_saturates_and_unknown_terms_score_zero():
    scores=field_scores('rare',['rare','rare rare','rare '*100],b=0)
    assert scores[0]<scores[1]<scores[2]<3*scores[0]
    assert field_scores('unknown',['known'])==[0]


def test_authorized_bm25_and_vector_outage_fallback(retrieval,client,actors):
    doc(client,actors['owner'],text='独立复核由另一账号完成。')
    drain(retrieval)
    retrieval.state.retrieval_encoder.fail=True
    response=client.post('/api/v1/retrieval/knowledge/ask',headers=actors['owner']['headers'],json={'query':'独立复核'})
    assert response.status_code==200 and response.json()['knowledge_citations']
    assert response.json()['retrieval_meta']['lexical_method']=='bm25'
    hidden=client.post('/api/v1/retrieval/knowledge/ask',headers=actors['stranger']['headers'],json={'query':'独立复核'})
    assert hidden.status_code==200 and not hidden.json()['knowledge_citations']
