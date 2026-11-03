import collections
import contextlib
import ctypes
import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from imaging import render_page, thumbnail, icon_png
from capabilities import make_ticket, page_geometry
import devices
from ipp import Printer
from security import Credentials

ROOT = Path(os.environ.get('PHOTO_ROOT', '/print-workspace/待打印')).resolve()
COMPLETED = Path(os.environ.get('COMPLETED_ROOT', '/print-workspace/已完成')).resolve()
DATA = Path(os.environ.get('DATA_ROOT', '/data'))
STATIC = Path(__file__).parent / 'static'
URI = os.environ.get('PRINTER_URI', '')
PRINTER_NAME = os.environ.get('PRINTER_NAME', '照片打印机')
FORMATS = {'.heic', '.heif', '.jpg', '.jpeg', '.png'}
scan_lock = threading.Lock()
image_lock = threading.Lock()
session_lock = threading.Lock()
queue_lock = threading.RLock()
archive_lock = threading.Lock()
sessions = {}
attempts = collections.defaultdict(list)
scan_state = {'running': False, 'updated': None, 'error': None}
printer_state = {'online': False, 'message': '正在连接打印机', 'checked': 0}
stop = threading.Event()


def db():
    connection = sqlite3.connect(DATA / 'library.sqlite3', timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def init():
    if ROOT == COMPLETED or ROOT.is_relative_to(COMPLETED) or COMPLETED.is_relative_to(ROOT):
        raise ValueError('待打印与已完成目录必须互相独立，不能互相包含')
    DATA.mkdir(parents=True, exist_ok=True)
    for directory in ('thumbs', 'prepared'):
        (DATA / directory).mkdir(exist_ok=True)
    Credentials(DATA).initialize()
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY, rel TEXT UNIQUE, day TEXT, size INTEGER, mtime REAL, seen TEXT);
        CREATE INDEX IF NOT EXISTS photos_day ON photos(day DESC);
        CREATE TABLE IF NOT EXISTS prepared(id TEXT PRIMARY KEY, photo_id TEXT, landscape INTEGER, fit TEXT, rotation INTEGER, created REAL, width INTEGER, height INTEGER);
        CREATE TABLE IF NOT EXISTS submissions(id TEXT PRIMARY KEY, created REAL, payload TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, submission TEXT, prepared_id TEXT, status TEXT, printer_job INTEGER, message TEXT, created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS archives(prepared_id TEXT, photo_id TEXT, destination TEXT, status TEXT, message TEXT, updated REAL, PRIMARY KEY(prepared_id,photo_id));
        CREATE TABLE IF NOT EXISTS prepared_sources(prepared_id TEXT, photo_id TEXT, source_rel TEXT, source_sha256 TEXT, PRIMARY KEY(prepared_id,photo_id));
        ''')
        columns = {r['name'] for r in c.execute('PRAGMA table_info(prepared)')}
        for name in ('source_rel', 'source_sha256'):
            if name not in columns:
                c.execute('ALTER TABLE prepared ADD COLUMN ' + name + ' TEXT')
        if 'paper' not in columns:
            c.execute("ALTER TABLE prepared ADD COLUMN paper TEXT DEFAULT '6inch'")
        devices.initialize(c, URI, PRINTER_NAME)
        # A crash during submission is ambiguous: never automatically print again.
        c.execute("UPDATE jobs SET status='unknown',message='服务在发送或等待期间重启，请先核对是否已出片，勿直接重复打印',updated=? WHERE status IN ('sending','submitted')", (time.time(),))


def photo_path(row):
    path = (ROOT / row['rel']).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise HTTPException(404, '照片已移动或尚未同步，请刷新图库')
    return path


def get_photo(photo_id):
    with db() as c:
        row = c.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '找不到这张照片')
    return row


def scan():
    if not scan_lock.acquire(blocking=False):
        return
    scan_state.update(running=True, error=None)
    generation = uuid.uuid4().hex
    try:
        if not ROOT.is_dir():
            raise ValueError('照片目录未挂载')
        rows = []
        with db() as c:
            def fail_walk(error):
                raise error
            for base, dirs, names in os.walk(ROOT, followlinks=False, onerror=fail_walk):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('@eaDir', '#recycle') and not (Path(base) / d).is_symlink()]
                for name in names:
                    if Path(name).suffix.lower() not in FORMATS or name.startswith('.'):
                        continue
                    path = Path(base) / name
                    if path.is_symlink():
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if stat.st_size == 0:
                        continue
                    rel = path.relative_to(ROOT).as_posix()
                    parts = Path(rel).parts
                    day = '/'.join(parts[:3]) if len(parts) >= 4 and all(p.isdigit() for p in parts[:3]) else datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y/%m/%d')
                    rows.append((hashlib.sha256(rel.encode()).hexdigest(), rel, day, stat.st_size, stat.st_mtime, generation))
                    if len(rows) >= 500:
                        c.executemany('INSERT INTO photos VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,seen=excluded.seen', rows)
                        c.commit()
                        rows.clear()
            c.executemany('INSERT INTO photos VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,seen=excluded.seen', rows)
            c.execute('DELETE FROM photos WHERE seen<>?', (generation,))
        scan_state['updated'] = time.time()
    except Exception:
        scan_state['error'] = '图库扫描失败，请检查照片目录和访问权限'
    finally:
        scan_state['running'] = False
        scan_lock.release()


def update_printer():
    global printer_state
    with db() as c:
        targets = devices.all_devices(c)
    for target in targets:
        try:
            attrs = Printer(target['uri'], timeout=4, lan_only=True).capabilities()
            with queue_lock, db() as c:
                current = devices.get(c, target['id'])
                if current['revision'] == target['revision']:
                    devices.refresh(c, target['id'], attrs)
        except Exception:
            with db() as c:
                state = {'online': False, 'accepting': False, 'message': '未连接，请确认开机和家庭 Wi-Fi', 'checked': time.time()}
                c.execute('UPDATE printers SET state=? WHERE id=?', (json.dumps(state, ensure_ascii=False), target['id']))
    with db() as c:
        targets = devices.all_devices(c)
    printer_state = dict(targets[0]['state'], name=targets[0]['name']) if targets else {'online': False, 'accepting': False, 'message': '请在设置中添加打印机'}


def set_job(job_id, status, message, printer_job=None):
    with db() as c:
        c.execute('UPDATE jobs SET status=?,message=?,printer_job=COALESCE(?,printer_job),updated=? WHERE id=?', (status, message, printer_job, time.time(), job_id))


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def move_no_replace(source, target):
    # Both folders live under one Docker bind mount. Linux renameat2 is atomic
    # and rejects a pre-existing destination instead of replacing it.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise ValueError('完成目录存在同名文件，未覆盖，请手动核对')
        raise OSError(code, os.strerror(code))


def archive_completed(prepared_id):
    """Archive all source copies only after every copy of a sheet completes."""
    with archive_lock:
        with db() as c:
            remaining = c.execute("SELECT 1 FROM jobs WHERE prepared_id=? AND status<>'completed' LIMIT 1", (prepared_id,)).fetchone()
            if remaining:
                return
            sources = c.execute('SELECT * FROM prepared_sources WHERE prepared_id=?', (prepared_id,)).fetchall()
        all_archived = bool(sources)
        for source_row in sources:
            photo_id = source_row['photo_id']
            with db() as c:
                event = c.execute('SELECT * FROM archives WHERE prepared_id=? AND photo_id=?', (prepared_id, photo_id)).fetchone()
                if event and event['status'] == 'archived':
                    continue
                if event is None:
                    original = Path(source_row['source_rel'])
                    day = datetime.datetime.now().strftime('%Y-%m-%d')
                    destination = day + '/' + original.stem + '--' + prepared_id[:10] + '-' + photo_id[:6] + original.suffix
                    c.execute('INSERT INTO archives VALUES(?,?,?,?,?,?)', (prepared_id, photo_id, destination, 'moving', '正在归档', time.time()))
                else:
                    destination = event['destination']
            try:
                source = ROOT / source_row['source_rel']
                target = COMPLETED / destination
                if source.is_symlink() or not source.resolve().is_relative_to(ROOT) or not target.resolve().is_relative_to(COMPLETED):
                    raise ValueError('文件路径已变化，未移动')
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.exists():
                    if file_hash(source) != source_row['source_sha256']:
                        raise ValueError('照片在预览后被修改，未移动，请手动核对')
                    move_no_replace(source, target)
                elif not target.is_file() or file_hash(target) != source_row['source_sha256']:
                    raise ValueError('找不到待归档照片，请手动核对')
                with db() as c:
                    c.execute("UPDATE archives SET status='archived',message='已移至已完成文件夹',updated=? WHERE prepared_id=? AND photo_id=?", (time.time(), prepared_id, photo_id))
                    c.execute('DELETE FROM photos WHERE id=?', (photo_id,))
            except (OSError, ValueError, AttributeError) as e:
                all_archived = False
                message = str(e) if isinstance(e, ValueError) else '移动失败，请检查已完成文件夹权限或空间；待打印照片仍保留'
                with db() as c:
                    c.execute("UPDATE archives SET status='failed',message=?,updated=? WHERE prepared_id=? AND photo_id=?", (message, time.time(), prepared_id, photo_id))
        with db() as c:
            message = '打印已完成，照片已移至已完成文件夹' if all_archived else '打印已完成；部分照片归档待处理，请检查待打印文件夹'
            c.execute("UPDATE jobs SET message=?,updated=? WHERE prepared_id=? AND status='completed'", (message, time.time(), prepared_id))


def worker():
    while not stop.is_set():
        with db() as c:
            ambiguous = c.execute("SELECT 1 FROM jobs WHERE status='unknown' LIMIT 1").fetchone()
            job = c.execute("SELECT j.*,p.ticket,p.kind FROM jobs j JOIN prepared p ON p.id=j.prepared_id WHERE status='pending' ORDER BY j.created,j.rowid LIMIT 1").fetchone()
        if job is None or ambiguous:
            stop.wait(2)
            continue
        try:
            ticket = json.loads(job['ticket'])
            with db() as c:
                devices.assert_ticket(c, ticket)
            printer = Printer(ticket['printer_uri'], timeout=30, lan_only=True)
            data = (DATA / 'prepared' / (job['prepared_id'] + '.jpg')).read_bytes()
            caps = printer.capabilities()
            if not caps.get('printer-is-accepting-jobs', [False])[0] or caps.get('printer-state', [0])[0] == 5:
                set_job(job['id'], 'failed', '打印机未就绪，本张未发送')
                continue
            printer.validate_ticket(ticket)
        except Exception:
            set_job(job['id'], 'failed', '连接或格式验证失败，本张未发送，请检查打印机')
            continue
        with queue_lock, db() as c:
            claim = c.execute("UPDATE jobs SET status='sending',message='正在发送照片，请勿重复提交',updated=? WHERE id=? AND status='pending'", (time.time(), job['id']))
            if claim.rowcount != 1:
                continue
        try:
            answer = printer.print_ticket(data, ticket)
            printer_job = answer.get('job-id', [None])[0]
            if not isinstance(printer_job, int):
                raise ValueError('没有返回任务编号')
            set_job(job['id'], 'submitted', '打印机已接收，等待出片', printer_job)
        except Exception:
            set_job(job['id'], 'unknown', '发送结果不确定：照片可能已被接收。请先检查出片和打印机队列；系统不会自动重发')
            continue
        deadline = time.monotonic() + 600
        while not stop.wait(3):
            if time.monotonic() > deadline:
                set_job(job['id'], 'unknown', '未能确认出片结果，请检查打印机；系统不会自动重发')
                break
            try:
                attrs = printer.job(printer_job)
                state = attrs.get('job-state', [0])[0]
                if state == 9:
                    set_job(job['id'], 'completed', '打印机报告已完成')
                    if job['kind'] != 'calibration':
                        archive_completed(job['prepared_id'])
                    break
                if state in (7, 8):
                    set_job(job['id'], 'failed', '打印机报告任务已取消或中止，请检查是否部分出片')
                    break
            except Exception:
                continue


def maintenance():
    last_scan = 0
    while not stop.is_set():
        if time.time() - last_scan > 60:
            scan()
            last_scan = time.time()
        update_printer()
        with db() as c:
            ready = [r[0] for r in c.execute("SELECT DISTINCT j.prepared_id FROM jobs j LEFT JOIN archives a ON a.prepared_id=j.prepared_id WHERE j.prepared_id IN (SELECT id FROM prepared WHERE kind='photo') AND j.status='completed' AND (a.status IS NULL OR a.status<>'archived') AND NOT EXISTS (SELECT 1 FROM jobs k WHERE k.prepared_id=j.prepared_id AND k.status<>'completed')")]
        for prepared_id in ready:
            archive_completed(prepared_id)
        with db() as c:
            expired = c.execute("SELECT id FROM prepared WHERE created<? AND id NOT IN (SELECT prepared_id FROM jobs)", (time.time() - 3600,)).fetchall()
            for row in expired:
                (DATA / 'prepared' / (row['id'] + '.jpg')).unlink(missing_ok=True)
                c.execute('DELETE FROM prepared_sources WHERE prepared_id=?', (row['id'],))
                c.execute('DELETE FROM prepared WHERE id=?', (row['id'],))
            old = c.execute("SELECT p.id FROM prepared p WHERE p.created<? AND EXISTS (SELECT 1 FROM jobs j WHERE j.prepared_id=p.id) AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.prepared_id=p.id AND j.status IN ('pending','sending','submitted','unknown'))", (time.time() - 86400,)).fetchall()
            for row in old:
                (DATA / 'prepared' / (row['id'] + '.jpg')).unlink(missing_ok=True)
            valid_thumbs = {hashlib.sha256(f"{r['id']}:{r['size']}:{r['mtime']}".encode()).hexdigest() + '.jpg' for r in c.execute('SELECT id,size,mtime FROM photos')}
        for path in (DATA / 'thumbs').glob('*.jpg'):
            if path.name not in valid_thumbs and time.time() - path.stat().st_mtime > 3600:
                path.unlink(missing_ok=True)
        stop.wait(20)


@contextlib.asynccontextmanager
async def lifespan(app):
    init()
    threads = [threading.Thread(target=f, daemon=True) for f in (maintenance, worker)]
    for thread in threads:
        thread.start()
    yield
    stop.set()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def protect(request: Request, call_next):
    path = request.url.path
    if request.method not in ('GET', 'HEAD'):
        if request.headers.get('X-Photo-Request') != '1':
            return JSONResponse({'detail': '请求验证失败'}, status_code=403)
        if int(request.headers.get('content-length', '0')) > 65536:
            return JSONResponse({'detail': '请求过大'}, status_code=413)
    if path.startswith('/api/') and path != '/api/login':
        cookie = request.cookies.get('photo_session', '')
        key = hashlib.sha256(cookie.encode()).hexdigest()
        with session_lock:
            expired = [k for k, expiry in sessions.items() if expiry < time.time()]
            for k in expired:
                del sessions[k]
            valid = bool(cookie) and key in sessions
        if not valid:
            return JSONResponse({'detail': '请先登录'}, status_code=401)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
    response.headers['Cache-Control'] = 'no-store' if path.startswith('/api/') else 'no-cache'
    return response


class Login(BaseModel):
    password: str = Field(max_length=200)


@app.post('/api/login')
def login(payload: Login, request: Request):
    host = request.client.host if request.client else 'local'
    with session_lock:
        now = time.time()
        attempts[host] = [t for t in attempts[host] if now - t < 60]
        if len(attempts[host]) >= 5:
            raise HTTPException(429, '尝试次数较多，请一分钟后再试')
        if not Credentials(DATA).verify(payload.password):
            attempts[host].append(now)
            raise HTTPException(401, '密码不正确')
        attempts.pop(host, None)
        token = secrets.token_urlsafe(32)
        sessions[hashlib.sha256(token.encode()).hexdigest()] = now + 86400 * 7
    response = JSONResponse({'ok': True})
    # TLS may terminate at the household reverse proxy. Accept only a literal
    # forwarded scheme; it changes cookie security, never identity or routing.
    secure = request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https'
    response.set_cookie('photo_session', token, max_age=86400 * 7, httponly=True, samesite='strict', secure=secure)
    return response


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)


@app.post('/api/password')
def change_password(payload: PasswordChange, request: Request):
    if payload.new_password != payload.confirm_password:
        raise HTTPException(400, '两次输入的新密码不一致')
    host = request.client.host if request.client else 'local'
    with session_lock:
        now = time.time()
        attempts[host] = [t for t in attempts[host] if now - t < 60]
        if len(attempts[host]) >= 5:
            raise HTTPException(429, '尝试次数较多，请一分钟后再试')
        try:
            Credentials(DATA).change(payload.current_password, payload.new_password)
        except ValueError as e:
            attempts[host].append(now)
            raise HTTPException(403, str(e))
        sessions.clear()
        attempts.clear()
        token = secrets.token_urlsafe(32)
        sessions[hashlib.sha256(token.encode()).hexdigest()] = now + 86400 * 7
    response = JSONResponse({'ok': True, 'message': '密码已修改，其他登录会话已失效'})
    secure = request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https'
    response.set_cookie('photo_session', token, max_age=86400 * 7, httponly=True, samesite='strict', secure=secure)
    return response


@app.post('/api/logout')
def logout(request: Request):
    with session_lock:
        sessions.pop(hashlib.sha256(request.cookies.get('photo_session', '').encode()).hexdigest(), None)
    response = JSONResponse({'ok': True})
    response.delete_cookie('photo_session')
    return response


@app.get('/api/status')
def status():
    with db() as c:
        count = c.execute('SELECT COUNT(*) FROM photos').fetchone()[0]
        latest = c.execute('SELECT MAX(day) FROM photos WHERE day<>?', ('其他照片',)).fetchone()[0]
        archived = c.execute("SELECT COUNT(*) FROM archives WHERE status='archived'").fetchone()[0]
    return {'version': '0.2.0', 'printer': printer_state, 'library': dict(scan_state, count=count, latest=latest, archived=archived)}


@app.get('/api/dates')
def dates():
    with db() as c:
        return [dict(row) for row in c.execute('SELECT day,COUNT(*) AS count FROM photos GROUP BY day ORDER BY day DESC')]


@app.get('/api/printers')
def printers():
    with db() as c:
        return devices.all_devices(c)


class AddPrinter(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    uri: str = Field(min_length=8, max_length=512)


@app.post('/api/printers')
def add_printer(payload: AddPrinter):
    try:
        printer = Printer(payload.uri.strip(), timeout=8, lan_only=True)
        attrs = printer.capabilities()
        with queue_lock, db() as c:
            return devices.save(c, payload.name.strip() or '照片打印机', payload.uri.strip(), attrs)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(409, '无法连接，请核对 IPP 地址、开机状态和证书；尚未保存')


class DeviceAction(BaseModel):
    action: str


@app.post('/api/printers/{printer_id}')
def printer_action(printer_id: str, payload: DeviceAction):
    try:
        with db() as c:
            device = devices.get(c, printer_id)
        attrs = Printer(device['uri'], timeout=8, lan_only=True).capabilities() if payload.action == 'refresh' else None
        with queue_lock, db() as c:
            if payload.action == 'refresh':
                devices.refresh(c, printer_id, attrs)
            elif payload.action == 'default':
                c.execute('UPDATE printers SET is_default=0')
                c.execute('UPDATE printers SET is_default=1 WHERE id=?', (printer_id,))
            elif payload.action == 'remove':
                devices.busy(c, printer_id)
                c.execute('UPDATE printers SET enabled=0,is_default=0 WHERE id=?', (printer_id,))
            else:
                raise ValueError('操作不正确')
        return {'ok': True}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(409, '能力刷新失败，请检查设备连接')


@app.get('/api/photos')
def photos(day: str = '', month: str = '', offset: int = 0, limit: int = 48):
    if offset < 0 or not 1 <= limit <= 96 or len(day) > 32 or len(month) > 16:
        raise HTTPException(400, '分页参数不正确')
    clause, args = ('WHERE day=?', [day]) if day else ('', [])
    if month and not day:
        clause, args = 'WHERE day LIKE ?', [month + '/%']
    with db() as c:
        count = c.execute('SELECT COUNT(*) FROM photos ' + clause, args).fetchone()[0]
        rows = c.execute('SELECT id,rel,day FROM photos ' + clause + ' ORDER BY day DESC,rel DESC LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
    return {'total': count, 'photos': [{'id': r['id'], 'day': r['day'], 'format': Path(r['rel']).suffix[1:].upper(), 'name': Path(r['rel']).name} for r in rows]}


@app.get('/api/thumb/{photo_id}')
def thumb(photo_id: str):
    row = get_photo(photo_id)
    key = hashlib.sha256(f"{row['id']}:{row['size']}:{row['mtime']}".encode()).hexdigest()
    target = DATA / 'thumbs' / (key + '.jpg')
    if not target.exists():
        with image_lock:
            if not target.exists():
                try:
                    target.write_bytes(thumbnail(photo_path(row)))
                except HTTPException:
                    raise
                except Exception:
                    raise HTTPException(422, '这张照片暂时无法生成预览')
    return FileResponse(target, media_type='image/jpeg')


@app.post('/api/refresh')
def refresh():
    threading.Thread(target=scan, daemon=True).start()
    return {'ok': True}


class Selection(BaseModel):
    id: str = Field(min_length=64, max_length=64, pattern=r'^[a-f0-9]+$')
    rotation: int = 0
    x: float = Field(default=.5, ge=0, le=1)
    y: float = Field(default=.5, ge=0, le=1)


class Prepare(BaseModel):
    photos: list[Selection] = Field(default_factory=list, max_length=10)
    printer_id: str | None = None
    media_id: str | None = None
    resolution_id: str | None = None
    custom_width_mm: float | None = Field(default=None, ge=10, le=1000)
    custom_height_mm: float | None = Field(default=None, ge=10, le=1000)
    fit: str = 'contain'
    layout: str = 'single'
    border_mm: float = Field(default=0, ge=0, le=20)
    gap_mm: float = Field(default=2, ge=0, le=20)
    photo_width_mm: float | None = Field(default=None, ge=5, le=1000)
    photo_height_mm: float | None = Field(default=None, ge=5, le=1000)
    repeat: bool = False
    calibration: bool = False
    background: str = '#ffffff'


@app.post('/api/prepare')
def prepare(payload: Prepare):
    if payload.fit not in ('contain', 'cover') or any(p.rotation not in (0, 90, 180, 270) for p in payload.photos):
        raise HTTPException(400, '排版参数不正确')
    if payload.background not in ('#ffffff', '#f4ead7', '#151515'):
        raise HTTPException(400, '请选择支持的底色')
    if not payload.calibration and not payload.photos:
        raise HTTPException(400, '请先选择照片')
    if len({p.id for p in payload.photos}) != len(payload.photos):
        raise HTTPException(400, '重复照片请使用铺满或份数设置')
    if payload.repeat and len(payload.photos) != 1:
        raise HTTPException(400, '重复铺满时请只选一张照片')
    try:
        with db() as c:
            device = devices.get(c, payload.printer_id)
        ticket = make_ticket(device, payload.media_id, payload.resolution_id, payload.custom_width_mm, payload.custom_height_mm)
        layout = 'single' if payload.calibration else payload.layout
        geometry = page_geometry(ticket, layout, payload.border_mm, payload.gap_mm, payload.photo_width_mm, payload.photo_height_mm)
        Printer(device['uri'], timeout=8, lan_only=True).validate_ticket(ticket)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(409, '设备未能确认这些参数，请检查连接、相纸和分辨率')
    prepared = []
    capacity = geometry['capacity']
    groups = [[]] if payload.calibration else [payload.photos[i:i + capacity] for i in range(0, len(payload.photos), capacity)]
    with image_lock:
        for group in groups:
            items, sources = [], []
            try:
                for item in group:
                    row = get_photo(item.id)
                    source = photo_path(row)
                    sha = file_hash(source)
                    items.append({'path': source, 'rotation': item.rotation, 'x': item.x, 'y': item.y})
                    sources.append({'photo_id': item.id, 'source_rel': row['rel'], 'source_sha256': sha})
                data, geometry, sizes = render_page(items, ticket, layout, payload.fit, payload.border_mm, payload.gap_mm, payload.background, payload.photo_width_mm, payload.photo_height_mm, payload.repeat, payload.calibration)
                if any(file_hash(item['path']) != source['source_sha256'] for item, source in zip(items, sources)):
                    raise HTTPException(409, '照片仍在上传或已被修改，请上传完成后重新预览')
            except HTTPException:
                raise
            except ValueError as e:
                raise HTTPException(422, str(e))
            except Exception:
                raise HTTPException(422, '照片无法转换，请上传完成或更换照片后重试')
            key = uuid.uuid4().hex
            (DATA / 'prepared' / (key + '.jpg')).write_bytes(data)
            source = sources[0] if sources else {}
            with db() as c:
                c.execute('INSERT INTO prepared(id,photo_id,landscape,fit,rotation,created,width,height,source_rel,source_sha256,paper,printer_id,ticket,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (key, source.get('photo_id'), 0, payload.fit, 0, time.time(), *geometry['pixels'], source.get('source_rel'), source.get('source_sha256'), ticket['media']['label'], device['id'], json.dumps(ticket), 'calibration' if payload.calibration else 'photo'))
                c.executemany('INSERT INTO prepared_sources VALUES(?,?,?,?)', [(key, r['photo_id'], r['source_rel'], r['source_sha256']) for r in sources])
            prepared.append({'id': key, 'photo_id': source.get('photo_id'), 'photo_ids': [r['photo_id'] for r in sources], 'landscape': False, 'source_size': sizes[0] if sizes else [], 'low_resolution': any(min(size) < 800 for size in sizes)})
    return {'prepared': prepared, 'paper': ticket['media']['label'], 'printer_id': device['id'], 'printer_name': device['name'], 'sheets': len(prepared), 'geometry': geometry, 'resolution': ticket['resolution']['label'], 'calibration': payload.calibration}


@app.get('/api/preview/{prepared_id}')
def preview(prepared_id: str):
    with db() as c:
        row = c.execute('SELECT id FROM prepared WHERE id=?', (prepared_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '预览已过期，请重新生成')
    return FileResponse(DATA / 'prepared' / (row['id'] + '.jpg'), media_type='image/jpeg')


class Submit(BaseModel):
    request_id: uuid.UUID
    printer_id: str | None = None
    prepared_ids: list[str] = Field(min_length=1, max_length=10)
    copies: int = Field(ge=1, le=5)
    confirmed: bool


@app.post('/api/print')
def submit(payload: Submit):
    if not payload.confirmed or len(payload.prepared_ids) * payload.copies > 10:
        raise HTTPException(400, '请确认预览，每批最多打印 10 张')
    submission_id = str(payload.request_id)
    canonical = json.dumps([payload.prepared_ids, payload.copies, payload.printer_id])
    with queue_lock, db() as c:
        c.execute('BEGIN IMMEDIATE')
        previous = c.execute('SELECT payload FROM submissions WHERE id=?', (submission_id,)).fetchone()
        if previous:
            if previous['payload'] != canonical:
                raise HTTPException(409, '请求编号重复，请重新打开预览')
            return {'id': submission_id, 'duplicate': True}
        if c.execute("SELECT 1 FROM jobs WHERE status IN ('pending','sending','submitted','unknown') LIMIT 1").fetchone():
            raise HTTPException(409, '已有打印任务，请先等待完成或核对未确认的任务')
        if len(set(payload.prepared_ids)) != len(payload.prepared_ids):
            raise HTTPException(400, '排版页不能重复，请使用份数选择')
        occupied_sources = set()
        for key in payload.prepared_ids:
            row = c.execute('SELECT * FROM prepared WHERE id=?', (key,)).fetchone()
            if row is None or time.time() - row['created'] > 3600:
                raise HTTPException(400, '预览已过期，请重新生成')
            try:
                if not row['ticket']:
                    raise ValueError('旧预览已过期，请重新排版')
                ticket = json.loads(row['ticket'])
                device = devices.assert_ticket(c, ticket)
                if payload.printer_id and device['id'] != payload.printer_id:
                    raise ValueError('所选打印机已变化，请重新预览')
                if not device['state'].get('online') or not device['state'].get('accepting') or device['state'].get('state') == 5:
                    raise ValueError('打印机未就绪，请确认开机后稍候')
            except ValueError as e:
                raise HTTPException(409, str(e))
            sources = c.execute('SELECT * FROM prepared_sources WHERE prepared_id=?', (key,)).fetchall()
            if not sources and row['kind'] != 'calibration':
                raise HTTPException(400, '请从待打印文件夹重新选择照片')
            for item in sources:
                if item['photo_id'] in occupied_sources:
                    raise HTTPException(409, '同一源照片不能出现在多份预览页中，请重新排版或使用份数')
                occupied_sources.add(item['photo_id'])
                source = ROOT / item['source_rel']
                if source.is_symlink() or not source.resolve().is_relative_to(ROOT) or not source.is_file() or file_hash(source) != item['source_sha256']:
                    raise HTTPException(409, '待打印照片已移动、已打印或被修改，请刷新列表后重新选片')
            if c.execute('SELECT 1 FROM archives WHERE prepared_id=?', (key,)).fetchone():
                raise HTTPException(409, '这份预览已打印，请刷新待打印列表')
        c.execute('INSERT INTO submissions VALUES(?,?,?)', (submission_id, time.time(), canonical))
        for key in payload.prepared_ids:
            for _ in range(payload.copies):
                now = time.time()
                c.execute('INSERT INTO jobs(id,submission,prepared_id,status,printer_job,message,created,updated,printer_id) VALUES(?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, submission_id, key, 'pending', None, '等待发送', now, now, c.execute('SELECT printer_id FROM prepared WHERE id=?', (key,)).fetchone()[0]))
    return {'id': submission_id, 'duplicate': False}


@app.get('/api/jobs')
def jobs():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT j.id,j.submission,j.status,j.message,j.created,j.updated,j.printer_id,COALESCE(d.name,'原打印机') AS printer_name FROM jobs j LEFT JOIN printers d ON d.id=j.printer_id ORDER BY j.created DESC,j.rowid DESC LIMIT 50")]


class Resolve(BaseModel):
    action: str


@app.post('/api/jobs/{job_id}/resolve')
def resolve(job_id: str, payload: Resolve):
    if payload.action not in ('acknowledge', 'cancel'):
        raise HTTPException(400, '操作不正确')
    with queue_lock, db() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '找不到任务')
        if payload.action == 'cancel' and row['status'] == 'pending':
            c.execute("UPDATE jobs SET status='cancelled',message='发送前已取消',updated=? WHERE id=?", (time.time(), job_id))
        elif payload.action == 'acknowledge' and row['status'] == 'unknown':
            # Cancel the rest of this batch, so acknowledgement cannot silently
            # resume printing paper the user may no longer expect.
            c.execute("UPDATE jobs SET status='acknowledged',message='用户已核对结果；未自动重发',updated=? WHERE id=?", (time.time(), job_id))
            c.execute("UPDATE jobs SET status='cancelled',message='本批因结果不确定而结束',updated=? WHERE submission=? AND status='pending'", (time.time(), row['submission']))
        else:
            raise HTTPException(409, '当前任务不能执行这项操作')
    return {'ok': True}


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')


@app.get('/app.js')
def javascript():
    return FileResponse(STATIC / 'app.js', media_type='text/javascript')


@app.get('/style.css')
def stylesheet():
    return FileResponse(STATIC / 'style.css', media_type='text/css')


@app.get('/icon.svg')
def icon():
    return FileResponse(STATIC / 'icon.svg', media_type='image/svg+xml')


@app.get('/manifest.webmanifest')
def manifest():
    return JSONResponse({'name': '纸间 Paperlane · NAS 照片打印', 'short_name': '纸间 Paperlane', 'start_url': '/', 'display': 'standalone', 'background_color': '#f4f2ec', 'theme_color': '#f4f2ec', 'icons': [{'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png'}, {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png'}]})


@app.get('/touch-icon.png')
def touch_icon():
    return FileResponse(STATIC / 'icon-192.png', media_type='image/png')


@app.get('/icon-{size}.png')
def png_icon(size: int):
    if size not in (192, 512):
        raise HTTPException(404)
    return FileResponse(STATIC / ('icon-' + str(size) + '.png'), media_type='image/png')
