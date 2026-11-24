"""承运费裁决：同车多票的唯一、可追溯结果。

规则要点
- 一车次（承运单）拥有唯一票号清单与承运费总额；清单外的生效票不能计入该车。
- 每票采用的计价重：已结票取生效版 billable_kg；未结票取暂定版或车次申报重量；
  车次票号对不上、且没有任何重量依据的票不得入账，整单不得封存。
- 承运单金额按计价重以“分”为单位分摊；各票承运费合计必须恰等于承运单金额。
  因此已结票只承担自己的份额，不会替未结票垫付车费；未结票的份额以预留值留痕。
- 同车重跑同一输入不新增承运费；已发布结果遇到后补更正形成独立调整版本。
"""
from collections import defaultdict
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import hashlib, json

from app.coerce import money, number
from app.config import RULE_VERSION
from app.errors import BusinessError

CENT = Decimal('0.01')
ZERO = Decimal('0.00')


def _text(value):
    return '' if value is None else str(value).strip()


def _amount(value, label='金额'):
    if value is None or _text(value) == '':
        raise BusinessError('MISSING_VALUE', f'{label}缺失')
    return number(value)


def _manifest_items(raw):
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise BusinessError('BAD_TRIP', '车次票号清单须为数组')
    items = []
    for entry in raw:
        declared = None
        if isinstance(entry, str):
            ticket = entry.strip()
        elif isinstance(entry, dict):
            ticket = _text(entry.get('ticket') or entry.get('ticket_no') or entry.get('id'))
            raw_weight = entry.get('declared_weight', entry.get('weight_kg', entry.get('declared_kg')))
            if raw_weight not in (None, ''):
                declared = number(raw_weight)
                if declared < 0:
                    raise BusinessError('INVALID_RANGE', '申报重量不能为负')
        else:
            raise BusinessError('BAD_TRIP', '车次票号格式无效')
        if not ticket:
            raise BusinessError('MISSING_ID', '车次票号缺失')
        items.append({'ticket': ticket, 'declared_weight': declared})
    return items


def _ticket_freights(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BusinessError('BAD_TRIP', '票面承运费须为对象')
    out = {}
    for ticket, value in raw.items():
        key = _text(ticket)
        if not key:
            raise BusinessError('MISSING_ID', '票面承运费票号缺失')
        amount = _amount(value, '票面承运费')
        if amount < 0:
            raise BusinessError('INVALID_RANGE', '票面承运费不能为负')
        out[key] = amount.quantize(CENT)
    return out


def normalize_trip(raw, index=0):
    if not isinstance(raw, dict):
        raise BusinessError('BAD_TRIP', '承运单须为对象')
    trip_no = _text(raw.get('trip_no') or raw.get('trip_id') or raw.get('code'))
    if not trip_no:
        raise BusinessError('MISSING_ID', '车次号缺失')
    waybill = _amount(raw.get('waybill_amount', raw.get('amount', raw.get('freight_amount'))), '承运单金额')
    if waybill < 0:
        raise BusinessError('INVALID_RANGE', '承运单金额不能为负')
    manifest = _manifest_items(raw.get('manifest', raw.get('tickets', raw.get('ticket_nos'))))
    seen = set()
    for item in manifest:
        if item['ticket'] in seen:
            raise BusinessError('DUPLICATE_TICKET', f"车次 {trip_no} 票号 {item['ticket']} 重复")
        seen.add(item['ticket'])
    return {
        'trip_no': trip_no,
        'carrier': _text(raw.get('carrier') or raw.get('carrier_name')),
        'driver': _text(raw.get('driver')),
        'departure_at': _text(raw.get('departure_at')),
        'currency': _text(raw.get('currency')) or 'CNY',
        'waybill': waybill.quantize(CENT),
        'manifest': manifest,
        'ticket_freights': _ticket_freights(raw.get('ticket_freights', raw.get('freight_split'))),
        'index': index,
    }


def allocate(waybill, weighted_tickets):
    """最大余数分摊；返回 {ticket: Decimal}，合计恰为承运单金额（分）。"""
    result = {ticket: ZERO for ticket, _ in weighted_tickets}
    total = sum((weight for _, weight in weighted_tickets), Decimal(0))
    cents = int((Decimal(waybill) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    if total <= 0 or cents <= 0:
        return result
    parts = []
    used = 0
    for ticket, weight in weighted_tickets:
        exact = Decimal(cents) * weight / total
        floor = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        used += floor
        parts.append((ticket, exact - floor, floor))
    remainder = cents - used
    parts.sort(key=lambda row: (-row[1], row[0]))
    for ticket, _, floor in parts:
        add = 1 if remainder > 0 else 0
        if add:
            remainder -= 1
        result[ticket] = (Decimal(floor + add) / 100).quantize(CENT)
    return result


def _provisional_weights(state):
    """未生效票的暂定计价重：取该票最新版本已算出的计价重，仅作分摊依据。"""
    grouped = defaultdict(list)
    for row in state.get('settlements', []):
        grouped[row['ticket']].append(row)
    provisional = {}
    for ticket, rows in grouped.items():
        row = max(rows, key=lambda item: int(item.get('version') or 0))
        try:
            provisional[ticket] = Decimal(row['billable_kg'])
        except (KeyError, ArithmeticError):
            pass
    return provisional


def _conflict_tickets(state):
    return {row.get('ticket') for row in state.get('version_conflicts', []) if row.get('ticket')}


def _seal(evidence):
    return hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def _empty_trip(raw, index, error):
    trip_no = _text(raw.get('trip_no')) if isinstance(raw, dict) else ''
    return {'trip_no': trip_no or f'#{index + 1}', 'carrier': '', 'driver': '',
            'departure_at': '', 'currency': 'CNY', 'waybill_amount': '0.00', 'allocated': '0.00', 'unallocated': '0.00',
            'balanced': False, 'settled_count': 0, 'pending_count': 0,
            'blocked': 1, 'sealable': False, 'status': '待处理', 'seal': '',
            'ledger': [], 'errors': [{'code': error.code, 'detail': error.message}]}


def build_trip(raw, state, index, claimed, claimed_tickets):
    try:
        trip = normalize_trip(raw, index)
    except BusinessError as error:
        return _empty_trip(raw, index, error)

    trip_no = trip['trip_no']
    errors = []
    if trip_no in claimed:
        errors.append({'code': 'DUPLICATE_TRIP', 'detail': f'车次号 {trip_no} 重复'})
    else:
        claimed[trip_no] = {item['ticket'] for item in trip['manifest']}
    # 同一票出现在两个车次：后出现的车次不能重复摊费，也不能把它当作已结票。
    duplicates = sorted({item['ticket'] for item in trip['manifest']} & claimed_tickets)
    for ticket in duplicates:
        errors.append({'code': 'TICKET_IN_MULTIPLE_TRIPS',
                       'detail': f'票号 {ticket} 已属于其他车次，本车不得重复分摊'})
    duplicates = set(duplicates)

    final = {row['ticket']: row for row in state.get('final', [])}
    provisional = _provisional_weights(state)
    conflicts = _conflict_tickets(state)

    rows = []
    missing = []
    for item in trip['manifest']:
        ticket = item['ticket']
        if ticket in duplicates:
            rows.append({'ticket': ticket, 'version': '', 'billable_kg': '',
                         'weight': None, 'weight_source': '缺失', 'status': '未入账',
                         'book': '不入账', 'amount': '0.00', 'paid': '0.00', 'due': '0.00',
                         'fish': '', 'reason': '票号已属于其他车次，本车不能重复分摊'})
            missing.append(ticket)
            continue
        if ticket in final:
            row = final[ticket]
            weight = Decimal(row['billable_kg'])
            rows.append({'ticket': ticket, 'version': row.get('version', ''),
                         'billable_kg': row.get('billable_kg', ''), 'weight': weight,
                         'weight_source': '生效', 'status': '已结', 'book': '入账',
                         'amount': row.get('amount', '0.00'), 'paid': row.get('paid', '0.00'),
                         'due': row.get('due', '0.00'), 'fish': row.get('fish', ''),
                         'reason': ''})
            continue
        if ticket in provisional:
            weight = provisional[ticket]
            source, reason = '暂定', '版本待裁决'
        elif item['declared_weight'] is not None:
            weight = item['declared_weight']
            source, reason = '申报', '结算未闭合'
        else:
            # 既没有生效版，也没有暂定/申报重量：票号与车次对不上，不得入账。
            reason = '车次票号对不上，不能入账' if ticket in conflicts else '票号在车次清单中但无重量依据'
            missing.append(ticket)
            rows.append({'ticket': ticket, 'version': '', 'billable_kg': '',
                         'weight': None, 'weight_source': '缺失', 'status': '未入账',
                         'book': '不入账', 'amount': '0.00', 'paid': '0.00', 'due': '0.00',
                         'fish': '', 'reason': reason})
            continue
        rows.append({'ticket': ticket, 'version': '', 'billable_kg': '',
                     'weight': weight, 'weight_source': source, 'status': '待定',
                     'book': '待定', 'amount': '0.00', 'paid': '0.00', 'due': '0.00',
                     'fish': '', 'reason': reason})

    waybill = trip['waybill']
    if not trip['manifest']:
        errors.append({'code': 'EMPTY_MANIFEST', 'detail': '承运单没有票号清单，不能分摊承运费'})
    if trip['ticket_freights'] is not None:
        # 承运商给出逐票分项：必须逐票齐全且合计等于承运单，否则不得封存。
        given = trip['ticket_freights']
        unknown = sorted(set(given) - {item['ticket'] for item in trip['manifest']})
        absent = sorted({item['ticket'] for item in trip['manifest']} - set(given))
        allocation = {row['ticket']: (given.get(row['ticket'], ZERO)
                                       if row['status'] != '未入账' else ZERO)
                      for row in rows}
        # 合计核对覆盖承运单上列出的全部票号；清单外的分项也必须计入，
        # 否则多出来的金额会凭空消失、账面看似相符。已分摊额只计实际入账票。
        given_total = sum(given.values(), ZERO)
        allocated = sum(allocation.values(), ZERO)
        balanced = given_total == waybill and allocated == waybill and not unknown and not absent
        if missing:
            errors.append({'code': 'UNALLOCATED_FREIGHT',
                           'detail': f"票号 {'、'.join(missing)} 无计价重，承运费无法完整分摊"})
        if unknown:
            errors.append({'code': 'FREIGHT_TICKET_UNKNOWN',
                           'detail': f"票面承运费含清单外票号：{'、'.join(unknown)}"})
        if absent:
            errors.append({'code': 'FREIGHT_TICKET_MISSING',
                           'detail': f"票面承运费缺少票号：{'、'.join(absent)}"})
        if not balanced:
            errors.append({'code': 'FREIGHT_NOT_BALANCED',
                           'detail': f'各票承运费合计 {money(allocated)} 与承运单 {money(waybill)} 不一致'})
    elif missing:
        # 有票号既无生效重、又无申报重，无法给出完整分摊，不得把车费挤给其余票。
        allocation = {row['ticket']: ZERO for row in rows}
        allocated = ZERO
        balanced = False
        errors.append({'code': 'UNALLOCATED_FREIGHT',
                       'detail': f"票号 {'、'.join(missing)} 无计价重，承运费无法完整分摊"})
    elif trip['manifest']:
        weighted = [(row['ticket'], row['weight']) for row in rows if row['weight'] > 0]
        allocation = allocate(waybill, weighted)
        allocated = sum(allocation.values(), ZERO)
        balanced = allocated == waybill
        if not balanced:
            errors.append({'code': 'FREIGHT_NOT_BALANCED',
                           'detail': f'各票承运费合计 {money(allocated)} 与承运单 {money(waybill)} 不一致'})
    else:
        allocation = {}
        allocated = ZERO
        balanced = False
    unallocated = (waybill - allocated).quantize(CENT)

    ledger = []
    settled = pending = 0
    for row in rows:
        freight = allocation.get(row['ticket'], ZERO)
        ledger.append({
            'ticket': row['ticket'], 'version': row['version'],
            'billable_kg': row['billable_kg'] if row['weight_source'] == '生效' else
                           (str(row['weight']) if row['weight'] is not None else ''),
            'weight_source': row['weight_source'], 'status': row['status'], 'book': row['book'],
            'freight': money(freight), 'amount': row['amount'], 'paid': row['paid'],
            'due': row['due'], 'fish': row['fish'], 'reason': row['reason'],
        })
        if row['status'] == '已结':
            settled += 1
        else:
            pending += 1

    sealable = balanced and not errors
    result = {
        'trip_no': trip_no, 'carrier': trip['carrier'], 'driver': trip['driver'],
        'departure_at': trip['departure_at'], 'currency': trip['currency'],
        'waybill_amount': money(waybill), 'allocated': money(allocated),
        'unallocated': money(unallocated), 'balanced': balanced,
        'settled_count': settled, 'pending_count': pending,
        'blocked': len(errors), 'errors': errors,
        'sealable': sealable, 'status': '可封存' if sealable else '待处理',
        'ledger': ledger,
    }
    result['seal'] = _seal({'trip_no': trip_no, 'waybill_amount': money(waybill),
                            'currency': trip['currency'], 'allocated': money(allocated),
                            'ledger': ledger, 'rule_version': RULE_VERSION})
    return result


def preview_adjustment(published, trip):
    """只读对比：已发布结果 vs 当前草稿，给出每票采用的计价重与补退。"""
    old = {row['ticket']: row for row in (published.get('ledger') or [])}
    new = {row['ticket']: row for row in (trip.get('ledger') or [])}
    preview = []
    for ticket in sorted(set(old) | set(new)):
        before = old.get(ticket, {})
        after = new.get(ticket, {})
        freight_before = Decimal(str(before.get('freight', '0')))
        freight_after = Decimal(str(after.get('freight', '0')))
        amount_before = Decimal(str(before.get('amount', '0')))
        amount_after = Decimal(str(after.get('amount', '0')))
        preview.append({
            'ticket': ticket,
            'weight_before': before.get('billable_kg', ''),
            'weight_after': after.get('billable_kg', ''),
            'weight_source': after.get('weight_source', ''),
            'status_before': before.get('status', '未入账'),
            'status_after': after.get('status', '未入账'),
            'freight_before': money(freight_before),
            'freight_after': money(freight_after),
            'freight_delta': money(freight_after - freight_before),
            'amount_delta': money(amount_after - amount_before),
            'net_delta': money((freight_after - freight_before) + (amount_after - amount_before)),
        })
    return preview


def publish_payload(trip):
    payments = [{'ticket': row['ticket'], 'version': row['version'],
                 'amount': row['amount'], 'paid': row['paid'], 'due': row['due']}
                for row in trip['ledger'] if row['status'] == '已结']
    reserves = [{'ticket': row['ticket'], 'version': row['version'],
                 'billable_kg': row['billable_kg'], 'weight_source': row['weight_source'],
                 'freight': row['freight'], 'reason': row['reason']}
                for row in trip['ledger'] if row['status'] != '已结']
    return {
        'trip_no': trip['trip_no'], 'carrier': trip.get('carrier', ''),
        'currency': trip.get('currency', 'CNY'),
        'waybill_amount': trip['waybill_amount'],
        'allocated': trip['allocated'], 'unallocated': trip['unallocated'],
        'balanced': trip['balanced'], 'sealable': trip['sealable'],
        'settled_count': trip['settled_count'], 'pending_count': trip['pending_count'],
        'seal': trip['seal'], 'ledger': trip['ledger'],
        'payments': payments, 'reserves': reserves,
    }


def result_digest(payload):
    evidence = {
        'trip_no': payload['trip_no'],
        'waybill_amount': payload['waybill_amount'],
        'currency': payload['currency'],
        'seal': payload['seal'],
        'ledger': payload['ledger'],
        'payments': payload['payments'],
        'reserves': payload.get('reserves', []),
        'rule_version': RULE_VERSION,
    }
    return hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def apply(state):
    raw_trips = state.get('context', {}).get('trips') or []
    if isinstance(raw_trips, dict):
        raw_trips = [raw_trips]
    claimed = {}
    claimed_tickets = set()
    trips = []
    for index, raw in enumerate(raw_trips):
        trips.append(build_trip(raw, state, index, claimed, claimed_tickets))
        try:
            claimed_tickets.update(item['ticket'] for item in normalize_trip(raw, index)['manifest'])
        except BusinessError:
            pass

    assigned = {row['ticket'] for trip in trips for row in trip.get('ledger', [])}
    stray = [{'ticket': row['ticket'], 'version': row.get('version', ''),
              'billable_kg': row.get('billable_kg', '')} for row in state.get('final', [])
             if row['ticket'] not in assigned]
    for row in state.get('version_conflicts', []):
        if row.get('ticket') and row['ticket'] not in assigned:
            stray.append({'ticket': row['ticket'], 'version': '', 'billable_kg': ''})
    unassigned = []
    seen = set()
    for row in stray:
        if row['ticket'] in seen:
            continue
        seen.add(row['ticket'])
        unassigned.append({**row, 'status': '不入账', 'code': 'TRIP_TICKET_MISMATCH',
                           'detail': '车次票号对不上，该票不能计入任何承运单'})

    rows = [{**row, 'trip_no': trip['trip_no']} for trip in trips for row in trip.get('ledger', [])]
    state['freight'] = {
        'trips': trips, 'unassigned': unassigned, 'rows': rows,
        'balanced': all(trip.get('balanced', False) for trip in trips),
        'sealable': all(trip.get('sealable', False) for trip in trips),
        'blocked': sum(trip.get('blocked', 0) for trip in trips),
        'pending_count': sum(trip.get('pending_count', 0) for trip in trips),
        'unassigned_count': len(unassigned),
        'seal': _seal([{'trip_no': trip['trip_no'], 'seal': trip.get('seal', '')} for trip in trips]) if trips else '',
    }
    total_allocated = sum((Decimal(trip['allocated']) for trip in trips), ZERO)
    total_waybill = sum((Decimal(trip['waybill_amount']) for trip in trips), ZERO)
    state['views']['freight'] = {
        'title': '承运结算', 'rows': rows,
        'metrics': {
            '车次': len(trips),
            '已结票': sum(trip.get('settled_count', 0) for trip in trips),
            '待定票': state['freight']['pending_count'],
            '未入账票': len(unassigned),
            '分摊合计_CNY': money(total_allocated),
            '承运单_CNY': money(total_waybill),
        },
    }
