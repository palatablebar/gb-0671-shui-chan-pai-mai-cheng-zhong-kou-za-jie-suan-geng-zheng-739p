import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    freight = result.get('freight') or {}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核'),'freight_seal':freight.get('seal','')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
        # 承运费逐票证据与已发布版本：原付款证据随封存保留，后补更正只追加调整版本。
        archive.writestr('freight.json',json.dumps({
            'trips':[{key:trip.get(key) for key in ('trip_no','carrier','driver','departure_at','currency',
                    'waybill_amount','allocated','unallocated','balanced','sealable','status',
                    'settled_count','pending_count','seal','errors','ledger')}
                     for trip in freight.get('trips',[])],
            'unassigned':freight.get('unassigned',[]),
            'seal':freight.get('seal',''),
        },ensure_ascii=False,indent=2))
        if freight.get('publications') is not None:
            archive.writestr('publications.json',json.dumps({
                'publications':freight.get('publications',[]),
                'adjustments':freight.get('adjustments',[]),
            },ensure_ascii=False,indent=2))
    return buffer.getvalue()
