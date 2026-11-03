"""Private persistent previews. Files are derivatives, never public storage URLs."""
import hashlib
import io
from sqlalchemy import select,update
from .common import require
from .models import Thumbnail,Space

PROFILES={'t640x480q75v1':((640,480),75),'t1280x1280q85v1':((1280,1280),85)}


def build(asset,storage,token,heartbeat):
    from .image_reviews import read_image
    image=read_image(asset,storage);rows=[]
    try:
        for profile,(size,quality) in PROFILES.items():
            heartbeat(True)
            copy=image.copy()
            try:copy.thumbnail(size);out=io.BytesIO();copy.save(out,'JPEG',quality=quality)
            finally:copy.close()
            raw=out.getvalue();require(len(raw)<=2*1024**2,'缩略图超过缓存容量',413)
            key=f'thumbnails/{asset.id}/{token}/{profile}'
            storage.put_bytes(key,raw)
            rows.append({'asset_id':asset.id,'profile':profile,'source_sha256':asset.sha256,
                'object_key':key,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    finally:image.close()
    return rows


def commit(db,job,rows,settings):
    old=db.scalars(select(Thumbnail).where(Thumbnail.asset_id==rows[0]['asset_id']).with_for_update()).all()
    difference=sum(r['size'] for r in rows)-sum(r.size for r in old)
    from .capacity import growth
    growth(db,difference,settings)
    changed=db.execute(update(Space).where(Space.id==job.space_id,
        Space.used_bytes+Space.reserved_bytes+difference<=Space.quota_bytes).values(used_bytes=Space.used_bytes+difference))
    require(changed.rowcount==1,'缩略图存储配额不足，预览仍可按需生成',409)
    previous=[r.object_key for r in old]
    for row in old:db.delete(row)
    db.flush()
    db.add_all([Thumbnail(**row) for row in rows])
    return previous


def read_persistent(request,asset,size,quality):
    profile=next((key for key,(dimensions,q) in PROFILES.items() if size==dimensions and quality==q),None)
    if not profile:return None
    with request.app.state.db.Session() as db:
        row=db.scalar(select(Thumbnail).where(Thumbnail.asset_id==asset.id,Thumbnail.profile==profile,
            Thumbnail.source_sha256==asset.sha256))
        info=(row.object_key,row.sha256,row.size) if row else None
    if info:
        try:
            raw=b''.join(request.app.state.storage.chunks(info[0],0,info[2]-1))
            if len(raw)==info[2] and hashlib.sha256(raw).hexdigest()==info[1]:return raw
        except Exception:
            # Missing/corrupt derivative never marks an original safe or changes authorization.
            return None
    return None


def delete_for_asset(db,asset,space_id,storage):
    rows=db.scalars(select(Thumbnail).where(Thumbnail.asset_id==asset.id).with_for_update()).all()
    for row in rows:storage.delete(row.object_key);db.delete(row)
    if rows:db.execute(update(Space).where(Space.id==space_id).values(used_bytes=Space.used_bytes-sum(r.size for r in rows)))
