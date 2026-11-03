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

from imaging import render_sheet, thumbnail, icon_png, PAPERS, LAYOUT_CAPACITY
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
queue_lock = threading.Lock()
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
    if not URI:
        printer_state = {'online': False, 'name': PRINTER_NAME, 'message': '尚未设置打印机地址，请配置 PRINTER_URI', 'checked': time.time()}
        return
    try:
        a = Printer(URI, timeout=6).capabilities()
        state = a.get('printer-state', [0])[0]
        printer_state = {'online': True, 'state': state, 'name': a.get('printer-name', [PRINTER_NAME])[0],
                         'message': {3: '空闲，可以打印', 4: '正在打印', 5: '打印机已暂停'}.get(state, '已连接'),
                         'reasons': a.get('printer-state-reasons', []), 'accepting': a.get('printer-is-accepting-jobs', [False])[0],
                         'queued': a.get('queued-job-count', [0])[0], 'checked': time.time()}
    except Exception:
        printer_state = {'online': False, 'name': PRINTER_NAME, 'message': '打印机未连接，请确认开机并连接家庭 Wi-Fi', 'checked': time.time()}


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
            job = c.execute("SELECT j.*,p.landscape,p.paper FROM jobs j JOIN prepared p ON p.id=j.prepared_id WHERE status='pending' ORDER BY j.created,j.rowid LIMIT 1").fetchone()
        if job is None or ambiguous:
            stop.wait(2)
            continue
        printer = Printer(URI, timeout=30)
        spec = PAPERS[job['paper']]
        try:
            data = (DATA / 'prepared' / (job['prepared_id'] + '.jpg')).read_bytes()
            caps = printer.capabilities()
            if not caps.get('printer-is-accepting-jobs', [False])[0] or caps.get('printer-state', [0])[0] == 5:
                set_job(job['id'], 'failed', '打印机未就绪，本张未发送')
                continue
            printer.validate(spec['media'], bool(job['landscape']), spec['hundredths_mm'])
        except Exception:
            set_job(job['id'], 'failed', '连接或格式验证失败，本张未发送，请检查打印机')
            continue
        with queue_lock, db() as c:
            claim = c.execute("UPDATE jobs SET status='sending',message='正在发送照片，请勿重复提交',updated=? WHERE id=? AND status='pending'", (time.time(), job['id']))
            if claim.rowcount != 1:
                continue
        try:
            answer = printer.print_jpeg(data, spec['media'], bool(job['landscape']), spec['hundredths_mm'])
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
            ready = [r[0] for r in c.execute("SELECT DISTINCT j.prepared_id FROM jobs j LEFT JOIN archives a ON a.prepared_id=j.prepared_id WHERE j.status='completed' AND (a.status IS NULL OR a.status<>'archived') AND NOT EXISTS (SELECT 1 FROM jobs k WHERE k.prepared_id=j.prepared_id AND k.status<>'completed')")]
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
    return {'version': '0.1.0', 'printer': printer_state, 'library': dict(scan_state, count=count, latest=latest, archived=archived)}


@app.get('/api/dates')
def dates():
    with db() as c:
        return [dict(row) for row in c.execute('SELECT day,COUNT(*) AS count FROM photos GROUP BY day ORDER BY day DESC')]


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
    photos: list[Selection] = Field(min_length=1, max_length=10)
    fit: str = 'contain'
    paper: str = '6inch'
    layout: str = 'single'
    border_mm: int = Field(default=0, ge=0, le=10)
    gap_mm: int = Field(default=2, ge=0, le=10)
    background: str = '#ffffff'


@app.post('/api/prepare')
def prepare(payload: Prepare):
    if payload.paper != '6inch':
        raise HTTPException(400, '当前仅使用 6 寸相纸；需要小尺寸照片时请选择 3 寸方形双拼排版后裁切')
    if payload.fit not in ('contain', 'cover') or any(p.rotation not in (0, 90, 180, 270) for p in payload.photos):
        raise HTTPException(400, '排版参数不正确')
    if payload.paper not in PAPERS or payload.layout not in LAYOUT_CAPACITY or payload.background not in ('#ffffff', '#f4ead7', '#151515'):
        raise HTTPException(400, '请选择支持的相纸和排版')
    if payload.layout == 'square3' and payload.paper != '6inch':
        raise HTTPException(400, '3 寸方形双拼需要使用 6 寸相纸')
    if len({p.id for p in payload.photos}) != len(payload.photos):
        raise HTTPException(400, '同一张照片请通过份数选择打印数量')
    prepared = []
    capacity = LAYOUT_CAPACITY[payload.layout]
    with image_lock:
        for start in range(0, len(payload.photos), capacity):
            group = payload.photos[start:start + capacity]
            items, sources = [], []
            try:
                for item in group:
                    row = get_photo(item.id)
                    source = photo_path(row)
                    sha = file_hash(source)
                    items.append({'path': source, 'rotation': item.rotation, 'x': item.x, 'y': item.y})
                    sources.append({'photo_id': item.id, 'source_rel': row['rel'], 'source_sha256': sha})
                data, landscape, sizes = render_sheet(items, payload.paper, payload.layout, payload.fit, payload.border_mm, payload.gap_mm, payload.background)
                if any(file_hash(item['path']) != source['source_sha256'] for item, source in zip(items, sources)):
                    raise HTTPException(409, '照片仍在上传或已被修改，请上传完成后再生成预览')
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(422, '有照片无法转换，请上传完成或更换照片后重试')
            key = uuid.uuid4().hex
            (DATA / 'prepared' / (key + '.jpg')).write_bytes(data)
            first = sources[0]
            with db() as c:
                c.execute('INSERT INTO prepared(id,photo_id,landscape,fit,rotation,created,width,height,source_rel,source_sha256,paper) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (key, first['photo_id'], int(landscape), payload.fit, group[0].rotation, time.time(), sizes[0][0], sizes[0][1], first['source_rel'], first['source_sha256'], payload.paper))
                c.executemany('INSERT INTO prepared_sources VALUES(?,?,?,?)', [(key, r['photo_id'], r['source_rel'], r['source_sha256']) for r in sources])
            prepared.append({'id': key, 'photo_id': first['photo_id'], 'photo_ids': [r['photo_id'] for r in sources], 'landscape': landscape, 'source_size': sizes[0], 'low_resolution': any(min(size) < 800 for size in sizes)})
    return {'prepared': prepared, 'paper': PAPERS[payload.paper]['label'], 'sheets': len(prepared)}


@app.get('/api/preview/{prepared_id}')
def preview(prepared_id: str):
    with db() as c:
        row = c.execute('SELECT id FROM prepared WHERE id=?', (prepared_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '预览已过期，请重新生成')
    return FileResponse(DATA / 'prepared' / (row['id'] + '.jpg'), media_type='image/jpeg')


class Submit(BaseModel):
    request_id: uuid.UUID
    prepared_ids: list[str] = Field(min_length=1, max_length=10)
    copies: int = Field(ge=1, le=5)
    confirmed: bool


@app.post('/api/print')
def submit(payload: Submit):
    if not payload.confirmed or len(payload.prepared_ids) * payload.copies > 10:
        raise HTTPException(400, '请确认预览，每批最多打印 10 张')
    submission_id = str(payload.request_id)
    canonical = json.dumps([payload.prepared_ids, payload.copies])
    with queue_lock, db() as c:
        c.execute('BEGIN IMMEDIATE')
        previous = c.execute('SELECT payload FROM submissions WHERE id=?', (submission_id,)).fetchone()
        if previous:
            if previous['payload'] != canonical:
                raise HTTPException(409, '请求编号重复，请重新打开预览')
            return {'id': submission_id, 'duplicate': True}
        if c.execute("SELECT 1 FROM jobs WHERE status IN ('pending','sending','submitted','unknown') LIMIT 1").fetchone():
            raise HTTPException(409, '已有打印任务，请先等待完成或核对未确认的任务')
        if not printer_state.get('online') or not printer_state.get('accepting'):
            raise HTTPException(409, '打印机未就绪，请确认开机后稍候')
        if len(set(payload.prepared_ids)) != len(payload.prepared_ids):
            raise HTTPException(400, '排版页不能重复，请使用份数选择')
        for key in payload.prepared_ids:
            row = c.execute('SELECT * FROM prepared WHERE id=?', (key,)).fetchone()
            if row is None or time.time() - row['created'] > 3600:
                raise HTTPException(400, '预览已过期，请重新生成')
            sources = c.execute('SELECT * FROM prepared_sources WHERE prepared_id=?', (key,)).fetchall()
            if not sources:
                raise HTTPException(400, '请从待打印文件夹重新选择照片')
            for item in sources:
                source = ROOT / item['source_rel']
                if source.is_symlink() or not source.resolve().is_relative_to(ROOT) or not source.is_file() or file_hash(source) != item['source_sha256']:
                    raise HTTPException(409, '待打印照片已移动、已打印或被修改，请刷新列表后重新选片')
            if c.execute('SELECT 1 FROM archives WHERE prepared_id=?', (key,)).fetchone():
                raise HTTPException(409, '这份预览已打印，请刷新待打印列表')
        c.execute('INSERT INTO submissions VALUES(?,?,?)', (submission_id, time.time(), canonical))
        for key in payload.prepared_ids:
            for _ in range(payload.copies):
                now = time.time()
                c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, submission_id, key, 'pending', None, '等待发送', now, now,))
    return {'id': submission_id, 'duplicate': False}


@app.get('/api/jobs')
def jobs():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT id,submission,status,message,created,updated FROM jobs ORDER BY created DESC,rowid DESC LIMIT 50')]


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
    return JSONResponse({'name': '拾光印 · NAS 照片打印', 'short_name': '拾光印', 'start_url': '/', 'display': 'standalone', 'background_color': '#f4f2ec', 'theme_color': '#f4f2ec', 'icons': [{'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png'}, {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png'}]})


@app.get('/touch-icon.png')
def touch_icon():
    return Response(icon_png(180), media_type='image/png')


@app.get('/icon-{size}.png')
def png_icon(size: int):
    if size not in (192, 512):
        raise HTTPException(404)
    return Response(icon_png(size), media_type='image/png')
