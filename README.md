# 照片排序器 · 共用相簿 (photo-sorter)

手機上傳照片 → 拖曳調整順序 → 顯示編號。**共用版**：照片存在自己電腦（伺服器），
別人開同一個網址就看得到、也能一起上傳與排序（自動同步）。

## 架構
- `server.py` — 純 Python 標準庫後端。照片存 `uploads/`、順序與中繼資料存 `data.json`。
  上傳走 raw body（`X-Filename` header），iPhone HEIC 自動轉 JPEG（需 `pillow-heif`）。
- `index.html` — 前端（共用版，透過 `/api/*` 讀寫、每 4 秒輪詢同步）。
- 對外：Cloudflare quick tunnel（`cloudflared`）。

## 啟動（Windows）
雙擊 **`start.bat`**：會啟動 server（port 8090）+ cloudflared，並印出對外網址。
把那個 `https://xxx.trycloudflare.com` 網址分享給別人即可。

> 注意：quick tunnel 網址每次重啟會變；重跑 `start.bat` 取得新網址。
> 電腦關機 / 這兩個背景程式關掉，網址就失效。

手動啟動：
```
pip install pillow-heif        # 一次即可（HEIC 轉檔）
python server.py 8090
cloudflared tunnel --url http://127.0.0.1:8090
```

## API
- `GET  /api/list` → `{photos:[{id,name,url,pos}]}`
- `POST /api/upload` （body=raw 圖片 bytes，header `X-Filename`）
- `POST /api/order` （body=JSON id 陣列）
- `POST /api/delete?id=`
- `POST /api/clear`

## 隱私
照片只存在這台電腦（`uploads/`），不會上傳到 GitHub（已 gitignore）。
只有拿到 tunnel 網址的人能存取。
