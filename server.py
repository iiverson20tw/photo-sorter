# -*- coding: utf-8 -*-
"""
照片排序器 — 共用相簿（相簿版）後端
資料模型 data.json:
{
  "albums": { aid: {"id","name","order":[photoId,...],"created"} },
  "albumOrder": [aid,...],
  "items": { photoId: {"name","file"} }
}
照片存 uploads/。上傳走 raw body（X-Filename header）。iPhone HEIC 自動轉 JPEG。
用法：python server.py [port]   預設 8090
"""
import sys, os, json, threading, io, mimetypes, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote
import zipfile, tempfile
import hmac, hashlib, secrets, time, http.cookies

ROOT = os.path.dirname(os.path.abspath(__file__))
UP   = os.path.join(ROOT, 'uploads')
THUMBS = os.path.join(UP, 'thumbs')
DATA = os.path.join(ROOT, 'data.json')
os.makedirs(UP, exist_ok=True)
os.makedirs(THUMBS, exist_ok=True)
THUMB_MAX = 420
LOCK = threading.Lock()
MAXBODY = 40 * 1024 * 1024

# ===== 密碼保護 =====
# PIN 與簽章密鑰都存在「不進 git 的檔案」裡（repo 是 public，寫死在原始碼等於公開密碼）
PIN_FILE    = os.path.join(ROOT, '.auth_pin')
SECRET_FILE = os.path.join(ROOT, '.auth_secret')
COOKIE   = 'ps_auth'
MAX_AGE  = 30 * 24 * 3600          # 記住登入 30 天
FAIL_MAX = 8                       # 同一 IP 連錯 8 次
FAIL_LOCK = 300                    # 鎖 5 分鐘
_FAILS = {}                        # ip -> [錯誤次數, 解鎖時間]
_FAIL_LOCK = threading.Lock()


def _read_or_create(path, default_factory):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            v = f.read().strip()
        if v:
            return v
    except Exception:
        pass
    v = default_factory()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(v)
    except Exception:
        pass
    return v


PIN    = os.environ.get('PHOTO_PIN') or _read_or_create(PIN_FILE, lambda: '2632')
SECRET = _read_or_create(SECRET_FILE, lambda: secrets.token_hex(32)).encode('utf-8')


def _token():
    """cookie 值＝用密鑰簽出來的固定 token，別人猜不到也偽造不了。"""
    return hmac.new(SECRET, b'ps_auth_v1', hashlib.sha256).hexdigest()


def _fail_state(ip):
    """回傳 (是否鎖定中, 還要等幾秒)。"""
    with _FAIL_LOCK:
        st = _FAILS.get(ip)
        if not st:
            return False, 0
        if st[1] > time.time():
            return True, int(st[1] - time.time())
        if st[1]:                      # 鎖定已過期，重新計數
            _FAILS.pop(ip, None)
        return False, 0


def _fail_add(ip):
    with _FAIL_LOCK:
        st = _FAILS.setdefault(ip, [0, 0])
        st[0] += 1
        if st[0] >= FAIL_MAX:
            st[1] = time.time() + FAIL_LOCK
            st[0] = 0


def _fail_clear(ip):
    with _FAIL_LOCK:
        _FAILS.pop(ip, None)


LOGIN_PAGE = """<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>我的相簿 · 請輸入密碼</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100dvh;display:flex;align-items:center;justify-content:center;
 background:#0f1020;font-family:"Microsoft JhengHei","PingFang TC",system-ui,sans-serif;padding:24px}
.box{width:100%;max-width:340px;text-align:center}
.logo{font-size:2.6rem}
h1{color:#fff;font-size:1.15rem;margin:12px 0 4px;font-weight:800}
p{color:#8b8fb5;font-size:.82rem;margin-bottom:22px}
input{width:100%;padding:15px;border-radius:12px;border:1px solid #2b2d52;background:#181a33;
 color:#fff;font-size:1.5rem;text-align:center;letter-spacing:.7rem;outline:none;font-family:inherit}
input:focus{border-color:#5b4bdb}
button{width:100%;margin-top:12px;padding:14px;border:none;border-radius:12px;
 background:#5b4bdb;color:#fff;font-size:1rem;font-weight:700;cursor:pointer;font-family:inherit}
button:disabled{opacity:.5}
.err{color:#ff7a90;font-size:.85rem;margin-top:14px;min-height:1.2em}
</style></head><body>
<div class="box">
  <div class="logo">📚</div>
  <h1>我的相簿</h1>
  <p>請輸入密碼</p>
  <form id="f">
    <input id="pin" type="password" inputmode="numeric" autocomplete="current-password"
           placeholder="••••" autofocus>
    <button type="submit">進入相簿</button>
  </form>
  <div class="err" id="err"></div>
</div>
<script>
var f=document.getElementById('f'),pin=document.getElementById('pin'),err=document.getElementById('err');
f.onsubmit=function(e){
  e.preventDefault(); err.textContent='';
  fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pin:pin.value})})
   .then(function(r){return r.json()})
   .then(function(j){
     if(j.ok){ location.replace('/'); }
     else if(j.locked){ err.textContent='錯太多次了，請 '+j.wait+' 秒後再試'; }
     else { err.textContent='密碼不對'; pin.value=''; pin.focus(); }
   })
   .catch(function(){ err.textContent='連線失敗'; });
};
</script></body></html>"""

try:
    import pillow_heif; from PIL import Image
    pillow_heif.register_heif_opener(); HEIF = True
except Exception:
    try:
        from PIL import Image; HEIF = False
    except Exception:
        Image = None; HEIF = False

def _new_id(): return uuid.uuid4().hex

def _normalize(d):
    """容錯 + 舊(扁平)結構自動遷移成相簿結構。"""
    if not isinstance(d, dict): d = {}
    items = d.get('items', {}) if isinstance(d.get('items'), dict) else {}
    if 'albums' not in d:
        # 舊扁平: {items, order} → 包成一個相簿；但完全空的新安裝就給空清單（不要生出幽靈相簿）
        old_order = [i for i in d.get('order', []) if i in items]
        if items:
            aid = _new_id()
            d = {"albums": {aid: {"id": aid, "name": "相簿", "order": old_order, "created": 0}},
                 "albumOrder": [aid], "items": items}
        else:
            d = {"albums": {}, "albumOrder": [], "items": {}}
    d.setdefault('albums', {}); d.setdefault('albumOrder', []); d.setdefault('items', {})
    # 清理：albumOrder 只留存在的相簿；每個相簿 order 只留存在的照片
    d['albumOrder'] = [a for a in d['albumOrder'] if a in d['albums']]
    for a in d['albums']:
        if a not in d['albumOrder']: d['albumOrder'].append(a)
    for a, al in d['albums'].items():
        al['order'] = [p for p in al.get('order', []) if p in d['items']]
    return d

def _load():
    raw = None
    if os.path.exists(DATA):
        try:
            with open(DATA, 'r', encoding='utf-8') as f: raw = json.load(f)
        except Exception: raw = None
    d = _normalize(raw if isinstance(raw, dict) else {})
    # 舊(扁平)結構第一次讀到就遷移並存起來（固定 aid，避免每次讀都生新 id）
    if isinstance(raw, dict) and 'albums' not in raw and raw.get('items'):
        try: _save(d)
        except Exception: pass
    return d

def _save(d):
    tmp = DATA + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, DATA)

def _cover(d, al):
    for pid in al.get('order', []):
        it = d['items'].get(pid)
        if it: return '/thumb/' + it['file']
    return None

def _albums_payload(d):
    out = []
    for aid in d['albumOrder']:
        al = d['albums'].get(aid)
        if not al: continue
        out.append({"id": aid, "name": al['name'], "count": len(al['order']), "cover": _cover(d, al)})
    return out

def _photos_payload(d, aid):
    al = d['albums'].get(aid)
    if not al: return None
    out = []
    for i, pid in enumerate(al['order']):
        it = d['items'].get(pid)
        if not it: continue
        out.append({"id": pid, "name": it['name'], "url": '/uploads/' + it['file'],
                    "thumb": '/thumb/' + it['file'], "taken": it.get('taken'), "pos": i + 1})
    return out

# ── EXIF 拍攝時間；沒有就回 None（iOS 網頁上傳常把時間戳拿掉）──
def _capture_time(fp):
    if not Image: return None
    try:
        im = Image.open(fp); ex = im.getexif()
        dt = None
        try: dt = ex.get_ifd(0x8769).get(36867)   # DateTimeOriginal
        except Exception: pass
        if not dt: dt = ex.get(306)                 # DateTime
        if dt and isinstance(dt, str):
            import datetime
            return datetime.datetime.strptime(dt.strip(), "%Y:%m:%d %H:%M:%S")
    except Exception: pass
    return None

def _natkey(s):
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s or '')]

def _detect_ext(raw, filename):
    lower = (filename or '').lower(); head = raw[:16]
    is_heic = lower.endswith(('.heic', '.heif')) or (head[4:8] == b'ftyp' and head[8:12] in (b'heic', b'heix', b'mif1', b'hevc', b'msf1'))
    if is_heic and Image and HEIF:
        try:
            im = Image.open(io.BytesIO(raw))
            if im.mode not in ('RGB', 'L'): im = im.convert('RGB')
            buf = io.BytesIO(); im.save(buf, 'JPEG', quality=90); return buf.getvalue(), 'jpg'
        except Exception: pass
    if head.startswith(b'\x89PNG'): return raw, 'png'
    if head[:3] == b'\xff\xd8\xff': return raw, 'jpg'
    if head[:6] in (b'GIF87a', b'GIF89a'): return raw, 'gif'
    if head[:4] == b'RIFF' and raw[8:12] == b'WEBP': return raw, 'webp'
    for e in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
        if lower.endswith('.' + e): return raw, ('jpg' if e == 'jpeg' else e)
    return raw, 'jpg'

class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'   # keep-alive：瀏覽器重用連線，避免大量縮圖時連線爆量造成 502
    timeout = 20
    def log_message(self, *a): pass
    def _send(self, code, body=b'', ctype='application/json; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'X-Filename, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Cache-Control', 'no-store')
        if extra:
            for k, v in extra.items(): self.send_header(k, v)
        self.end_headers()
        if body: self.wfile.write(body)
    def _json(self, obj, code=200): self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'))
    def do_OPTIONS(self): self._send(204)

    def _read_body(self):
        n = int(self.headers.get('Content-Length', 0))
        if n <= 0 or n > MAXBODY: return None
        buf = b''
        while len(buf) < n:
            chunk = self.rfile.read(min(65536, n - len(buf)))
            if not chunk: break
            buf += chunk
        return buf

    # ---- 密碼守門 ----
    def _client_ip(self):
        return (self.headers.get('CF-Connecting-IP')
                or self.headers.get('X-Forwarded-For', '').split(',')[0].strip()
                or self.client_address[0])

    def _authed(self):
        raw = self.headers.get('Cookie')
        if not raw:
            return False
        try:
            c = http.cookies.SimpleCookie(raw)
        except Exception:
            return False
        m = c.get(COOKIE)
        return bool(m) and hmac.compare_digest(m.value, _token())

    def _gate(self, path):
        """未登入就擋下來。回 True 表示已處理（呼叫端要 return）。"""
        if path in ('/login', '/api/login') or self._authed():
            return False
        if path in ('/', '/index.html', '/login'):
            self._send(200, LOGIN_PAGE.encode('utf-8'), 'text/html; charset=utf-8')
        else:
            self._json({"error": "unauthorized"}, 401)
        return True

    def do_GET(self):
        u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
        if self._gate(path):
            return
        if path == '/login':
            return self._send(200, LOGIN_PAGE.encode('utf-8'), 'text/html; charset=utf-8')
        if path in ('/', '/index.html'):
            try:
                with open(os.path.join(ROOT, 'index.html'), 'rb') as f: b = f.read()
                return self._send(200, b, 'text/html; charset=utf-8')
            except Exception:
                return self._send(500, b'index.html missing', 'text/plain; charset=utf-8')
        if path == '/api/albums':
            with LOCK: d = _load()
            return self._json({"albums": _albums_payload(d)})
        if path == '/api/list':
            aid = q.get('album', [''])[0]
            with LOCK: d = _load()
            ph = _photos_payload(d, aid)
            if ph is None: return self._json({"error": "no such album"}, 404)
            al = d['albums'][aid]
            return self._json({"album": {"id": aid, "name": al['name']}, "photos": ph})
        if path == '/api/download':
            aid = q.get('album', [''])[0]
            with LOCK: d = _load(); al = d['albums'].get(aid)
            if not al: return self._send(404, b'no album', 'text/plain')
            order = list(al['order']); items = dict(d['items']); name = al['name']
            width = max(3, len(str(len(order))))
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip'); tmp.close()
            try:
                with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_STORED) as z:
                    for i, pid in enumerate(order):
                        it = items.get(pid)
                        if not it: continue
                        src = os.path.join(UP, it['file'])
                        if not os.path.isfile(src): continue
                        base = os.path.basename(it.get('name') or it['file']) or it['file']
                        z.write(src, str(i + 1).zfill(width) + '_' + base)   # 前綴編號＝排好的順序
                size = os.path.getsize(tmp.name)
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Length', str(size))
                fn = quote(((name or 'album') + '.zip'))
                self.send_header('Content-Disposition', "attachment; filename=\"album.zip\"; filename*=UTF-8''" + fn)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                with open(tmp.name, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk: break
                        self.wfile.write(chunk)
            finally:
                try: os.remove(tmp.name)
                except Exception: pass
            return
        if path.startswith('/thumb/'):
            fn = os.path.basename(unquote(path[len('/thumb/'):]))
            src = os.path.join(UP, fn)
            if not os.path.isfile(src): return self._send(404, b'not found', 'text/plain')
            tp = os.path.join(THUMBS, fn + '.jpg')
            if not os.path.isfile(tp) and Image:
                try:
                    im = Image.open(src)
                    try: im.draft('RGB', (THUMB_MAX*2, THUMB_MAX*2))
                    except Exception: pass
                    im = im.convert('RGB') if im.mode not in ('RGB', 'L') else im
                    im.thumbnail((THUMB_MAX, THUMB_MAX))
                    tmp = tp + '.tmp'; im.save(tmp, 'JPEG', quality=80); os.replace(tmp, tp)
                except Exception:
                    tp = None
            if tp and os.path.isfile(tp):
                with open(tp, 'rb') as f: b = f.read()
                return self._send(200, b, 'image/jpeg', {'Cache-Control': 'public, max-age=31536000'})
            # 無法產生縮圖→退回原圖
            ctype = mimetypes.guess_type(src)[0] or 'application/octet-stream'
            with open(src, 'rb') as f: b = f.read()
            return self._send(200, b, ctype, {'Cache-Control': 'public, max-age=31536000'})
        if path.startswith('/uploads/'):
            fn = os.path.basename(unquote(path[len('/uploads/'):]))
            fp = os.path.join(UP, fn)
            if os.path.isfile(fp):
                ctype = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
                with open(fp, 'rb') as f: b = f.read()
                return self._send(200, b, ctype, {'Cache-Control': 'public, max-age=31536000'})
            return self._send(404, b'not found', 'text/plain')
        return self._send(404, b'not found', 'text/plain')

    def do_POST(self):
        u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
        if path == '/api/login':
            ip = self._client_ip()
            locked, wait = _fail_state(ip)
            if locked:
                return self._json({"ok": False, "locked": True, "wait": wait}, 429)
            try:
                pin = str((json.loads(self._read_body() or b'{}') or {}).get('pin', ''))
            except Exception:
                pin = ''
            if hmac.compare_digest(pin, PIN):
                _fail_clear(ip)
                return self._send(200, b'{"ok":true}',
                                  extra={'Set-Cookie':
                                         '%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax'
                                         % (COOKIE, _token(), MAX_AGE)})
            _fail_add(ip)
            return self._json({"ok": False}, 401)
        if self._gate(path):
            return

        if path == '/api/album/create':
            raw = self._read_body() or b'{}'
            try: name = (json.loads(raw.decode('utf-8')).get('name') or '').strip()
            except Exception: name = ''
            if not name: name = '未命名相簿'
            aid = _new_id()
            with LOCK:
                d = _load(); d['albums'][aid] = {"id": aid, "name": name[:60], "order": [], "created": 0}
                d['albumOrder'].append(aid); _save(d)
            return self._json({"id": aid, "name": name[:60], "count": 0, "cover": None})

        if path == '/api/album/rename':
            aid = q.get('id', [''])[0]; raw = self._read_body() or b'{}'
            try: name = (json.loads(raw.decode('utf-8')).get('name') or '').strip()
            except Exception: name = ''
            with LOCK:
                d = _load(); al = d['albums'].get(aid)
                if not al: return self._json({"error": "no album"}, 404)
                if name: al['name'] = name[:60]
                _save(d)
            return self._json({"ok": True})

        if path == '/api/album/delete':
            aid = q.get('id', [''])[0]; files = []
            with LOCK:
                d = _load(); al = d['albums'].pop(aid, None)
                if aid in d['albumOrder']: d['albumOrder'].remove(aid)
                if al:
                    for pid in al['order']:
                        it = d['items'].pop(pid, None)
                        if it: files.append(it['file'])
                _save(d)
            for fn in files:
                try: os.remove(os.path.join(UP, fn))
                except Exception: pass
            return self._json({"ok": True})

        if path == '/api/album/order':
            raw = self._read_body() or b'[]'
            try: ids = json.loads(raw.decode('utf-8'))
            except Exception: return self._json({"error": "bad json"}, 400)
            with LOCK:
                d = _load(); known = set(d['albums'].keys())
                new = [i for i in ids if i in known]
                for i in d['albumOrder']:
                    if i not in new: new.append(i)
                d['albumOrder'] = new; _save(d)
            return self._json({"ok": True})

        if path == '/api/upload':
            aid = q.get('album', [''])[0]
            raw = self._read_body()
            if raw is None: return self._json({"error": "空的或太大（單張上限 40MB）"}, 400)
            fname = unquote(self.headers.get('X-Filename', '') or (q.get('name', [''])[0]))
            data, ext = _detect_ext(raw, fname)
            pid = _new_id(); file = pid + '.' + ext
            with LOCK:
                d = _load()
                if aid not in d['albums']: return self._json({"error": "no album"}, 404)
                with open(os.path.join(UP, file), 'wb') as f: f.write(data)
                disp = fname or ('photo.' + ext)
                # 拍攝時間：優先 EXIF；沒有就用瀏覽器送來的檔案日期(File.lastModified)
                ct = _capture_time(os.path.join(UP, file))
                taken = ct.timestamp() if ct else None
                if taken is None:
                    try:
                        v = float(self.headers.get('X-Taken', '') or 0) / 1000.0
                        if v > 946684800: taken = v      # 2000 年後才視為合理日期
                    except Exception: pass
                d['items'][pid] = {"name": disp, "file": file, "taken": taken}
                d['albums'][aid]['order'].append(pid); _save(d)
            return self._json({"id": pid, "url": '/uploads/' + file, "name": disp})

        if path == '/api/order':
            aid = q.get('album', [''])[0]; raw = self._read_body() or b'[]'
            try: ids = json.loads(raw.decode('utf-8'))
            except Exception: return self._json({"error": "bad json"}, 400)
            with LOCK:
                d = _load(); al = d['albums'].get(aid)
                if not al: return self._json({"error": "no album"}, 404)
                cur = set(al['order'])
                new = [i for i in ids if i in cur]
                for i in al['order']:
                    if i not in new: new.append(i)   # 保底：漏掉的補回，絕不掉照片
                al['order'] = new; _save(d)
            return self._json({"ok": True})

        if path == '/api/delete':
            pid = q.get('id', [''])[0]; aid = q.get('album', [''])[0]; fn = None
            with LOCK:
                d = _load(); al = d['albums'].get(aid)
                if al and pid in al['order']: al['order'].remove(pid)
                it = d['items'].pop(pid, None)
                if it: fn = it['file']
                _save(d)
            if fn:
                try: os.remove(os.path.join(UP, fn))
                except Exception: pass
            return self._json({"ok": True})

        if path == '/api/sort_by_time':
            aid = q.get('album', [''])[0]
            with LOCK:
                d = _load(); al = d['albums'].get(aid)
                if not al: return self._json({"error": "no album"}, 404)
                ids = list(al['order'])
                # 每張的拍攝時間：優先用上傳時存的 taken(EXIF 或檔案日期)，沒有再即時讀 EXIF
                times = {}
                for pid in ids:
                    it = d['items'].get(pid) or {}
                    t = it.get('taken')
                    if t is None:
                        ct = _capture_time(os.path.join(UP, it['file'])) if it.get('file') else None
                        t = ct.timestamp() if ct else None
                    times[pid] = t
                if ids and all(times[i] is not None for i in ids):
                    ids.sort(key=lambda i: times[i]); method = 'time'
                else:
                    ids.sort(key=lambda i: _natkey(d['items'].get(i, {}).get('name', ''))); method = 'filename'
                al['order'] = ids; _save(d)
            return self._json({"ok": True, "method": method, "count": len(ids)})

        if path == '/api/delete_many':
            aid = q.get('album', [''])[0]; raw = self._read_body() or b'[]'
            try: ids = json.loads(raw.decode('utf-8'))
            except Exception: return self._json({"error": "bad json"}, 400)
            files = []
            with LOCK:
                d = _load(); al = d['albums'].get(aid)
                if not al: return self._json({"error": "no album"}, 404)
                idset = set(ids)
                al['order'] = [p for p in al['order'] if p not in idset]
                for pid in ids:
                    it = d['items'].pop(pid, None)
                    if it: files.append(it['file'])
                _save(d)
            for fn in files:
                for pth in (os.path.join(UP, fn), os.path.join(THUMBS, fn + '.jpg')):
                    try: os.remove(pth)
                    except Exception: pass
            return self._json({"ok": True, "deleted": len(files)})

        if path == '/api/clear':
            aid = q.get('album', [''])[0]; files = []
            with LOCK:
                d = _load(); al = d['albums'].get(aid)
                if not al: return self._json({"error": "no album"}, 404)
                for pid in al['order']:
                    it = d['items'].pop(pid, None)
                    if it: files.append(it['file'])
                al['order'] = []; _save(d)
            for fn in files:
                try: os.remove(os.path.join(UP, fn))
                except Exception: pass
            return self._json({"ok": True})

        return self._send(404, b'not found', 'text/plain')

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    print('photo-sorter (albums) on http://127.0.0.1:%d  HEIF=%s' % (port, HEIF), flush=True)
    srv.serve_forever()
