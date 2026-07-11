# -*- coding: utf-8 -*-
"""
照片排序器 — 共用相簿後端（用戶自己電腦當伺服器）
純標準庫 http.server；照片存 uploads/、順序/中繼資料存 data.json。
上傳走 raw body（X-Filename header），避開 multipart 解析。
iPhone HEIC 自動轉 JPEG（pillow-heif），確保別人裝置也看得到。
用法：python server.py [port]   預設 8090
"""
import sys, os, json, threading, io, mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
UP   = os.path.join(ROOT, 'uploads')
DATA = os.path.join(ROOT, 'data.json')
os.makedirs(UP, exist_ok=True)
LOCK = threading.Lock()
MAXBODY = 40 * 1024 * 1024   # 單張上限 40MB

# 選用：HEIC/HEIF 轉 JPEG
try:
    import pillow_heif; from PIL import Image
    pillow_heif.register_heif_opener(); HEIF = True
except Exception:
    try:
        from PIL import Image; HEIF = False
    except Exception:
        Image = None; HEIF = False

def _load():
    if os.path.exists(DATA):
        try:
            with open(DATA, 'r', encoding='utf-8') as f: return json.load(f)
        except Exception: pass
    return {"items": {}, "order": []}

def _save(d):
    tmp = DATA + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, DATA)

def _list_payload(d):
    out = []
    for i, pid in enumerate(d["order"]):
        it = d["items"].get(pid)
        if not it: continue
        out.append({"id": pid, "name": it["name"], "url": "/uploads/" + it["file"], "pos": i + 1})
    return out

def _new_id():
    # 不依賴 uuid 也行，但 uuid4 最穩
    import uuid; return uuid.uuid4().hex

def _detect_ext(raw, filename):
    lower = (filename or '').lower()
    head = raw[:16]
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

    def do_GET(self):
        u = urlparse(self.path); path = u.path
        if path == '/' or path == '/index.html':
            try:
                with open(os.path.join(ROOT, 'index.html'), 'rb') as f: b = f.read()
                return self._send(200, b, 'text/html; charset=utf-8')
            except Exception:
                return self._send(500, b'index.html missing', 'text/plain; charset=utf-8')
        if path == '/api/list':
            with LOCK: d = _load()
            return self._json({"photos": _list_payload(d)})
        if path.startswith('/uploads/'):
            fn = os.path.basename(unquote(path[len('/uploads/'):]))
            fp = os.path.join(UP, fn)
            if os.path.isfile(fp):
                ctype = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
                with open(fp, 'rb') as f: b = f.read()
                return self._send(200, b, ctype, {'Cache-Control': 'public, max-age=31536000'})
            return self._send(404, b'not found', 'text/plain')
        return self._send(404, b'not found', 'text/plain')

    def _read_body(self):
        n = int(self.headers.get('Content-Length', 0))
        if n <= 0 or n > MAXBODY: return None
        buf = b''
        while len(buf) < n:
            chunk = self.rfile.read(min(65536, n - len(buf)))
            if not chunk: break
            buf += chunk
        return buf

    def do_POST(self):
        u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
        if path == '/api/upload':
            raw = self._read_body()
            if raw is None: return self._json({"error": "空的或太大（單張上限 40MB）"}, 400)
            fname = unquote(self.headers.get('X-Filename', '') or (q.get('name', [''])[0]))
            data, ext = _detect_ext(raw, fname)
            pid = _new_id(); file = pid + '.' + ext
            with open(os.path.join(UP, file), 'wb') as f: f.write(data)
            disp = fname or ('photo.' + ext)
            with LOCK:
                d = _load(); d["items"][pid] = {"name": disp, "file": file}; d["order"].append(pid); _save(d)
            return self._json({"id": pid, "url": "/uploads/" + file, "name": disp})
        if path == '/api/order':
            raw = self._read_body() or b'[]'
            try: ids = json.loads(raw.decode('utf-8'))
            except Exception: return self._json({"error": "bad json"}, 400)
            with LOCK:
                d = _load(); known = set(d["items"].keys())
                new = [i for i in ids if i in known]
                for i in d["order"]:
                    if i not in new: new.append(i)   # 保底：沒被列到的補回
                d["order"] = new; _save(d)
            return self._json({"ok": True})
        if path == '/api/delete':
            pid = q.get('id', [''])[0]
            with LOCK:
                d = _load(); it = d["items"].pop(pid, None)
                if pid in d["order"]: d["order"].remove(pid)
                _save(d)
            if it:
                try: os.remove(os.path.join(UP, it["file"]))
                except Exception: pass
            return self._json({"ok": True})
        if path == '/api/clear':
            with LOCK:
                d = _load(); files = [it["file"] for it in d["items"].values()]
                d = {"items": {}, "order": []}; _save(d)
            for fn in files:
                try: os.remove(os.path.join(UP, fn))
                except Exception: pass
            return self._json({"ok": True})
        return self._send(404, b'not found', 'text/plain')

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    print('photo-sorter server on http://127.0.0.1:%d  (HEIF=%s)' % (port, HEIF), flush=True)
    srv.serve_forever()
