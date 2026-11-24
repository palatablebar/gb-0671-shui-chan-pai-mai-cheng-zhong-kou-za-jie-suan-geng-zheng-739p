import hashlib, json

def archive_decision(state):
    freight = state.get('freight') or {}
    freight_ok = bool(freight.get('trips')) and freight.get('balanced', True) and not freight.get('unassigned_count')
    ready = (state['balance']['row_balanced'] and state['balance']['money_balanced']
             and not state['balance']['blocked'] and freight_ok)
    seal = hashlib.sha256(json.dumps(
        {'final': state['final'], 'freight': freight.get('seal', '')},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'seal': seal, 'status': '可归档' if ready else '待处理',
            'tickets': len(state['final']), 'reasons': len(state['quarantine']),
            'freight_seal': freight.get('seal', ''),
            'freight_balanced': freight.get('balanced', False),
            'unassigned': freight.get('unassigned_count', 0)}

def apply(state):
    state['archive'] = archive_decision(state)
    state['views']['archive'] = {
        'title': '结算封存', 'rows': state['version_audit'],
        'metrics': {'封存票': state['archive']['tickets'], '隔离证据': len(state['quarantine']),
                    '状态': state['archive']['status'],
                    '承运封存': state['archive']['freight_seal'][:16] or '—'}}
