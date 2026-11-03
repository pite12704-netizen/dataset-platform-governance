"""从已有扫描记录恢复整图待办，不推断人工通过，不读取任意路径。"""
def coverage(rows, provenance, known_assets=None):
    result={}
    for row in [*rows,*provenance.get('scans',[])]:
        asset_id=row.get('asset_id')
        width,height=row.get('width'),row.get('height')
        if (isinstance(asset_id,str) and isinstance(width,int) and isinstance(height,int)
                and width>0 and height>0 and (known_assets is None or asset_id in known_assets)):
            result[asset_id]={'width':width,'height':height}
    for row in provenance.get('exact_duplicates',[]):
        identifier,representative=row.get('asset_id'),row.get('representative_asset_id')
        if representative in result and (known_assets is None or identifier in known_assets):
            result[identifier]=result[representative].copy()
    return result


def restored_pages(tables):
    """备份 v3 升级与数据库迁移使用相同的确定性 ID 和待办规则。"""
    from uuid import uuid5,NAMESPACE_URL
    assets={a['id']:a for a in tables.get('assets',[])}
    pages=[]
    for batch in tables.get('image_review_batches',[]):
        rows=[i for i in tables.get('image_review_items',[]) if i['batch_id']==batch['id']]
        known={a['id'] for a in assets.values() if a['version_id']==batch['version_id']}
        for identifier,dimensions in coverage(rows,batch['provenance'],known).items():
            pages.append({**dimensions,'id':uuid5(NAMESPACE_URL,'dp-review:'+batch['id']+':'+identifier).hex,
                'batch_id':batch['id'],'asset_id':identifier,'status':'pending','revision':0,'snapshot':{},
                'editor_id':None,'reviewer_id':None,'claimed_by_id':None,'claim_token':None,
                'claim_expires_at':None,'created_at':batch['created_at']})
    return pages
