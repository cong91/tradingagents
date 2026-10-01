---
title: Báo cáo triển khai Control-Panel UI (web/)
updated: 2026-09-30 (rev 2: áp góp ý đọc thử — đếm file, .gitignore mới, số pytest thực chạy, mục Bảo mật / Vòng đời state / Việc còn mở)
source: docs/ui-api-contract.md (rev 2) + server/ + web/ + tests/test_server_api.py
---

# Báo cáo triển khai Control-Panel UI

Báo cáo này tóm tắt phần đã hiện thực của control-panel UI cho TradingAgents:
backend FastAPI bọc engine tại `server/`, frontend Next.js tại `web/`, 6 màn hình
theo hợp đồng `docs/ui-api-contract.md`, kèm cách chạy, cách test, mức độ
kiểm chứng thực tế, bảo mật/giới hạn và backlog còn mở. Không có sửa đổi nào
trong `tradingagents/` hay `cli/`; chưa commit gì (git HEAD giữ nguyên `38de826`).

## 1. Kiến trúc

```
Browser ──► web/ (Next.js 16.3.7, React 19, Tailwind v4, port 3000)
                │  rewrite /api/* → http://127.0.0.1:8000/api/*   (web/next.config.ts:8)
                ▼
        server/ (FastAPI control panel, port 8000)
                │  gọi engine có sẵn — KHÔNG sửa tradingagents/ hay cli/
                ▼
        tradingagents/ (graph, dataflows, exec/backtest) + state server/data/*.json|.jsonl
```

Hai tiến trình riêng biệt: Next.js chỉ phục vụ UI và proxy `/api/*` sang
FastAPI (`web/next.config.ts:4-12`); FastAPI là lớp mỏng bọc engine theo hợp
đồng. Không có cơ chế mới nào đụng đến mã lõi.

### 1.1 Backend — `server/` (FastAPI bọc engine)

11 module mới (kiểm tra bằng `ls server/`): `__init__.py`, `main.py`,
`contract.py`, `paths.py`, `settings.py`, `watchlist.py`, `runs.py`,
`approvals.py`, `audit.py`, `history.py`, `health.py` — và thư mục state
`server/data/` (đang có `watchlist.json`, `audit.jsonl` sau lần chạy verify).

- **`main.py`** — dựng `app`, CSRF guard (contract §0) ở middleware HTTP:
  mọi POST/PUT/DELETE phải có `Content-Type: application/json` (sai → 415),
  `Origin` phải nằm trong `ALLOWED_ORIGINS` (`server/main.py:26-33`), sai →
  403 `csrf_rejected`; chặn `Sec-Fetch-Site: cross-site` (403). GET và SSE
  không đổi trạng thái nên được miễn. `lifespan` gọi
  `watchlist.ensure_seeded()` khi khởi động (`server/main.py:38-41`).
  Về **origin :8420** trong `ALLOWED_ORIGINS` (`server/main.py:29-30`):
  đây là Base URL mà hợp đồng chỉ định cho control panel —
  `docs/ui-api-contract.md:13` ghi "Base URL: `http://127.0.0.1:8420/api`
  (local control panel; server bind localhost mặc định)". Trong kiến trúc
  2 tiến trình đang mô tả (§1) **không có tiến trình nào lắng nghe 8420**:
  triển khai chạy uvicorn trên **8000** (docstring `server/main.py:5`),
  origin 8420 được whitelist để bảng điều khiển cũng có thể được phục vụ ở
  port hợp đồng chỉ định mà CSRF vẫn cho phép.
- **`settings.py`** — GET/PUT `/api/settings`: đọc config + env với mask
  credential; PUT áp `set_config` in-memory rồi upsert `.env` (ghi utf-8,
  tmp + replace). Cấm ghi key gate (FR5) — cổng thực thi chỉ mở được qua env.
- **`watchlist.py`** — CRUD watchlist dùng file state riêng
  (`server/data/watchlist.json`) và sync ngược `exec_watchlist` trong
  `DEFAULT_CONFIG` để một daily runner chạy trong cùng process đọc đúng danh
  sách.
- **`runs.py`** — POST `/api/runs` + GET `/api/runs/{id}` + GET
  `/api/runs/{id}/events` (SSE `StreamingResponse`): buffer theo `seq`,
  replay bằng `Last-Event-ID`; `mode: "mock"` replay từ
  `full_states_log_<date>.json` hoặc fixture synthetic — UI chạy không cần
  LLM key. Single-run lock (một run tại một thời điểm, trùng → 409 kèm
  `run_id`); worker thread stream chunk theo đúng mô hình
  `cli/run.py:226-323`. File tự ghi chú endpoint daily là
  "future daily-job endpoint" (`server/runs.py:7`).
- **`approvals.py`** — hàng đợi bền tại `server/data/approvals.jsonl`
  (JSONL, sống sót qua restart). Approve chỉ mở khi config `exec_live=True`;
  cổng đóng → **412 `gate_closed`**, `halted_today` → **403**. Reject là
  audit-only, không đụng exchange. Mọi action ghi audit trail riêng tại
  `server/data/audit.jsonl`. Item chưa xử lý **hết hạn sau 24h**
  (`EXPIRY_HOURS = 24`, `server/approvals.py:27`; action vào item hết hạn
  trả **410 `expired`** — `server/approvals.py:94`); xem §7 về vòng đời.
- **`audit.py`** — GET `/api/audit`: đọc `exec_log_path` phân trang + halt
  state; log hỏng → 503.
- **`history.py`** — GET `/api/history` + GET `/api/backtest`, dùng parser
  của engine (decision log, run tree, `results_dir/backtest/*`).
- **`health.py`** — GET `/api/health`: mode/halt/key status, active run,
  số approval pending.
- **`contract.py` / `paths.py`** — error shape thống nhất + error handlers;
  các đường dẫn state (`paths.py:12-16` — `DATA_DIR = server/data/`,
  gitignored; docstring `paths.py:3-5` ghi rõ state của engine
  `~/.tradingagents/` không bị đụng).

Phụ thuộc: `pyproject.toml` thêm optional-dependency **`web`**
(`fastapi>=0.115`, `uvicorn>=0.30`) — `pyproject.toml:45-48`, không đụng
`dependencies` chính; package finder thêm `server*` (`pyproject.toml:54`).

### 1.2 Hợp đồng API — số endpoint

Ghi nhận trung thực về con số: nguyên liệu giao cho bước viết báo cáo ghi
"15 endpoint", nhưng hợp đồng hiện tại trên đĩa là **rev 2 (2026-09-30)**,
bảng tổng hợp §9 liệt kê **17 endpoint** (`docs/ui-api-contract.md:678-700`,
`endpointCount = 17`) — rev 2 đã thêm `POST /api/daily` +
`GET /api/daily/{job_id}` (§5, hàng đợi lệnh).

- `server/main.py:68-77` đăng ký đúng 7 router:
  settings / watchlist / runs / approvals / audit / history / health →
  hiện thực **15/17** endpoint (số 1–8, 11–17 của bảng §9).
- **2 endpoint daily (số 9, 10) chưa có router** — đã grep toàn `server/`,
  không có route nào cho `/api/daily`; `runs.py:7` ghi rõ đây là future
  endpoint. Hệ quả hợp đồng: `POST /api/daily` là **nguồn duy nhất** nuôi
  hàng đợi approvals (`server/approvals.py:99`) — cho tới khi có router
  daily, queue không có producer production; màn M3 (scanner) đã được viết
  đúng contract §5 kèm error-state + gợi ý tiếng Việt cho trường hợp 404.

### 1.3 Frontend — `web/` (Next.js App Router)

Khởi tạo bằng `create-next-app` non-interactive: Next.js **16.3.7**, React
19, Tailwind v4, App Router (`web/package.json:11-22`); `shadcn init -y -d`
chạy thành công, kèm 4 primitive trong `web/components/ui/`:
`button.tsx`, `card.tsx`, `badge.tsx`, `dialog.tsx`; `lightweight-charts`
`^5.2.1` có sẵn trong `web/package.json:15`.

App shell (không đổi khi làm từng màn):

- **Sidebar 6 mục tiếng Việt** — `web/components/sidebar.tsx:15-22`: M1 Tổng
  quan lợi nhuận (`/`), M2 Agent Pipeline (`/pipeline`), M3 Tìm cặp giao dịch
  (`/scanner`), M4 Duyệt lệnh (`/approvals`), M5 Cấu hình (`/settings`),
  M6 Lịch sử & Audit (`/audit`), kèm dòng miễn trừ trách nhiệm
  "Công cụ nghiên cứu — không phải lời khuyên tài chính" (`sidebar.tsx:57-59`).
- **Mode bar toàn cục** — `web/components/mode-bar.tsx`, mounted trong
  `web/app/layout.tsx:33`: đọc `GET /api/settings` mỗi 30s
  (`mode-bar.tsx:22,47-51`). Hiển thị **SANDBOX** (viền xanh dương + khiên
  `ShieldCheckIcon`, chữ "Chế độ mô phỏng — không có lệnh thật nào được gửi")
  khi `effective_exec_mode !== "live"`, hoặc **"LIVE — LỆNH THẬT"** (viền đỏ
  + `TriangleAlertIcon` + mô tả "lệnh được duyệt sẽ gửi thẳng lên sàn") khi
  cả hai cổng FR5 mở (`mode-bar.tsx:53-79`). Màu không phải tín hiệu duy
  nhất — luôn kèm icon + chữ. Ba trạng thái hạng nhất: ĐANG TẢI,
  KHÔNG KẾT NỐI (viền amber + nút "Thử lại") (`mode-bar.tsx:80-101,116-125`).

Design tokens (`web/app/globals.css`): `--color-profit` / `--color-loss`
(globals.css:31-32, giá trị oklch ở :71-72 light và :110-111 dark) qua
`web/lib/format.ts` — lãi luôn kèm dấu `+`, lỗ luôn kèm dấu `−` (U+2212),
số tiền luôn kèm đơn vị; màu không bao giờ là tín hiệu duy nhất (WCAG
1.4.1). Utility `min-touch` ≥44px (globals.css:131) và focus-visible ring 2px
rõ ràng.

### 1.4 Sáu màn hình

| Màn | Route | Code | Endpoint dùng | Trạng thái code | Runtime |
|---|---|---|---|---|---|
| M1 Tổng quan lợi nhuận | `/` | `web/app/page.tsx` + `web/components/overview/` (7 file) | GET /api/health, /api/history, /api/audit | Xong (code) | Prerender qua `next start` (không backend) |
| M2 Agent Pipeline | `/pipeline` | `web/app/pipeline/page.tsx` + `web/components/pipeline/` (8 file) | POST /api/runs, GET /api/runs/{id}, /events (SSE), /api/health | Xong (code) | **✓ smoke với backend :8000** |
| M3 Tìm cặp giao dịch | `/scanner` | `web/app/scanner/page.tsx` + `web/components/scanner/` (7 file) | watchlist CRUD (§2), POST /api/daily + poll (§5) | Xong (code) | Chưa; daily chưa có router |
| M4 Duyệt lệnh | `/approvals` | `web/app/approvals/page.tsx` + `web/components/approvals/` (11 file) | GET /api/approvals, POST approve/reject, /api/health | Xong (code) | Chưa (chỉ page 200 trong smoke) |
| M5 Cấu hình | `/settings` | `web/app/settings/page.tsx` + `web/components/settings/` (10 file) | GET/PUT /api/settings (§1) | Xong (code) | Chưa |
| M6 Lịch sử & Audit | `/audit` | `web/app/audit/page.tsx` + `web/components/audit/` (8 file) | GET /api/audit, /api/history, /api/backtest | Xong (code) | **✓ e2e qua browser với backend thật** |

Định nghĩa cột, để không gây hiểu nhầm:

- **"Xong (code)"** = mã màn hình hoàn thành + qua eslint / tsc / `next
  build`. Không đồng nghĩa với "đã chạy được với dữ liệu thật".
- **"Runtime"** = màn đã được kiểm tra với backend sống hay chưa; chi tiết
  từng lần chạy ở §5.1, phần chưa kiểm chứng ở §5.3.

Điểm chung của cả 6 màn: 4 trạng thái hạng nhất
loading/empty/error/stale (poll theo `stale_after` của hợp đồng; poll lỗi
giữ dữ liệu cũ + badge "Dữ liệu cũ"); empty-state nêu việc cần làm tiếp và
trỏ sang màn liên quan; nút chính và input ≥44px; mọi label tiếng Việt;
số tiền luôn kèm đơn vị (USD/BTC…) và dấu +/− phía lệnh. Các màn có thao tác
không reversable dùng dialog 2 bước (M4 duyệt), còn thao tác reversable
(xóa watchlist) không hỏi confirm. M1/M4/M6 có banner RiskGuard
halt (đỏ) và lệnh chờ duyệt (amber). M2 dùng SSE (`EventSource` tự
reconnect `Last-Event-ID`, dedupe `seq`, poll 2s khi SSE rớt, badge "đang
treo" sau 60s; v1 chưa có nút huỷ). M5 dùng state machine
loading/error/stale-first-class (poll 30s, không ghi đè form khi dirty, save
bar chỉ gửi key dirty, map lỗi 422 `coercion_error` về đúng ô, thẻ chỉ-đọc
cho khóa API masked).

## 2. Cách chạy

Yêu cầu: đã `pip install -e ".[dev,web]"` (extra `web` cấp fastapi/uvicorn)
và `cd web && npm install` — node_modules **không** nằm trong một clone git
(bị ignore, đúng như `.gitignore:230`); câu trước có nghĩa là node_modules
**đã được cài đặt sẵn trên máy làm việc này** từ những bước trước, máy mới
phải tự chạy `npm install`.

**Backend** — từ thư mục gốc repo (Windows):

```bash
.venv\Scripts\python.exe -m uvicorn server.main:app --port 8000
```

Đúng như docstring của chính `server/main.py:5`. Khởi động xong sẽ seed
watchlist qua lifespan. Về port: hợp đồng chỉ định Base URL `:8420`
(`docs/ui-api-contract.md:13`) nhưng triển khai chuẩn hoá về **8000** cho
khớp rewrite của Next (`web/next.config.ts:8-9`); nếu chạy `--port 8420`
thì CSRF vẫn chấp nhận vì origin 8420 nằm sẵn trong `ALLOWED_ORIGINS`
(`server/main.py:29-30`).

**Frontend** — terminal thứ hai:

```bash
cd web
npm run dev
```

Next dev lắng nghe port 3000 và proxy mọi `/api/*` sang
`http://127.0.0.1:8000/api/*` (web/next.config.ts). Mở
`http://localhost:3000` — mode bar sẽ hiện SANDBOX hoặc LIVE tuỳ `GET
/api/settings`.

State server nằm ở `server/data/` (`watchlist.json`, `approvals.jsonl`,
`audit.jsonl` — vòng đời xem §7). Chỉ bind backend ra ngoài 127.0.0.1 sau
khi thêm token — chính sách chi tiết ở §6.

## 3. Cách test

**Backend (pytest unit — không mạng, không LLM):**

```bash
.venv\Scripts\python.exe -m pytest -q            # toàn suite repo
.venv\Scripts\python.exe -m pytest -q tests/test_server_api.py
```

`tests/test_server_api.py` có **28 unit test** dùng FastAPI `TestClient`,
không mở socket: fixture `_no_network` (`tests/test_server_api.py:35-58`,
docstring mô tả hành vi ở :37-44) chặn mọi `connect` thật nhưng whitelist
đúng cặp socket self-pipe mà event loop Windows tự dựng; không gọi LLM
(fake graph); mọi dữ liệu mock vào tmp. Phủ đủ các luồng: health
dry-mode, settings mask/persist/cấm key gate/coercion 422, 3 nhánh CSRF
(415 content-type, 403 origin lạ, 403 cross-site + 1 case origin hợp lệ),
watchlist roundtrip, run mock hoàn thành với synthetic events, v.v.

**Lint backend:**

```bash
.venv\Scripts\python.exe -m ruff check .
```

**Frontend — giới hạn đã biết: `web/` hiện KHÔNG có test tự động nào.**
`web/package.json` chỉ có 4 script `dev / build / start / lint`
(`web/package.json:5-9`), devDependencies không có vitest / jest /
playwright — ngoài lint + typecheck + build, frontend chưa được kiểm tra
bằng gì khác. Thêm harness test frontend là mục còn mở (§8).

```bash
cd web
npm run lint        # eslint (config eslint-config-next)
npx tsc --noEmit    # typecheck
npm run build       # next build (kèm typecheck)
```

**Smoke chạy thật (đã thực hiện trong phiên triển khai):** bật backend
uvicorn :8000 + `npm run dev` :3000 song song, curl qua rewrite:
`GET /api/health` 200, `GET /` 200, `GET /approvals` 200, `GET /pipeline`
200; backend riêng cũng verify health/CSRF-415/watchlist-seed/SSE mock
replay đúng — sau đó tắt cả hai server và dọn state. Chi tiết, giới hạn và
việc không còn lưu vết xem §5.

## 4. Kiểm tra tự động — con số thực chạy

Con số dưới đây là kết quả chạy trực tiếp trong phiên viết báo cáo này,
trên máy hiện tại, sau khi toàn bộ `server/` + 28 test mới + `web/` đã tồn
tại (không phải điểm neo cũ):

- `.venv\Scripts\python.exe -m pytest -q` →
  **1212 passed, 5 skipped, 22 warnings, 91 subtests passed in 12.38s**
  (chạy trong phiên này; con số "1001 passed, 5 skipped" trong AGENTS.md
  là mốc 2026-09-28 — trước khi `server/` + 28 test mới tồn tại, chênh
  211 test — không mô tả suite hiện tại).
- `.venv\Scripts\python.exe -m ruff check .` → "All checks passed!"
  (chạy trong phiên này).

Các lớp chỉ có từ nguyên liệu của phiên triển khai (chưa chạy lại ở đây):
`npm run build` trong `web/` — "Compiled successfully", TypeScript OK,
9/9 route static; `npm run lint` — "No issues found" (sau khi sửa 2 lỗi:
unused import và react-hooks/set-state-in-effect).

## 5. Mức độ kiểm chứng

### 5.1 Đã kiểm chứng trực tiếp (trong phiên triển khai, theo nguyên liệu)

> **Lưu ý lưu vết:** toàn bộ kết quả runtime dưới đây chỉ dựa trên nguyên
> liệu của phiên triển khai — **không có bản lưu vết nào được giữ lại từ
> các lần smoke** (không log, không screenshot, không transcript trong
> repo; `server/data/` hiện chỉ còn `audit.jsonl` 304B +
> `watchlist.json` 194B sau khi dọn). Quản trị viên không thể đối chiếu lại
> bất kỳ mục nào của mục này từ repo — đây là minh bạch về nguồn, không
> phải bằng chứng tái kiểm chứng được.

- **Backend sống trên port 8000** (uvicorn, `server.main:app`):
  `GET /api/health` → 200 với body đúng hợp đồng (`exec_mode:"dry"`,
  `halted_today:false`, `pending_approvals:0`, không có LLM key, LLM wire
  protocol `chat`); CSRF trả 415 khi Content-Type sai; watchlist seed đúng;
  SSE mock replay đúng; sau verify đã tắt server và dọn state.
- **Frontend build/lint/typecheck**: `npm run build` sạch 9/9 static,
  `npm run lint` "No issues found".
- **Smoke song song 2 server**: backend + `npm run dev` cùng lúc —
  `/api/health` 200, `/` 200, `/approvals` 200, `/pipeline` 200; sau đó
  TaskStop cả hai task và xác nhận `netstat` không còn LISTENING :8000/:3000
  (không sót tiến trình).
- **M6 kiểm tra end-to-end qua browser** với backend thật: 7 dòng audit +
  3 quyết định + backtest rỗng, trang render đúng theo snapshot.
- **M2 smoke với backend sống**: POST mock 201; 409 conflict kèm
  `details.run_id`; 400 validation_error cho ngày tương lai; SSE replay
  21 events đủ 11 loại đúng thứ tự; terminal `run_completed` signal
  "Underweight".
- **`.gitignore`**: root hiện có `web/node_modules/`, `web/.next/` và
  `server/data/` — nhưng đây **không phải vốn có trước**: `git diff
  .gitignore` cho thấy chính **7 dòng này được THÊM MỚI** trong thay đổi
  hiện tại (block "Next.js control panel (web/)" + "Control-panel server
  state", +7 insertions), `git status` ghi `.gitignore` là `M`. Trước đó
  nguyên liệu xác nhận `git check-ignore` exit=0 — tức ignore đang **hiệu
  lực**, không phải đã tồn tại từ trước khi làm UI. Tất cả chưa commit.

### 5.2 Phát hiện trong phép thử smoke (trung thực)

Phép thử smoke kết luận `ok=false` vì **duy nhất** một phép thử:
`GET http://127.0.0.1:3000/dashboard` → **404**. Nguyên nhân đã kiểm chứng
bằng `ls -R web/app`: route `/dashboard` **không tồn tại** trong app —
các route thật là `/`, `/pipeline`, `/scanner`, `/approvals`, `/settings`,
`/audit` (khớp sidebar §1.3). Phép thử đối chứng trên cùng server:
`GET /pipeline` và `GET /approvals` đều 200, tức app phục vụ route con bình
thường. Đây không phải lỗi của app mà là phép thử gọi sai tên route;
không cần sửa code.

### 5.3 Chưa kiểm chứng / giới hạn đã biết

- **Router daily chưa tồn tại ở backend** (§1.2): `/api/daily` +
  `/api/daily/{job_id}` mới chỉ có trong hợp đồng; M3 chưa chạy được luồng
  daily thật và hàng đợi approvals chưa có producer production. Đây là
  phần triển khai còn mở, không phải lỗi UI — vào backlog (§8).
- **M1, M3, M5 chưa chạy runtime với backend sống / browser** (M2, M6 đã
  có smoke/e2e như §5.1; M4 chỉ page 200): trạng thái
  loading/empty/error/stale của các màn đó được thiết kế và qua được
  lint/build nhưng chưa được kiểm tra bằng dữ liệu thật.
- **Frontend không có test tự động** ngoài lint/tsc/build (§3).
- Phép thử đối chứng `/dashboard` 404 — ghi nhận ở §5.2, không phải lỗi
  sản phẩm.
- Chưa commit gì; `pyproject.toml` chỉ thay đổi đúng optional-extra `web`
  và package finder; không đụng `tradingagents/`, `cli/`. Header của
  `docs/ui-api-contract.md:3` vẫn ghi "status: design (chưa có
  implementation)" — đã lỗi thời; đưa vào backlog (§8).

## 6. Bảo mật & giới hạn

Đây là bảng điều khiển có thể duyệt lệnh LIVE, nên các giới hạn an ninh
được ghi tách riêng:

- **CSRF guard ≠ xác thực.** Guard ở `server/main.py:48-65` chỉ chặn
  *trang web lạ trong browser* (Content-Type safelist + Origin +
  `Sec-Fetch-Site`); nó **không** chống được process local, malware hay bất
  kỳ client nào chạy trực tiếp trên máy — mọi endpoint vẫn tin tưởng
  localhost. Đây là nhận định của chính hợp đồng, mục rủi ro §10.12
  (`docs/ui-api-contract.md`: "CSRF guard ≠ auth … token auth chỉ bắt buộc
  khi mở bind ngoài `127.0.0.1`").
- **Token auth — điều kiện bắt buộc trước khi mở bind.** Hiện trạng:
  server bind localhost; docstring `server/main.py:12-13` trích contract
  §0 và §10.12 để nói rõ token chỉ bắt buộc khi bind ra ngoài
  `127.0.0.1`. Thiết kế token chưa được hiện thực ở `server/` (không có
  middleware auth nào ngoài CSRF guard — grep `server/` không thấy);
  do đó **chưa được phép** chạy `--host` ngoài loopback. Để mở bind cần
  lần lượt: thêm token (middleware auth), đưa token vào `web/` khi gọi
  `/api/*`, rồi mới đổi port/host.
- **Cổng thực thi FR5 chỉ có hai khóa, cả hai ngoài tay API.** PUT
  `/api/settings` cấm ghi key gate (403 `forbidden_key` — test
  `test_settings_put_forbids_gate_and_unknown_keys`); mode bar đọc
  `effective_exec_mode` = "live" chỉ khi `exec_live` (config) và
  `TRADINGAGENTS_EXEC_LIVE` (env) cùng mở. Approve tiếp tục chặn 412 khi
  cổng đóng (`server/approvals.py`). Không có endpoint reset halt trong v1
  (contract §10 mục 8 — operator dùng CLI).
- **Giao diện của guard không thay thế net-isolation**: 3000/8000 vẫn là
  port local; nếu phải mở ra LAN, làm theo §6 trên và review lại toàn bộ
  mục này.

## 7. Vòng đời dữ liệu state

State riêng của server nằm dưới `server/data/` (gitignored —
`server/paths.py:1-8,12-16`; state engine-side `~/.tradingagents/` không bị
đụng). Ba file, ba vòng đời khác nhau:

- **`watchlist.json`** — trạng thái thường trú của CRUD watchlist, được
  seed lần đầu qua lifespan và sync ngược `exec_watchlist` khi server khởi
  động (`server/watchlist.py`). Ghi đè toàn bộ mỗi lần CRUD; không hết hạn.
- **`approvals.jsonl`** — hàng đợi bền (JSONL, sống qua restart). Item
  chưa xử lý **hết hạn sau 24 giờ**: `EXPIRY_HOURS = 24`
  (`server/approvals.py:27`), mọi action vào item quá hạn trả
  **410 `expired`** (`server/approvals.py:94`). Grep `server/` không thấy
  mã xoay/truncate/xoá dòng (`rotate|truncat|unlink` — 0 kết quả ngoài
  tham số phân trang `limit`), nên **item đã xử lý vẫn nằm trong file**
  — file chỉ tăng dài; chưa có chính sách nén/luân chuyển.
- **`audit.jsonl`** — audit trail của API layer, append-only, **không có
  xoay hay giới hạn kích thước** (grep `rotate|truncate|unlink|limit` trong
  `server/audit.py` và `server/paths.py` — không kết quả về vòng đời).
  Hợp đồng đã tự liệt kê rủi ro này ở §10 mục 7: `/api/audit` scan toàn
  file, khi lớn cần index/mmap; dòng JSON hỏng bị bỏ qua — UI cần thấy
  `skipped_lines`.
- **Backup/restore: chưa có cơ chế nào** cho cả ba file; muốn sao lưu phải
  copy thủ công `server/data/` trước khi nâng cấp code. File
  `exec_log_path` của engine (đầu vào phân trang của `/api/audit`, nằm ở
  `~/.tradingagents/logs`) thuộc vòng đời của engine/CLI, không do API quản.

Hiện trạng đĩa sau lần verify: `server/data/` chỉ còn `audit.jsonl` (304B)
và `watchlist.json` (194B) — `approvals.jsonl` vắng mặt vì state đã được
dọn sau smoke.

## 8. Việc còn mở / bước tiếp theo

Thứ tự dưới đây là **đề xuất** của người viết báo cáo (dựa trên các факт
đã xác minh ở §1.2, §5.3, §6, §7), không phải quyết định đã duyệt:

1. **Router daily + producer cho hàng đợi approvals** — hiện thực
   `POST /api/daily` + `GET /api/daily/{job_id}` (số 9–10 hợp đồng §9) bọc
   `run_daily` như hợp đồng §5 chỉ định; đây là điều kiện để M3 chạy thật
   và là **nguồn duy nhất** nuôi queue approvals (`server/approvals.py:99`).
   Cần lưu ý rủi ro hợp đồng §10 mục 11: job daily chiếm worker lock lâu
   (10–30 phút cho watchlist 5 coin), v1 không có cancel.
2. **Commit / PR** — `git status` hiện có: `M .gitignore`, `M
   pyproject.toml`, untracked `AGENTS.md`, `docs/`, `server/`, `web/`,
   `tests/test_server_api.py` (và file rác `nul` ở root — cần xoá trước
   khi commit). Đề xuất tách PR: backend `server/` + tests + pyproject +
   .gitignore trước, frontend `web/` sau (hoặc một PR nếu muốn nguyên khối).
3. **Cập nhật header hợp đồng** — `docs/ui-api-contract.md:3` còn ghi
   "status: design (chưa có implementation)"; sửa thành trạng thái
   "implementation: server/ 15/17 endpoint, web/ 6 màn".
4. **Kiểm chứng runtime còn thiếu** — M1/M3/M5 (và M4 phần data-flow)
   chạy với backend sống; M3 phụ thuộc mục 1.
5. **Test frontend** — thêm harness (vitest cho lib/format + hooks, và
   Playwright khi cần e2e) vì hiện `web/` chỉ có lint/tsc/build (§3).
6. **Vận hành dài hạn 2 tiến trình** — v1 là 2 terminal thủ công
   (§2); lâu dài cần script/service (ví dụ `npm run dev` + uvicorn gói
   chung, hoặc chạy production `next build && next start` + uvicorn dưới
   systemd/Task Scheduler) và chính sách sao lưu `server/data/` (§7).
