from decimal import Decimal
import pytest

from app.engine import evaluate
from app.modules.freight.rules import allocate
from app.errors import BusinessError


def trip(state, trip_no):
    return next(t for t in state['freight']['trips'] if t['trip_no'] == trip_no)


def test_trip_allocation_balances_waybill(sample):
    for row in sample['freight']['trips']:
        assert row['balanced'] is True
        assert row['allocated'] == row['waybill_amount']
        assert Decimal(row['unallocated']) == 0


def test_every_ticket_has_weight_source(sample):
    for row in sample['freight']['rows']:
        assert row['weight_source'] in {'生效', '暂定', '申报'}
        if row['weight_source'] == '生效':
            assert row['billable_kg']


def test_closed_ticket_does_not_carry_unclosed_share(sample):
    # 车次 02 有一张后补待定票，但它的份额是单独预留的，已结票只承担自己的重量占比。
    row = trip(sample, 'T-20261117-02')
    settled = [line for line in row['ledger'] if line['status'] == '已结']
    pending = [line for line in row['ledger'] if line['status'] == '待定']
    assert len(pending) == 1
    total = sum(Decimal(line['billable_kg']) for line in row['ledger'])
    expected = (Decimal(row['waybill_amount']) * Decimal(pending[0]['billable_kg']) / total).quantize(Decimal('.01'))
    assert abs(Decimal(pending[0]['freight']) - expected) <= Decimal('0.01')
    assert sum(Decimal(line['freight']) for line in row['ledger']) == Decimal(row['waybill_amount'])


def test_allocate_remainder_uses_exact_cents():
    result = allocate(Decimal('100.00'), [('a', Decimal('1')), ('b', Decimal('1')), ('c', Decimal('1'))])
    assert sum(result.values()) == Decimal('100.00')
    assert set(result.values()) == {Decimal('33.33'), Decimal('33.34')}


def test_allocate_zero_weight():
    assert allocate(Decimal('50'), []) == {}


def test_unknown_ticket_not_booked():
    # 车次清单里出现结算数据中没有的票号：不能入账，且该车不得封存。
    raw = {'trip_no': 'T-X', 'waybill_amount': '100',
           'manifest': ['00001', '99999']}
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': [raw]}})
    row = trip(state, 'T-X')
    pending = [line for line in row['ledger'] if line['ticket'] == '99999'][0]
    assert pending['status'] == '未入账'
    assert pending['book'] == '不入账'
    assert row['balanced'] is False
    assert row['sealable'] is False
    assert any(error['code'] == 'UNALLOCATED_FREIGHT' for error in row['errors'])


def test_ticket_outside_manifest_is_unassigned(sample):
    # clean 的 00001..00020 中 00001-00020 全在清单内；换成只含 00001 的车次，
    # 其余生效票必须标为未入账而不是被摊到 00001 上。
    raw = {'trip_no': 'T-ONE', 'waybill_amount': '100', 'manifest': ['00001']}
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': [raw]}})
    row = trip(state, 'T-ONE')
    assert Decimal(row['allocated']) == Decimal('100.00')
    unassigned = {item['ticket'] for item in state['freight']['unassigned']}
    assert '00002' in unassigned
    assert '00001' not in unassigned


def test_ticket_freight_mismatch_blocks_seal():
    raw = {'trip_no': 'T-SPLIT', 'waybill_amount': '100',
           'manifest': ['00001', '00002'],
           'ticket_freights': {'00001': '60', '00002': '30'}}
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': [raw]}})
    row = trip(state, 'T-SPLIT')
    assert row['balanced'] is False
    assert any(error['code'] == 'FREIGHT_NOT_BALANCED' for error in row['errors'])
    assert row['status'] == '待处理'


def test_ticket_freight_must_cover_manifest():
    raw = {'trip_no': 'T-SPLIT', 'waybill_amount': '100',
           'manifest': ['00001', '00002'],
           'ticket_freights': {'00001': '100'}}
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': [raw]}})
    row = trip(state, 'T-SPLIT')
    assert any(error['code'] == 'FREIGHT_TICKET_MISSING' for error in row['errors'])
    assert row['sealable'] is False


def test_ticket_freight_outside_manifest():
    raw = {'trip_no': 'T-SPLIT', 'waybill_amount': '100',
           'manifest': ['00001'],
           'ticket_freights': {'00001': '100', '00099': '5'}}
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': [raw]}})
    row = trip(state, 'T-SPLIT')
    assert any(error['code'] == 'FREIGHT_TICKET_UNKNOWN' for error in row['errors'])
    assert row['balanced'] is False


def test_duplicate_ticket_in_manifest():
    raw = {'trip_no': 'T-DUP', 'waybill_amount': '100', 'manifest': ['00001', '00001']}
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': [raw]}})
    row = trip(state, 'T-DUP')
    assert row['sealable'] is False
    assert row['errors'][0]['code'] in {'DUPLICATE_TICKET', 'BAD_TRIP'}


def test_duplicate_trip_no():
    trips = [{'trip_no': 'T-SAME', 'waybill_amount': '10', 'manifest': ['00001']},
             {'trip_no': 'T-SAME', 'waybill_amount': '20', 'manifest': ['00002']}]
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': trips}})
    second = state['freight']['trips'][1]
    assert any(error['code'] == 'DUPLICATE_TRIP' for error in second['errors'])


def test_ticket_cannot_be_in_two_trips():
    trips = [{'trip_no': 'A', 'waybill_amount': '100', 'manifest': ['00001', '00002']},
             {'trip_no': 'B', 'waybill_amount': '50', 'manifest': ['00001', '00003']}]
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': trips}})
    first, second = state['freight']['trips']
    assert first['balanced'] is True
    assert second['balanced'] is False
    assert any(error['code'] == 'TICKET_IN_MULTIPLE_TRIPS' for error in second['errors'])
    row = next(line for line in second['ledger'] if line['ticket'] == '00001')
    assert row['book'] == '不入账'


def test_missing_waybill_amount():
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': [
        {'trip_no': 'T-NOAMT', 'manifest': ['00001']}]}})
    row = trip(state, 'T-NOAMT')
    assert row['sealable'] is False
    assert row['errors'][0]['code'] == 'MISSING_VALUE'


def test_bad_manifest_shape():
    state = evaluate({'dataset': 'clean', 'overrides': {'trips': [
        {'trip_no': 'T-BAD', 'waybill_amount': '10', 'manifest': '00001'}]}})
    assert trip(state, 'T-BAD')['sealable'] is False


def test_rerun_same_freight_digest(sample):
    again = evaluate({'dataset': 'dirty'})
    assert again['freight']['seal'] == sample['freight']['seal']
