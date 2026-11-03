"""Additional training adapters; human labels remain the only target answers."""
import hashlib
import json
import zipfile
from .common import require


def write(archive,name,raw,checksums):
    if isinstance(raw,str):raw=raw.encode('utf-8')
    info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_STORED
    info.create_system=3;info.external_attr=0o600<<16
    archive.writestr(info,raw);checksums.append({'path':name,'sha256':hashlib.sha256(raw).hexdigest()})


def validate(contract,format):
    if format=='yolo':
        require(contract['split'],'YOLO 交付需要已独立批准的数据划分',409)
        splits={contract['split']['mapping'][identifier] for identifier in contract['selected_asset_ids']}
        require({'train','val'}<=splits,'当前筛选缺少训练或验证样本，不能生成虚假划分',409)


def extra_files(archive,payload,format,checksums):
    by_image={}
    for annotation in payload['annotations']:by_image.setdefault(annotation['image_id'],[]).append(annotation)
    partitions={}
    for image in payload['images']:
        partitions.setdefault(image['split'],[]).append(image['file_name'])
        if format=='yolo':
            lines=[]
            for item in by_image.get(image['id'],[]):
                x,y,w,h=item['bbox'];width,height=image['width'],image['height']
                right=min(width,x+w);bottom=min(height,y+h)
                normalized=((x+right)/2/width,(y+bottom)/2/height,(right-x)/width,(bottom-y)/height)
                require(min(normalized[2:])>1e-10 and min(normalized)>=0 and max(normalized)<=1,'审核框无法安全归一化，请复核后再导出 YOLO',422)
                lines.append(str(item['category_id']-1)+' '+' '.join(f'{value:.10f}' for value in normalized))
            write(archive,'labels/'+image['source_asset_id']+'.txt','\n'.join(lines)+('\n' if lines else ''),checksums)
    if format=='yolo':
        for split,paths in partitions.items():write(archive,f'{split}.txt','\n'.join('./'+path for path in paths)+'\n',checksums)
        yaml='train: train.txt\nval: val.txt\n'
        if partitions.get('test'):yaml+='test: test.txt\n'
        names='names:\n'+''.join(f"  {c['id']-1}: {c['name']}\n" for c in payload['categories'])
        write(archive,'dataset.yaml',yaml+names,checksums)
    if format=='vlm-jsonl':
        lines={}
        for image in payload['images']:
            answer={'persons':[{'bbox':item['bbox'],'labels':item['attributes']} for item in by_image.get(image['id'],[])]}
            if payload.get('review_template'):
                categories={c['id']:c['name'] for c in payload['categories']}
                answer={'objects':[{'bbox':item['bbox'],'category':categories[item['category_id']],'labels':item['attributes']} for item in by_image.get(image['id'],[])]}
            row={'messages':[{'role':'user','content':'<image>请定位图中所有真实人物，并以原图像素 xywh 坐标输出 bbox、帽子、袖长与上衣颜色标签；不推断身份、性别或年龄。'},
                {'role':'assistant','content':json.dumps(answer,ensure_ascii=False,sort_keys=True)}],'images':[image['file_name']]}
            if payload.get('review_template'):row['messages'][0]['content']='<image>按以下审核模板定位目标，输出原图像素 xywh 框、category 与 labels；未知属性为 null。模板：'+json.dumps(payload['review_template'],ensure_ascii=False,sort_keys=True)
            split='all' if image['split']=='unpartitioned' else image['split']
            lines.setdefault(split,[]).append(json.dumps(row,ensure_ascii=False,sort_keys=True))
        definitions={}
        for split,rows in lines.items():
            write(archive,split+'.jsonl','\n'.join(rows)+'\n',checksums)
            definitions['dataset_platform_'+split]={'file_name':split+'.jsonl','formatting':'sharegpt',
                'columns':{'messages':'messages','images':'images'},
                'tags':{'role_tag':'role','content_tag':'content','user_tag':'user','assistant_tag':'assistant'}}
        write(archive,'dataset_info.json',json.dumps(definitions,ensure_ascii=False,sort_keys=True),checksums)
