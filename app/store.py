import sqlite3, json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from app.errors import BusinessError


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("""CREATE TABLE IF NOT EXISTS publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trip_no TEXT NOT NULL,
        version INTEGER NOT NULL,
        run_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT '已发布',
        supersedes INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE(trip_no, version),
        UNIQUE(trip_no, run_id, digest)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trip_no TEXT NOT NULL,
        base_publication INTEGER NOT NULL,
        publication INTEGER NOT NULL,
        run_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(base_publication, digest)
    )""")
    return con


def save(path, result):
    run_id = result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)',
                    (run_id, result['manifest']['sha256'], json.dumps(result, ensure_ascii=False)))
    return get(path, run_id)


def get(path, run_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    if not row:
        raise BusinessError('NOT_FOUND', '批次不存在', 404)
    result = json.loads(row['result'])
    result.update(revision=row['revision'], status=row['status'])
    return result


def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]


def review(path, run_id, revision):
    with connect(path) as con:
        cur = con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',
                          ('已复核', run_id, revision))
        if cur.rowcount != 1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
                raise BusinessError('NOT_FOUND', '批次不存在', 404)
            raise BusinessError('STALE_VERSION', '复核版本已更新，请刷新', 409)
    return get(path, run_id)


def _publication_row(row):
    return {'id': row['id'], 'trip_no': row['trip_no'], 'version': row['version'],
            'run_id': row['run_id'], 'digest': row['digest'],
            'status': row['status'], 'supersedes': row['supersedes'],
            'created_at': row['created_at'], **json.loads(row['payload'])}


def publications(path, trip_no=None):
    with connect(path) as con:
        if trip_no:
            rows = con.execute('SELECT * FROM publications WHERE trip_no=? ORDER BY version DESC',
                               (trip_no,)).fetchall()
        else:
            rows = con.execute('SELECT * FROM publications ORDER BY trip_no, version DESC').fetchall()
    return [_publication_row(row) for row in rows]


def latest_publication(path, trip_no):
    with connect(path) as con:
        row = con.execute('SELECT * FROM publications WHERE trip_no=? ORDER BY version DESC LIMIT 1',
                          (trip_no,)).fetchone()
    return _publication_row(row) if row else None


def adjustments(path, trip_no=None):
    with connect(path) as con:
        if trip_no:
            rows = con.execute('SELECT * FROM adjustments WHERE trip_no=? ORDER BY id DESC',
                               (trip_no,)).fetchall()
        else:
            rows = con.execute('SELECT * FROM adjustments ORDER BY id DESC').fetchall()
    return [{'id': row['id'], 'trip_no': row['trip_no'], 'base_publication': row['base_publication'],
             'publication': row['publication'], 'run_id': row['run_id'], 'digest': row['digest'],
             'created_at': row['created_at'], **json.loads(row['payload'])} for row in rows]


def publish(path, run_id, trip_no=None):
    """封存车次承运费结果。

    - 全部写入在一个事务内完成；任何异常回滚，旧账、旧版本和原付款证据不变。
    - 同一车次同一结果幂等：返回原发布，不新增承运费或版本。
    - 已发布后遇到后补更正：旧版本保留，新结果以独立版本 + 调整记录发布。
    """
    from app.modules.freight.rules import preview_adjustment, publish_payload, result_digest

    result = get(path, run_id)
    freight = result.get('freight') or {}
    trips = freight.get('trips') or []
    if not trips:
        raise BusinessError('NO_TRIPS', '该批次没有承运单车次')
    targets = [trip for trip in trips if trip_no is None or trip['trip_no'] == trip_no]
    if trip_no is not None and not targets:
        raise BusinessError('TRIP_NOT_FOUND', '车次不存在', 404)
    blocked = [trip for trip in targets if not trip.get('balanced') or trip.get('errors')]
    if blocked:
        names = '、'.join(trip['trip_no'] for trip in blocked)
        raise BusinessError('FREIGHT_NOT_SEALABLE', f'车次 {names} 承运费合计与承运单不一致或存在阻断项，不得封存')

    published = []
    with connect(path) as con:
        try:
            con.execute('BEGIN IMMEDIATE')
            for trip in targets:
                payload = publish_payload(trip)
                digest = result_digest(payload)
                base = con.execute('SELECT * FROM publications WHERE trip_no=? ORDER BY version DESC LIMIT 1',
                                   (trip['trip_no'],)).fetchone()
                if base is not None and base['digest'] == digest:
                    published.append({**_publication_row(base), 'idempotent': True})
                    continue
                version = 1 if base is None else base['version'] + 1
                if base is not None:
                    lines = preview_adjustment(json.loads(base['payload']), trip)
                    payload = {**payload, 'supersedes': base['version'], 'adjustment': lines}
                now = datetime.now(timezone.utc).isoformat()
                cur = con.execute(
                    'INSERT INTO publications(trip_no,version,run_id,digest,payload,status,supersedes,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (trip['trip_no'], version, run_id, digest,
                     json.dumps(payload, ensure_ascii=False), '已发布',
                     base['id'] if base is not None else None, now))
                publication_id = cur.lastrowid
                if base is not None:
                    record = {'base_version': base['version'],
                              'base_seal': json.loads(base['payload']).get('seal', ''),
                              'base_publication': base['id'],
                              'new_version': version,
                              'run_id': run_id,
                              'lines': payload['adjustment']}
                    con.execute(
                        'INSERT INTO adjustments(trip_no,base_publication,publication,run_id,digest,payload,created_at) VALUES(?,?,?,?,?,?,?)',
                        (trip['trip_no'], base['id'], publication_id, run_id, digest,
                         json.dumps(record, ensure_ascii=False), now))
                row = con.execute('SELECT * FROM publications WHERE id=?', (publication_id,)).fetchone()
                published.append({**_publication_row(row), 'idempotent': False})
            con.commit()
        except Exception:
            con.rollback()
            raise
    return {'run_id': run_id, 'published': published}


def attach(path, result):
    """只读附加：本批车次相对已发布结果的补退预览与发布历史。"""
    freight = result.get('freight')
    if not freight:
        return result
    from app.modules.freight.rules import preview_adjustment

    zero = Decimal('0.00')
    for trip in freight.get('trips', []):
        base = latest_publication(path, trip['trip_no'])
        trip['published'] = base
        trip['published_version'] = base['version'] if base else 0
        trip['preview'] = preview_adjustment(base, trip) if base else []
        trip['pending_delta'] = format(
            sum((Decimal(line['net_delta']) for line in trip['preview']), zero).quantize(zero), 'f')
    freight['publications'] = publications(path)
    freight['adjustments'] = adjustments(path)
    return result
