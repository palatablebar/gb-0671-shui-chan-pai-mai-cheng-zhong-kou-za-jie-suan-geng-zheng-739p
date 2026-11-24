import io, json, zipfile
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import ROOT
from app.engine import evaluate
from app.errors import BusinessError
from app import store


def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'freight.sqlite'))
    return TestClient(main.app)


def run_payload(client):
    return client.post('/api/runs', json={}).json()


def corrected_csv(base_text, ticket='00001', gross='135', version=3, replaces=2, paid='1285.35'):
    extra = (f'{ticket}v{version}gross,{ticket},gross,{gross},kg,黄鱼,12.30,0.05,{version},{replaces},{paid}\n'
             f'{ticket}v{version}tare,{ticket},tare,15,kg,黄鱼,12.30,0.05,{version},{replaces},0\n')
    return base_text.rstrip('\n') + '\n' + extra


def test_publish_creates_one_version_per_trip(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    run = run_payload(c)
    rid = run['manifest']['run_id']
    assert all(trip['sealable'] for trip in run['freight']['trips'])
    body = c.post(f'/api/runs/{rid}/publish', json={}).json()
    assert all(item['version'] == 1 and not item['idempotent'] for item in body['published'])
    assert {item['trip_no'] for item in body['published']} == {'T-20261117-01', 'T-20261117-02'}
    published = c.get(f'/api/runs/{rid}/publications').json()
    assert len(published['publications']) == 2
    assert published['adjustments'] == []


def test_republish_same_result_is_idempotent(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    rid = run_payload(c)['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/publish', json={}).json()
    second = c.post(f'/api/runs/{rid}/publish', json={}).json()
    assert all(item['idempotent'] for item in second['published'])
    assert [item['version'] for item in second['published']] == [item['version'] for item in first['published']]
    assert len(c.get(f'/api/runs/{rid}/publications').json()['publications']) == 2


def test_rerun_does_not_add_freight(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    rid = run_payload(c)['manifest']['run_id']
    c.post(f'/api/runs/{rid}/publish', json={})
    rerun = c.post(f'/api/runs/{rid}/rerun', json={})
    assert rerun.status_code == 200
    body = c.post(f'/api/runs/{rid}/publish', json={}).json()
    assert all(item['idempotent'] for item in body['published'])
    assert len(c.get(f'/api/runs/{rid}/publications').json()['publications']) == 2


def test_late_correction_creates_independent_adjustment(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    rid = run_payload(c)['manifest']['run_id']
    c.post(f'/api/runs/{rid}/publish', json={'trip_no': 'T-20261117-01'})
    base = (ROOT / 'sample_data/dirty.csv').read_text(encoding='utf-8-sig')
    late = c.post('/api/runs', json={'csv_text': corrected_csv(base)}).json()
    late_rid = late['manifest']['run_id']
    trip = late['freight']['trips'][0]
    assert trip['published_version'] == 1
    assert trip['pending_delta'] != '0.00'
    delta = {line['ticket']: line for line in trip['preview']}
    assert delta['00001']['amount_delta'] == '116.85'
    assert delta['00001']['freight_delta'] != '0.00'
    # 合计补退守恒：各票承运费补退之和为 0，承运单总额不变。
    from decimal import Decimal
    assert sum(Decimal(line['freight_delta']) for line in trip['preview']) == Decimal('0.00')

    published = c.post(f'/api/runs/{late_rid}/publish', json={'trip_no': 'T-20261117-01'}).json()
    new = published['published'][0]
    assert new['version'] == 2 and new['supersedes'] == 1
    old = c.get(f'/api/runs/{rid}/publications').json()['publications']
    assert {row['version'] for row in old if row['trip_no'] == 'T-20261117-01'} == {1, 2}
    records = c.get(f'/api/runs/{late_rid}/publications').json()['adjustments']
    assert records[0]['base_version'] == 1 and records[0]['new_version'] == 2
    assert records[0]['lines'][0]['ticket'] == '00001'


def test_original_payment_evidence_survives_adjustment(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    rid = run_payload(c)['manifest']['run_id']
    c.post(f'/api/runs/{rid}/publish', json={'trip_no': 'T-20261117-01'})
    v1 = store.latest_publication(main.DB_PATH, 'T-20261117-01')
    base = (ROOT / 'sample_data/dirty.csv').read_text(encoding='utf-8-sig')
    late_rid = c.post('/api/runs', json={'csv_text': corrected_csv(base)}).json()['manifest']['run_id']
    c.post(f'/api/runs/{late_rid}/publish', json={'trip_no': 'T-20261117-01'})
    versions = store.publications(main.DB_PATH, 'T-20261117-01')
    assert [row['version'] for row in versions] == [2, 1]
    assert versions[1]['payments'] == v1['payments']
    assert versions[1]['digest'] == v1['digest']


def test_mismatched_freight_cannot_seal(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    trips = [{'trip_no': 'T-BAD', 'waybill_amount': '100',
              'manifest': ['00001', '00002'],
              'ticket_freights': {'00001': '40', '00002': '40'}}]
    run = c.post('/api/runs', json={'dataset': 'clean', 'overrides': {'trips': trips}}).json()
    rid = run['manifest']['run_id']
    response = c.post(f'/api/runs/{rid}/publish', json={})
    assert response.status_code == 422
    assert response.json()['code'] == 'FREIGHT_NOT_SEALABLE'
    assert c.get(f'/api/runs/{rid}/publications').json()['publications'] == []


def test_unknown_ticket_cannot_be_booked(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    trips = [{'trip_no': 'T-MIX', 'waybill_amount': '100', 'manifest': ['00001', '00002', '88888']}]
    run = c.post('/api/runs', json={'dataset': 'clean', 'overrides': {'trips': trips}}).json()
    row = next(line for line in run['freight']['rows'] if line['ticket'] == '88888')
    assert row['book'] == '不入账'
    assert run['freight']['trips'][0]['balanced'] is False
    rid = run['manifest']['run_id']
    assert c.post(f'/api/runs/{rid}/publish', json={}).status_code == 422


def test_interrupted_publish_rolls_back(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    rid = run_payload(c)['manifest']['run_id']
    original = store.connect

    class Exploding:
        def __init__(self, con):
            self._con = con

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._con.close()
            return False

        def __getattr__(self, name):
            return getattr(self._con, name)

        def execute(self, sql, *args):
            if sql.lstrip().startswith('INSERT INTO adjustments'):
                raise RuntimeError('disk full')
            return self._con.execute(sql, *args)

    def fake_connect(path):
        return Exploding(original(path))

    monkeypatch.setattr(store, 'connect', fake_connect)
    # 第一次发布就会写 adjustments 吗？不会；先发布一次，再让后补更正触发调整写入。
    monkeypatch.setattr(store, 'connect', original)
    c.post(f'/api/runs/{rid}/publish', json={'trip_no': 'T-20261117-01'})
    before = store.publications(main.DB_PATH, 'T-20261117-01')
    base = (ROOT / 'sample_data/dirty.csv').read_text(encoding='utf-8-sig')
    late_rid = c.post('/api/runs', json={'csv_text': corrected_csv(base)}).json()['manifest']['run_id']

    monkeypatch.setattr(store, 'connect', fake_connect)
    with pytest.raises(RuntimeError):
        store.publish(main.DB_PATH, late_rid, 'T-20261117-01')
    monkeypatch.setattr(store, 'connect', original)
    after = store.publications(main.DB_PATH, 'T-20261117-01')
    assert after == before
    assert store.adjustments(main.DB_PATH, 'T-20261117-01') == []
    # 批次本身仍可读取，旧版本付款证据未被覆盖。
    assert store.attach(main.DB_PATH, store.get(main.DB_PATH, late_rid))['freight']['trips'][0]['published_version'] == 1


def test_publish_rejects_unknown_trip(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    rid = run_payload(c)['manifest']['run_id']
    assert c.post(f'/api/runs/{rid}/publish', json={'trip_no': 'T-404'}).status_code == 404


def test_export_contains_freight_evidence(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    rid = run_payload(c)['manifest']['run_id']
    c.post(f'/api/runs/{rid}/publish', json={})
    payload = c.get(f'/api/export/{rid}')
    assert payload.status_code == 200
    bundle = zipfile.ZipFile(io.BytesIO(payload.content))
    freight = json.loads(bundle.read('freight.json'))
    assert len(freight['trips']) == 2
    assert all(trip['seal'] for trip in freight['trips'])
    publications = json.loads(bundle.read('publications.json'))
    assert len(publications['publications']) == 2


def test_archive_blocks_when_freight_unbalanced():
    from app.modules.archive.rules import archive_decision
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': [
        {'trip_no': 'T-MIX', 'waybill_amount': '100', 'manifest': ['00001', '88888']}]}})
    assert archive_decision(state)['status'] == '待处理'
    assert archive_decision(state)['freight_balanced'] is False
