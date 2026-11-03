"""Persistent local printer registry. Jobs retain their original destination."""
import json
import time
import uuid

from capabilities import normalize, fingerprint


def initialize(c, uri, name):
    c.execute('CREATE TABLE IF NOT EXISTS printers(id TEXT PRIMARY KEY,name TEXT,uri TEXT,revision INTEGER,capabilities TEXT,capability_hash TEXT,state TEXT,is_default INTEGER,enabled INTEGER,updated REAL)')
    if uri and not c.execute('SELECT 1 FROM printers LIMIT 1').fetchone():
        c.execute('INSERT INTO printers VALUES(?,?,?,?,?,?,?,?,?,?)', ('legacy', name, uri, 1, '{}', '', '{}', 1, 1, time.time()))
    for table, fields in (('prepared', {'printer_id': 'TEXT', 'ticket': 'TEXT', 'kind': "TEXT DEFAULT 'photo'"}), ('jobs', {'printer_id': 'TEXT'})):
        existing = {r['name'] for r in c.execute('PRAGMA table_info(' + table + ')')}
        for name, spec in fields.items():
            if name not in existing:
                c.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' ' + spec)
    c.execute("UPDATE prepared SET printer_id='legacy' WHERE printer_id IS NULL")
    c.execute("UPDATE jobs SET printer_id=COALESCE((SELECT printer_id FROM prepared WHERE id=jobs.prepared_id),'legacy') WHERE printer_id IS NULL")
    c.execute("UPDATE jobs SET status='cancelled',message='版本升级：旧预览已过期，请重新排版',updated=? WHERE status='pending' AND prepared_id IN (SELECT id FROM prepared WHERE ticket IS NULL)", (time.time(),))


def unpack(row):
    if row is None:
        raise ValueError('请先在设置中添加打印机')
    item = dict(row)
    for key in ('capabilities', 'state'):
        item[key] = json.loads(item[key])
    item['backend'] = 'ipp-jpeg'
    return item


def get(c, printer_id=None):
    return unpack(c.execute('SELECT * FROM printers WHERE enabled=1 AND id=?', (printer_id,)).fetchone() if printer_id else c.execute('SELECT * FROM printers WHERE enabled=1 ORDER BY is_default DESC,updated LIMIT 1').fetchone())


def all_devices(c):
    return [unpack(r) for r in c.execute('SELECT * FROM printers WHERE enabled=1 ORDER BY is_default DESC,updated')]


def busy(c, printer_id):
    if c.execute("SELECT 1 FROM jobs WHERE printer_id=? AND status IN ('pending','sending','submitted','unknown')", (printer_id,)).fetchone():
        raise ValueError('该设备有未结束的任务，请先等待或核对打印结果')


def save(c, name, uri, attrs):
    if c.execute('SELECT COUNT(*) FROM printers WHERE enabled=1').fetchone()[0] >= 8:
        raise ValueError('最多保存 8 台打印机')
    if c.execute('SELECT 1 FROM printers WHERE enabled=1 AND uri=?', (uri,)).fetchone():
        raise ValueError('此地址已添加，请刷新已有设备')
    key = uuid.uuid4().hex
    c.execute('INSERT INTO printers VALUES(?,?,?,?,?,?,?,?,?,?)', (key, name, uri, 1, '{}', '', '{}', int(not all_devices(c)), 1, time.time()))
    refresh(c, key, attrs)
    return get(c, key)


def refresh(c, key, attrs):
    caps = normalize(attrs)
    # Readiness changes do not alter the immutable geometry of a preview.
    geometry = dict(caps, media=[{k: v for k, v in m.items() if k not in ('ready', 'label')} for m in caps['media']])
    geometry['media'].sort(key=lambda m: m['id'])
    digest = fingerprint(geometry)
    state = {'online': True, 'accepting': attrs.get('printer-is-accepting-jobs', [False])[0], 'state': attrs.get('printer-state', [0])[0], 'checked': time.time(), 'reasons': attrs.get('printer-state-reasons', [])}
    state['message'] = {3: '空闲，可以打印', 4: '正在打印', 5: '打印机已暂停'}.get(state['state'], '已连接')
    if not caps['compatible']:
        state['message'] = '暂不兼容：' + '；'.join(caps['reasons'])
    c.execute('UPDATE printers SET capabilities=?,capability_hash=?,state=?,updated=? WHERE id=?', (json.dumps(caps, ensure_ascii=False), digest, json.dumps(state, ensure_ascii=False), time.time(), key))


def assert_ticket(c, ticket):
    device = get(c, ticket['printer_id'])
    if device['revision'] != ticket['revision'] or device['uri'] != ticket['printer_uri'] or device['capability_hash'] != ticket['capability_hash']:
        raise ValueError('打印机配置或能力已变化，请重新生成预览')
    return device
