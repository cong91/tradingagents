---
title: Sổ tay vận hành Control Panel — TradingAgents
updated: 2026-10-01 (rev 2 — sửa theo góp ý đọc thử; changelog cuối tài liệu)
audience: người điều hành (operator) chưa từng dùng hệ thống
nguồn: docs/ui-api-contract.md (rev 3.1), docs/ui-implementation.md, web/README.md, server/, web/components/
---

# Sổ tay vận hành Control Panel

Tài liệu này dành cho người điều hành mới: từ bật hệ thống, chạy phiên phân tích,
đến duyệt lệnh và tra cứu audit. Mọi mô tả màn hình lấy đúng từ code hiện có của
repo (`web/`, `server/`) — không mô tả tính năng chưa tồn tại.

> **Miễn trừ trách nhiệm:** đây là công cụ nghiên cứu, không phải lời khuyên tài chính
> (dòng này cũng hiển thị cố định ở chân sidebar của ứng dụng — `web/components/sidebar.tsx:55`).

---

## 1. Bức tranh chung

### 1.1 Control panel là gì?

Control panel là giao diện web gồm **2 tiến trình**:

```
Trình duyệt ──► web/ (Next.js, port 3000)  ── proxy /api/* ──►  server/ (FastAPI, port 8000)
                                                                     │
                                                                     ▼
                                                       tradingagents/ (engine LangGraph + ccxt — KHÔNG bị sửa)
```

- **web/** chỉ phục vụ giao diện và chuyển tiếp mọi `/api/*` sang backend
  (`web/next.config.ts:4-12`) — nên không cần cấu hình CORS, mở `http://localhost:3000`
  là dùng được cả API.
- **server/** là lớp mỏng bọc engine: quản lý cấu hình, watchlist, chạy phiên phân
  tích, giữ hàng đợi duyệt lệnh (`server/data/approvals.jsonl`), đọc audit log.
- **Engine** (`tradingagents/`) là phần phân tích bằng LLM + thực thi qua sàn (ccxt).
  Control panel không thay đổi logic của nó.

### 1.2 Nguyên tắc an toàn số 1: lệnh thật chỉ đi qua MỘT cửa

- Pipeline phân tích (`POST /api/runs`) và job quét (`POST /api/daily`) **chỉ phân
  tích và lập kế hoạch — không bao giờ tự đặt lệnh** (`server/runs.py:1-10`,
  `server/daily.py:1-11`).
- Kế hoạch lệnh vào **hàng đợi Duyệt lệnh**. Người điều hành duyệt (hoặc từ chối)
  từng kế hoạch — duyệt là **đường duy nhất** đưa lệnh vào `execute_order`
  (`server/approvals.py:5-10`).
- Lệnh thật (LIVE) cần **cổng FR5 hai khoá**, và hai khoá mở theo **hai đường khác
  nhau** — điểm này quan trọng nhất của hệ thống:
  - **Nửa config** `exec_live`: mặc định `False` (`tradingagents/default_config.py:206`)
    và **không đặt được qua `.env`** — key cố ý vắng khỏi bảng biến môi trường
    (`default_config.py:33-36`), PUT `/api/settings` cũng từ chối key này (403
    `forbidden_key` — `server/settings.py:209-211`). Muốn mở phải sửa code: đổi
    `default_config.py:206` thành `"exec_live": True` rồi khởi động lại backend, hoặc
    một script chạy trong cùng process server gọi `set_config({"exec_live": True})`.
    Quy trình đầy đủ ở §4.8.
  - **Nửa môi trường** `TRADINGAGENTS_EXEC_LIVE`: **cái này mới đặt được qua `.env`**
    — bridge đọc flag lúc gọi, fail-closed (`tradingagents/execution/bridge.py:527-539`).
  - Giao diện web không mở được khoá nào (`server/settings.py:5-9`, hợp đồng §1 quy tắc 2).

### 1.3 Luồng một ngày làm việc

```
1. CẤU HÌNH (/settings)      — kiểm tra key API (trạng thái), ngưỡng rủi ro, sàn, chế độ mode bar
2. WATCHLIST/SCANNER (/scanner) — thêm/bớt mã, bấm "Quét ngay"
3. PIPELINE (/pipeline)       — (tùy chọn) chạy một phiên phân tích riêng cho 1 mã, đọc stream agent
4. HÀNG ĐỢI DUYỆT (/approvals) — kế hoạch lệnh từ bước 2 xuất hiện ở đây, kèm lý do RiskGuard
5. DUYỆT hoặc TỪ CHỐI          — duyệt: server chạy lại kiểm tra rủi ro rồi thực thi (dry hoặc live).
                                  Lưu ý cài mới: cổng FR5 đang đóng → mọi lần duyệt bị từ chối
                                  412 cho tới khi mở cổng (quy trình §4.8).
6. AUDIT & P&L (/ và /audit)  — xem lệnh đã thực thi, P&L đã chốt, nhật ký audit đầy đủ
```

Vòng lặp: quét → duyệt → xem kết quả → (điều chỉnh ngưỡng rủi ro nếu cần) → quét lại.

---

## 2. Bắt đầu

### 2.1 Khởi động hệ thống

**Cách nhanh nhất — nháy đúp `start.bat` ở thư mục gốc repo.** File này tự làm
đủ mọi thứ: kiểm tra venv, tự `npm install` lần đầu nếu thiếu, khởi động backend
và frontend (mỗi tiến trình một cửa sổ log riêng), **tái sử dụng instance đang
chạy** nếu port 8000/3000 đã có người nghe, chờ backend sẵn sàng rồi tự mở
trình duyệt ở `http://localhost:3000`.

Cách thủ công (khi cần xem log trực tiếp hoặc debug):

Cài lần đầu (máy mới): cần Python venv của repo với extra `web` (cấp FastAPI +
uvicorn — `pyproject.toml:42-45`) và Node.js cho frontend:

```powershell
# Cài backend (một lần)
.venv\Scripts\python.exe -m pip install -e ".[web]"

# Cài frontend (một lần)
cd web
npm install
cd ..
```

Mỗi ngày làm việc, mở **hai cửa sổ terminal** từ thư mục gốc repo:

```powershell
# Cửa sổ 1 — backend FastAPI (port 8000)
.venv\Scripts\python.exe -m uvicorn server.main:app --port 8000

# Cửa sổ 2 — frontend (port 3000)
cd web
npm run dev
```

Mở **http://localhost:3000** trong trình duyệt.

> Kiểm chứng đã thực hiện (2026-10-01, chính máy này): `start.bat` chạy trọn
> vẹn — khởi động mới lẫn tái sử dụng instance đang chạy, health trả 200, giao
> diện port 3000 trả HTTP 200; lệnh thủ công trên cũng khởi động thành công với
> `exec_mode:"dry"`. Xem §5.

Khi backend khởi động xong, nó tự seed watchlist mặc định lần đầu (lấy từ cấu hình
engine — `server/watchlist.py:47-51`).

### 2.2 API key — cài ở đâu?

**Màn Cấu hình chỉ HIỂN THỊ trạng thái key, không cho sửa.** Đây là chủ đích an toàn:
key không bao giờ được trả nguyên văn về UI — chỉ "Đã cấu hình" + 4 ký tự cuối
(`web/components/settings/credentials-card.tsx:25-28`, `server/settings.py:140-148`).

Cách cài key:

1. Mở file `.env` ở thư mục gốc repo (mẫu: `.env.example`).
2. Thêm/sửa dòng, ví dụ:
   - `OPENAI_API_KEY=...` (hoặc `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY` tuỳ provider)
   - `BINANCE_API_KEY=...` và `BINANCE_SECRET=...` (khi chạy lệnh thật qua sàn)
3. **Khởi động lại backend** (Ctrl+C cửa sổ 1 rồi chạy lại lệnh uvicorn) — env được
   đọc lúc process khởi động.
4. Vào màn **Cấu hình**, thẻ "Khóa API" phải hiện badge "Đã cấu hình ••••abcd".

Key nào cần khi nào? Xem §5.3 (tóm tắt: Mock không cần key nào; phân tích Live cần
LLM key; duyệt lệnh thật cần thêm key sàn).

---

## 3. Từng màn hình

Sidebar trái có 6 mục (`web/components/sidebar.tsx:15-22`). Thanh **Mode bar** nằm
đỉnh mọi trang.

### 3.0 Mode bar SANDBOX / LIVE

Đọc `GET /api/settings` mỗi 30 giây (`web/components/mode-bar.tsx:22`). Bốn trạng thái:

| Hiển thị | Ý nghĩa |
|---|---|
| **SANDBOX** (viền xanh dương, khiên) | Chế độ mô phỏng — **không có lệnh thật nào được gửi lên sàn**. Chữ SANDBOX che **hai trạng thái khác nhau** mà mode bar không phân biệt được (đều hiện SANDBOX): **(a) Cài mới** — `exec_live` đang `False` (mặc định, `default_config.py:206`): **mọi lần bấm Duyệt bị server từ chối 412 `gate_closed`**, kế hoạch giữ nguyên "Chờ duyệt", không có gì chạy cả mô phỏng (`server/approvals.py:246-251`). **(b) Trung gian** — `exec_live=True` trong config nhưng env `TRADINGAGENTS_EXEC_LIVE` tắt: duyệt được chấp nhận và chỉ *dry-fill* (ghi sổ mô phỏng, không chạm ccxt). |
| **LIVE — LỆNH THẬT** (viền đỏ, tam giác cảnh báo) | "Cả hai cổng thực thi đang mở: lệnh được duyệt sẽ gửi thẳng lên sàn." Lệnh duyệt ở trạng thái này là tiền thật, không hoàn tác. |
| ĐANG TẢI CHẾ ĐỘ… | Request đầu tiên chưa trả về. |
| KHÔNG KẾT NỐI (viền vàng) + nút "Thử lại" | Không đọc được `/api/settings` — gần như chắc chắn backend chưa chạy (xem §4.1). |

> Hiểu nhanh: **SANDBOX ≠ duyệt được.** Trên cài mới, SANDBOX nghĩa là "không duyệt
> được gì" (412); trạng thái dry-fill nằm giữa hai cổng và chỉ xuất hiện sau khi
> operator đã mở nửa config (§4.8). Bấm Duyệt và nhận 412 là hành vi bình thường của
> cài mới — không phải lỗi.

Vì sao LIVE phải hiển thị rõ ràng như vậy: duyệt lệnh LIVE là hành động không thể
hoàn tác và mất tiền thật. LIVE chỉ bật khi **cả hai nửa cổng FR5** cùng mở —
`exec_live=True` (config) **và** `TRADINGAGENTS_EXEC_LIVE=true` (biến môi trường);
nếu chỉ thấy "live" trên mode bar thì nghĩa là cả hai nửa đang mở đồng thời
(`server/settings.py:60-70`). **UI không thể bật khoá nào** — nửa config chỉ mở bằng
cách sửa code (§4.8), nửa env mới qua `.env`/môi trường. Màu không bao giờ là tín
hiệu duy nhất: luôn kèm icon + chữ (WCAG).

### 3.1 Tổng quan lợi nhuận (`/`)

Màn mở đầu — nhìn một cái thấy tiền và trạng thái. Tự làm mới: health 15s, audit 10s,
portfolio 30s, lịch sử 60s (`web/components/overview/dashboard.tsx:36-39`).

**Banner trên cùng:**
- Banner đỏ "RiskGuard đã dừng giao dịch hôm nay" — kèm lý do và link "Xem audit"
  (`dashboard.tsx:229-238`).
- Banner vàng "N lệnh đang chờ duyệt" — link thẳng sang màn Duyệt lệnh
  (`dashboard.tsx:240-248`).

**Card "Danh mục"** (`portfolio-card.tsx`): 4 số chính — *Tiền mặt*, *Giá trị ước tính*
(cash + tổng vị thế nhân giá thị trường), *Đã chốt (tổng)*, *Đã chốt hôm nay* (dấu
+/− rõ ràng). Bảng vị thế: Mã, Khối lượng, Giá vốn, Giá thị trường, Giá trị,
Lãi/lỗ tạm tính. Chú thích đáng đọc:
- "Đã chốt hôm nay" tính theo **ngày UTC** và chỉ gồm lệnh đã đóng — **khác** con số
  RiskGuard dùng (guard cộng thêm lãi/lỗ tạm tính của vị thế mở trong ngày).
- Vị thế mở **trước khi audit log bắt đầu** không có giá vốn — hiển thị "—" (không bịa số).
- Chưa có dữ liệu gì → empty-state: "Số liệu sẽ xuất hiện sau khi pipeline đặt lệnh
  đầu tiên (qua màn Duyệt lệnh)".

**Card "Hiệu suất quyết định"**: KPI + biểu đồ alpha (điểm %, lãi luôn `+`, lỗ luôn
`−`) của các quyết định trong decision log; quyết định chưa chốt chưa có alpha.

**Card "Lệnh thực thi gần đây"**: bảng rút từ audit log của engine — *"mua là tiền ra
(−), bán là tiền vào (+)"*.

Mọi card có badge **"Dữ liệu cũ"** khi poll gần nhất lỗi (giữ dữ liệu cũ, không nhảy
trắng) và nút **Làm mới** toàn trang.

### 3.2 Agent Pipeline (`/pipeline`)

**Mục đích:** chạy **một phiên phân tích** cho một mã và xem từng agent làm việc theo
thời gian thực. Phiên **chỉ phân tích — không bao giờ sinh lệnh giao dịch**
(`pipeline-screen.tsx:126-129`).

**Tạo phiên** (`run-form.tsx`): điền
- *Mã tài sản* (mặc định `BTC-USD`; ví dụ `BTC-USD`, `NVDA`),
- *Ngày phân tích* (không được ở tương lai — server so với "hôm nay" UTC),
- *Loại tài sản*: Crypto hoặc Cổ phiếu (crypto dùng 3 analyst: market, social, news;
  stock thêm fundamentals — `server/runs.py:79-84`),
- *Chế độ*: **Mock — không gọi LLM** (mặc định) hoặc **Live — gọi LLM thật**,
- tùy chọn *Bật checkpoint* (tiếp tục phiên bị gián đoạn),

rồi bấm **"Chạy phân tích"**.

**Mock vs Live — khác nhau thế nào:**
- *Mock*: server phát lại chuỗi sự kiện từ file state log thật của mã
  (`full_states_log_<date>.json` — file tồn tại sau mỗi run hoàn tất) nếu có, không
  có thì dùng fixture tổng hợp; **không gọi LLM, không gọi mạng, không cần API key**;
  mọi sự kiện mang nhãn "Mock (không gọi LLM)" (`server/runs.py:144-270`). Dùng để
  học giao diện và demo.
- *Live*: dựng graph LangGraph thật, gọi LLM (cần key của provider đã cấu hình) và
  data vendor; mất nhiều phút; ghi báo cáo thật ra đĩa (`results_dir`).

**Một phiên tại một thời điểm.** Hệ thống chỉ chạy được một run/job cùng lúc (worker
duy nhất vì config là process-wide). Bấm thêm khi đang chạy → lỗi **409** kèm id phiên
đang chạy; UI hiện nút "Theo dõi phiên đang chạy (…)" để nối vào phiên đó
(`pipeline-screen.tsx:70-98`).

**Đọc stream:** thẻ "Phiên phân tích" có badge **"SSE trực tiếp"** (hoặc "Đang kết nối
lại…"). Mất kết nối tự reconnect và phát lại phần thiếu. **Stage tracker 7 bậc**
(`stage-tracker.tsx:17-25`) — ý nghĩa:

1. **Analyst** — các analyst (thị trường, sentiment, news, [fundamentals]) thu thập dữ liệu.
2. **Tranh luận Bull/Bear** — 2 researcher lập luận tăng/giảm.
3. **Research Manager** — chấm dứt tranh luận, rút ra kế hoạch nghiên cứu.
4. **Trader** — biến kế hoạch thành ý định giao dịch.
5. **Tranh luận rủi ro** — 3 phía (hùng hợp/bảo thủ/trung lập) tranh cãi về rủi ro.
6. **Quản lý danh mục (PM)** — chốt quan điểm cuối.
7. **Quyết định cuối** — tín hiệu 5-tier.

Card "Luồng sự kiện" liệt kê nguyên văn nội dung agent + tool call, mới nhất ở cuối.

**Kết quả phiên** (`run-summary.tsx`): tín hiệu cuối dạng badge — **Buy (Mua)**,
**Overweight (Thiên tăng)**, **Hold (Giữ)**, **Underweight (Thiên giảm)**,
**Sell (Bán)**, **REVIEW (Cần rà soát)**. `REVIEW` là tín hiệu **không giao dịch
được** (quyết định không đọc được rating) — phải rà soát thủ công. Phiên Live còn liệt kê
đường dẫn tệp báo cáo + state log; phiên Mock tổng hợp không ghi tệp.

**Phiên treo:** không nhận sự kiện > 60 giây trong khi vẫn "running" → badge
"Đang treo?" + gợi ý đợi thêm/kiểm tra log server. **v1 chưa có nút huỷ phiên** —
đợi kết thúc tự nhiên hoặc khởi động lại server (phiên là state in-memory, mất khi
restart; các dòng audit đã ghi vẫn còn).

### 3.3 Tìm cặp giao dịch (`/scanner`)

Hai card:

**Card "Danh sách theo dõi"** (`watchlist-panel.tsx`): các mã sẽ được quét.
- Thêm: nhập mã (định dạng Yahoo, ví dụ `BTC-USD`, `SOL-USD`, `NVDA` — server tự
  uppercase) → "Thêm mã". Trùng mã bị từ chối (lỗi 409 hiển thị ngay dưới ô nhập).
- Xoá: nút ✕ đầu dòng — thao tác đảo ngược được nên **không hỏi confirm**.
- Xoá hết vẫn hợp lệ, nhưng lúc đó không quét được gì.
- Watchlist rỗng lần đầu → server tự seed theo mặc định engine (`BTC-USD`, `ETH-USD`).

> ⚠️ **Job quét luôn phân tích theo crypto — kể cả mã cổ phiếu.** Worker của "Quét
> ngay" dựng graph cứng `selected_analysts=("market","social","news")` và gọi
> `propagate(..., asset_type="crypto")` cho mọi mã trong watchlist
> (`server/daily.py:163-164`). Đưa mã cổ phiếu (ví dụ `NVDA`) vào watchlist nghĩa là
> nó bị phân tích **thiếu fundamentals analyst** và lập kế hoạch như crypto. Muốn
> phân tích cổ phiếu đúng, dùng màn **Agent Pipeline** với "Loại tài sản = Cổ phiếu"
> (`run-form.tsx:83-92`) — chỗ đó mới chọn được stock.

**Card "Quét pipeline"** (`scan-panel.tsx`): bấm **"Quét ngay"** → tạo job quét toàn
bộ watchlist hiện hành (`POST /api/daily`), rồi UI tự poll kết quả mỗi 3 giây.
Cần biết:
- **Nút "Quét ngay" hiện chạy chế độ LIVE (gọi LLM thật)** — UI gửi body rỗng `{}`,
  server mặc định `mode:"live"` (`web/components/scanner/scanner-api.ts:71-78`,
  `server/daily.py:266-268`). Mỗi mã chạy đủ chuỗi agent nên **job có thể mất nhiều
  phút** (chế độ mock của daily job chỉ có ở API, chưa có công tắc trên màn này).
- **Job không bao giờ tự đặt lệnh** — kết quả chỉ là kế hoạch + tham chiếu hàng đợi
  duyệt (`web/components/scanner/scanner-types.ts:2-4`).
- Bảng kết quả theo mã: *Cặp* (có badge "Mock" nếu job mock) · *Tín hiệu*
  (Buy/Overweight/Hold/Underweight/Sell) · *Lệnh đề xuất* (MUA/BÁN · khối lượng @ giá) ·
  *Dòng tiền* (mua `−`, bán `+`) · *Kết quả* (lý do nghiệp vụ — ví dụ lý do RiskGuard
  từ chối, hoặc "awaiting approval") · cột *Duyệt* → link "Xem hàng duyệt".
- **Một coin fail không dừng job** — coin khác vẫn chạy tiếp (`server/daily.py:212-240`).
- Job xong → dòng tổng kết "Job hoàn tất — N kế hoạch trong hàng đợi duyệt" + nút
  **"Xem trong Duyệt lệnh"**.
- Job treo: không đổi trạng thái > 15 phút → cảnh báo "kiểm tra log của server FastAPI"
  (`scan-panel.tsx:242-250`).
- **Job là state in-memory: khởi động lại server làm mất job** (poll trả 404 — UI có
  sẵn câu giải thích này; các dòng audit đã ghi vẫn còn).

### 3.4 Duyệt lệnh (`/approvals`)

**Mục đích:** cổng phê duyệt — *duyệt là con đường duy nhất đưa lệnh vào thực thi; từ
chối không đụng sàn* (`approvals-screen.tsx:3-5`).

**Banner cổng (đỉnh màn, đọc `/api/health` mỗi 15s):**
- Đỏ "RiskGuard đã dừng giao dịch hôm nay — mọi phê duyệt sẽ bị server từ chối (403).
  Việc mở lại halt chỉ do operator thực hiện phía server — màn này cố ý không có nút
  reset" (`gate-banner.tsx:67-84`).
- Vàng khi không đọc được health.

**Đọc một thẻ lệnh đang chờ** (`approval-card.tsx`):
- Tiêu đề: **ticker + chip MUA (xanh)/BÁN (đỏ)** + *Tín hiệu* + badge "Kế hoạch mô
  phỏng (mock)" nếu là plan demo. (Hợp đồng còn định nghĩa badge "Quá hạn duyệt"
  suy ra từ `expires_at`, nhưng **hiện chưa bao giờ hiển thị**: server chưa trả
  trường `expires_at` nên UI không có gì để render — `approval-card.tsx:30,73`
  chỉ vẽ khi trường có giá trị.)
- Dòng meta: *Đề xuất lúc* + *ID* (`ap_xxxx`) + *job d_xxxx* sinh ra nó. Lưu ý:
  **không có dòng "Hạn duyệt"** — hợp đồng quy định hạn 24h
  (`server/approvals.py:38`) nhưng phần trả `expires_at` về client chưa được
  implement; item quá 24h vẫn đứng nguyên trong hàng đợi, chỉ khi bấm hành động
  vào nó server mới trả 410 `expired`.
- 4 ô số liệu: **Khối lượng** (ví dụ `0.0012 BTC`), **Giá ước tính**, **Giá trị lệnh**
  (mua có dấu `−` = tiền ra; bán `+` = tiền vào), **Thị trường** (Spot, kèm "đòn bẩy
  N×" nếu có).
- Hàng cuối: badge *"Lập kế hoạch ở chế độ LIVE/mô phỏng"* + **"Lý do: …"** — đây là
  nơi đọc **lý do RiskGuard** gắn với kế hoạch (ví dụ "position BTC-USD would reach
  85.22% of equity …"). Thẻ mock có thêm cảnh báo vàng: dữ liệu tổng hợp không thể
  thành lệnh thật, chỉ có thể từ chối.

**Quy trình DUYỆT — hai bước bắt buộc** (hành động không thể hoàn tác):
1. Bấm **"Duyệt lệnh"** trên thẻ → mở hộp thoại **"Xác nhận duyệt lệnh ap_xxxx?"**
   hiển thị lại đầy đủ: cặp lệnh + phía, khối lượng, giá ước tính, giá trị, thời điểm
   đề xuất (`approve-dialog.tsx:44-93`). Khi cổng đang LIVE, thoại hiện cảnh báo đỏ
   "Lệnh thật có thể được gửi lên sàn — kiểm tra kỹ số liệu trước khi duyệt" và nút
   xác nhận đổi màu đỏ.
2. Bấm **"Xác nhận duyệt"** → nút chuyển "Đang duyệt…" (lệnh live có thể mất vài giây —
   đừng tắt trang). Kết quả hiện banner:
   - Xanh: "Đã duyệt ap_xxxx. Mã lệnh trên sàn: <order_id>" (live) hoặc "Chế độ thực
     thi: mô phỏng (dry)" (dry-fill).
   - **Cờ đỏ đặc biệt:** nếu server trả `warning` nghĩa là *"order ĐÃ được đặt nhưng
     ghi audit thất bại"* → banner đỏ "Tuyệt đối không duyệt lại kế hoạch này — lệnh có
     thể đã tồn tại trên sàn" (`approvals-screen.tsx:70-92`). Retry ở đây = lệnh trùng,
     mất tiền thật.

Server làm gì sau khi bạn xác nhận (theo thứ tự — `server/approvals.py:242-334`):
kiểm tra cổng FR5 → kiểm tra halt → khoá item (chống duyệt trùng từ tab khác) → dựng
lại kế hoạch → RiskGuard **kiểm tra lại lần nữa ngay trước khi gửi** → gọi
`execute_order` → ghi audit cả hai tầng (server trail + engine log). Dry-fill (cổng
config mở nhưng env LIVE đóng) ghi sổ mô phỏng, `order_id` null.

**Quy trình TỪ CHỐI** (`reject-dialog.tsx`): bấm "Từ chối" → hộp thoại một bước với ô
*Lý do (tùy chọn — được ghi vào audit)* → "Từ chối kế hoạch". Không có lệnh nào được
gửi; lý do lưu vào audit (`approval_denied`). Từ chối **luôn hợp lệ** — kể cả khi cổng
đóng, kể cả với plan mock. **Ngoại lệ duy nhất:** item quá hạn 24h **không từ chối
được cũng như không duyệt được** — cả hai hành động đi qua cùng một kiểm tra
`_pending_item` và trả 410 `expired` (`server/approvals.py:344-346, 103-105`); item đó
sẽ nằm mãi trong hàng đợi, đợi job quét sau sinh kế hoạch mới thay thế.

**Các lỗi có thể gặp khi duyệt** (UI dịch sẵn sang tiếng Việt — `errors.ts`):

| Mã | Nghĩa | Việc cần làm |
|---|---|---|
| `412 gate_closed` | **Cổng FR5 (exec_live) đang đóng.** Duyệt cần `exec_live=True` trong config; item giữ nguyên "Chờ duyệt". | Đây là hành vi bình thường trên cài mới. Mở cổng theo đúng quy trình **§4.8** — chú ý: nửa config **không** đặt được qua `.env`. Trước khi duyệt lại, kiểm tra mode bar/thẻ Cổng thực thi (§4.8 bước 3). |
| `403 risk_halted` | RiskGuard đã dừng giao dịch hôm nay (kèm lý do). | Xem §4.4 — halt tự hết khi sang ngày UTC mới; mở lại sớm là hành động phía server (lệnh ở §4.4). |
| `409 conflict` | Item đã được xử lý nơi khác (tab khác đã duyệt/từ chối) hoặc không dựng được kế hoạch. | Danh sách tự làm mới. |
| `409 mock_not_executable` | Kế hoạch sinh từ job mock — dữ liệu tổng hợp không thể thành lệnh thật. | Từ chối kế hoạch này. |
| `410 expired` | Quá hạn duyệt 24 giờ. | Item này không duyệt được **và cũng không từ chối được** — đợi job quét sau sinh kế hoạch mới. |
| `502 execute_failed` | Sàn từ chối/lỗi sau khi thực thi bắt đầu (thiếu credentials, giá dưới minimum, lỗi mạng…). | Đọc `details.reason` trong thông báo; kiểm tra key sàn; xem audit. |
| `503 audit_log_unreadable` | Audit log không đọc được — server từ chối fail-closed. | Quy trình hồi phục ở **§4.9**. |

**"Đã xử lý gần đây"** (`resolved-list.tsx`): danh sách kế hoạch đã resolve — badge
trạng thái *Chờ duyệt / Đã duyệt / Đã từ chối / Đã thực thi / Thực thi lỗi / Hết hạn*,
kèm `order` id nếu live, chú thích "dry-fill (chưa gửi sàn)" nếu dry, và lý do đã lưu.
Trong thực tế **badge "Hết hạn" không bao giờ xuất hiện**: `expired` không phải trạng
thái server lưu — item quá hạn ở lại `pending` trong hàng đợi và mọi hành động vào nó
trả 410 (`server/approvals.py:103-105`), nên nó không bao giờ lọt xuống danh sách này.

### 3.5 Cấu hình (`/settings`)

Đọc/ghi cài đặt engine qua `GET/PUT /api/settings`. Thay đổi **áp dụng cho phiên phân
tích mới** (phiên đang chạy không thấy); lưu vào `.env` ngoài việc áp in-memory
(`server/settings.py:219-220`).

**Ba nhóm cài đặt có thể sửa** (`web/components/settings/types.ts:91-137`):

- **LLM & Mô hình** — nhà cung cấp (`llm_provider`: openai/google/anthropic hoặc
  endpoint tương thích), mô hình suy luận sâu/nhanh, số vòng tranh luận nghiên cứu/rủi
  ro (nhiều vòng = chậm + tốn token), temperature, giới hạn token, ngôn ngữ đầu ra…
- **Giới hạn rủi ro** — các ngưỡng **RiskGuard** dùng để chặn lệnh. Tác động cụ thể:
  - *Lỗ tối đa trong ngày* (`risk_max_daily_loss_pct`): chạm ngưỡng → dừng giao dịch
    cả ngày (halt).
  - *Tỷ trọng vị thế tối đa mỗi tài sản* (`risk_max_position_pct_per_asset`): kế hoạch
    mua làm một mã vượt ngưỡng % equity → bị từ chối ở bước lập kế hoạch (lý do hiện
    trên thẻ duyệt và trong audit `rejected_by_risk`).
  - *Tổng mức phơi nhiễm tối đa* (`risk_max_total_exposure_pct`): chặn khi tổng giá trị
    vị thế quá lớn so với equity.
  - *Giới hạn lệnh lỗ liên tiếp* (`risk_max_consecutive_loss_count`): đủ N lỗ liên
    tiếp (đếm xuyên mã) → halt trong ngày.
  - *Đòn bẩy phái sinh tối đa / Phơi nhiễm phái sinh tối đa* (0 = chặn hẳn phái sinh).
  - Lưu ý: giá trị mới chỉ áp cho guard được dựng **sau** khi lưu (`types.ts:120`).
- **Thực thi & Sàn** — `exec_exchange_id` (ví dụ `binance`), đồng tiền báo giá
  (`exec_quote_currency`, ví dụ `USDT`), công tắc *Chế độ sandbox (testnet)* — chuyển
  kết nối sang testnet của sàn, hướng an toàn.

**Các thẻ chỉ-đọc:**
- *Khóa API* — trạng thái key (mục §2.2); không sửa được từ UI.
- *Cổng thực thi (chỉ đọc)* — hiển thị **cả 6 hàng cổng MỞ/ĐÓNG** — 3 cặp cổng, mỗi
  cặp gồm nửa config và nửa biến môi trường: *LIVE* (`exec_live` /
  `TRADINGAGENTS_EXEC_LIVE`), *Tự động duyệt* (`exec_auto_confirm` / env), *Phái sinh*
  (`exec_derivatives` / env) — `server/settings.py:154-161`. Thẻ cũng hiển thị "Chế độ
  thực thi hiệu lực": LIVE — LỆNH THẬT hoặc "Mô phỏng (dry)". Các cổng cố ý vắng khỏi
  API ghi — **không thể bật LIVE từ UI** (`gates-card.tsx:32-35`).
- *Đường dẫn* — nơi engine đọc/ghi (thư mục kết quả, cache, nhật ký quyết định, nhật
  ký audit thực thi).

**Cách lưu:** sửa vài ô → thanh **Save bar** xuất hiện, chỉ gửi các key bạn đã sửa →
lỗi 422 (kiểu dữ liệu sai) được map về đúng ô. Có badge "Dữ liệu cũ" khi poll lỗi;
nếu server có dữ liệu mới hơn bản đang xem, hiện banner vàng với nút "Nạp dữ liệu máy
chủ" — thay đổi chưa lưu của bạn không bị ghi đè.

> Đã biết (cosmetic): **toàn bộ chuỗi tiếng Việt do `web/components/settings/
> settings-screen.tsx` tự render đang bị lỗi mã hoá (mojibake)** — không chỉ tiêu
> đề/mô tả mà cả các nút và nhãn mà chính mục này gọi tên: badge "Dữ liệu cũ" hiện
> thành `Dá»¯ liá»‡u cÅ©`, nút "Làm mới" thành `LÃ m má»›i`, nút "Nạp dữ liệu máy chủ"
> thành `Náº¡p dá»¯ liá»‡u mÃ¡y chá»§` (đã kiểm byte trên đĩa). Chức năng không ảnh
> hưởng; khi đọc hướng dẫn, khớp các affordance theo vị trí + hình nút chứ không theo
> chữ trên màn.

### 3.6 Lịch sử & Audit (`/audit`)

Ba section, đều chỉ-đọc, mỗi section có badge **"Dữ liệu cũ"** + thời điểm cập nhật +
nút làm riêng (`web/components/audit/`):

1. **Lịch sử quyết định** (`GET /api/history`): các quyết định trong decision log, ghép
   với tệp run trên đĩa. Bộ lọc: *Ticker*, ô tick *"Chỉ quyết định chưa settle"* —
   quyết định "Chưa settle" chưa thể chấm điểm (chưa đủ thời gian nắm giữ). Nút "Tải
   thêm 25" khi còn. Stale sau 60s.
2. **Nhật ký audit** (`GET /api/audit`, stale 10s): nhật ký kiểm soát thực thi — *mọi
   quyết định RiskGuard, lệnh và hành động duyệt đều nằm ở đây*. Bộ lọc: Ticker,
   Hành động (Mua/Bán/Không đặt lệnh/RiskGuard từ chối/Từ chối lúc thực thi/Thực thi
   bị chặn/Chờ duyệt/Đã duyệt/Từ chối duyệt), Giai đoạn (Lập kế hoạch `plan` / Thực thi
   `execute`), Từ ngày/Đến ngày. Phân trang 25 dòng. Cột: Thời điểm, Ticker, Hành động
   (badge + `#order_id`), Khối lượng, Giá ước tính, Chế độ (badge "LIVE — lệnh thật" /
   "Mô phỏng (dry)" + Đã xác nhận/Chưa xác nhận), Lý do. Dòng điều khiển đặc biệt hiển
   thị vàng: **"HALT — dừng giao dịch trong ngày"** / "Mở lại giao dịch (halt_reset)".
   Banner đỏ khi halted hôm nay, kèm nhắc: "Việc mở lại halt chỉ thực hiện được trên
   máy chủ — cố ý không có nút reset trên UI".
3. **Backtest** (`GET /api/backtest`, stale 60s): tổng kết các lần sweep backtest —
   quyết định quá khứ chấm điểm theo giá sau đó: *Số quyết định, Tỉ lệ trúng (hit rate),
   Alpha trung bình* theo từng rating. Bấm "Xem chi tiết" để bung entry thật của run.
   Sweep chạy từ CLI (không phải từ UI): ví dụ `tradingagents backtest NVDA,AAPL --start
   2026-07-01 --end 2026-09-30`.

---

## 4. Tình huống hay gặp

### 4.1 Backend chưa chạy

Triệu chứng (nhiều nơi cùng báo): mode bar → **"KHÔNG KẾT NỐI"** vàng; các panel →
"Không đọc được dữ liệu (network_error)"; màn Duyệt lệnh → "Không tải được hàng đợi phê
duyệt" **kèm sẵn lệnh khởi động**:
`.venv\Scripts\python.exe -m uvicorn server.main:app --port 8000`
(`approvals-screen.tsx:294-303`). Xử lý: bật backend theo §2.1, quay lại bấm "Thử lại".

### 4.2 409 — phiên/job đang chạy

Pipeline và Scanner **dùng chung một worker** — một lúc chỉ chạy được một thứ
(`server/runs.py:426-434`, `server/daily.py:280-288`). Bấm "Chạy phân tích" hoặc "Quét
ngay" khi có run/job active → lỗi 409 dạng "another run is already active" kèm
`run_id` (hoặc "another daily job is already active" kèm `job_id`). Xử lý:
- Ở Pipeline: lỗi kèm nút **"Theo dõi phiên đang chạy (…)"** — bấm để nối vào phiên đó.
- Ở Scanner: đợi job hiện tại xong (bảng kết quả điền dần per-coin) rồi quét lại.
- Một run live có thể kéo dài hàng chục phút; v1 không có nút huỷ.

### 4.3 Lệnh bị RiskGuard chặn — xem ở đâu?

RiskGuard chặn ở 2 thời điểm (lập kế hoạch, hoặc kiểm tra lại ngay trước khi gửi):
1. **Trong kết quả quét** (màn Scanner): cột *Kết quả* mang lý do, ví dụ
   "PermissionError: risk guard halted trading: 3 consecutive losing trades reached the
   limit 3…" — coin đó không sinh kế hoạch.
2. **Trong audit** (màn Lịch sử & Audit → Nhật ký audit): lọc *Hành động = "RiskGuard
   từ chối (rejected_by_risk)"* (hoặc "Từ chối lúc thực thi" / "Thực thi bị chặn"),
   cột Lý do chi tiết, ví dụ "position BTC-USD would reach 85.22% of equity …".
3. **Trên thẻ duyệt**: hàng "Lý do: …" dưới cùng thẻ kế hoạch.
4. Kế hoạch đã duyệt mà guard từ chối lần cuối → item vào "Đã xử lý gần đây" với badge
   *Thực thi lỗi* + lý do.

### 4.4 Halt trong ngày (RiskGuard dừng giao dịch)

Banner đỏ xuất hiện ở Tổng quan, Duyệt lệnh, và Nhật ký audit. Hệ quả: **mọi approve trả
403 risk_halted**. Hai đường hết halt:

1. **Tự hết khi sang ngày UTC mới** — halt gắn với ngày: sang ngày kế tiếp không cần
   làm gì, giao dịch mở lại tự động (`tradingagents/execution/risk.py:24-25`).
2. **Mở lại sớm (reset)**: v1 **không có lệnh CLI và không có endpoint API nào** —
   seam duy nhất là một method Python chạy trên máy chủ (`risk.py:591-596`), ghi thêm
   dòng `halt_reset` vào audit log (append-only, không xoá gì):

   ```powershell
   .venv\Scripts\python.exe -c "from tradingagents.execution.risk import RiskGuard; RiskGuard().reset_halt(r'C:\Users\<ten_ban>\.tradingagents\execution\audit.jsonl')"
   ```

   Đường dẫn chính xác xem ở màn **Cấu hình → thẻ Đường dẫn → "Nhật ký audit thực thi"**
   (mặc định `~/.tradingagents/execution/audit.jsonl` — `default_config.py:207`).
   Chỉ chủ tài khoản thực hiện; snippet ở trên đã được kiểm chứng ở mức import
   (chưa chạy reset thật — việc đó ghi vào audit log thật của bạn).

### 4.5 Dữ liệu cũ (stale)

Mỗi panel tự poll theo chu kỳ riêng. Khi poll gần nhất lỗi nhưng đã từng có dữ liệu,
UI **giữ dữ liệu cũ + badge "Dữ liệu cũ" + dòng đỏ "Lần poll gần nhất lỗi (code) — đang
hiển thị dữ liệu cũ"** — không bao giờ xoá trắng màn hình. Đây là hành vi chủ đích
(`web/components/overview/use-panel.ts:1-7`). Xử lý: sửa nguyên nhân (thường là backend
đứt) rồi bấm **Làm mới**; badge tự biến mất khi poll thành công.

### 4.6 Job/phiên treo

- Scanner: job không đổi trạng thái > 15 phút → cảnh báo vàng, UI vẫn tiếp tục theo dõi.
- Pipeline: không có sự kiện mới > 60s trong khi vẫn running → badge "Đang treo?".
- v1 **không có nút huỷ**: đợi thêm, kiểm tra log cửa sổ uvicorn, hoặc (cùng cực)
  khởi động lại server. Restart làm mất run/job in-memory (poll → 404) nhưng **các dòng
  audit đã ghi và item hàng đợi đã enqueue vẫn còn** để đối soát.

### 4.7 Run Live thất bại vì thiếu LLM key

Run live dựng graph xong mới gọi LLM — thiếu key → phiên kết thúc `run_failed` với lỗi
trong thẻ kết quả. Màn Cấu hình (thẻ Khóa API) và `/api/health` cho biết key đã cấu
hình chưa. Phân tích Mock không bao giờ dính lỗi này.

### 4.8 Duyệt trả 412 `gate_closed` — mở cổng FR5 đúng cách

**Triệu chứng:** bấm "Duyệt lệnh" → hộp thoại báo lỗi "Cổng FR5 (exec_live) đang
đóng…"; item vẫn "Chờ duyệt". **Đây là trạng thái mặc định của cài mới** (`exec_live:
False` — `default_config.py:206`) — không phải lỗi.

**Quy trình mở cổng** (operator tự chịu trách nhiệm với từng bước):

1. **Mở nửa config `exec_live`** — đường duy nhất là sửa code, vì key này cố ý vắng
   khỏi bảng biến môi trường (`default_config.py:33-36`) và bị PUT settings từ chối
   (403 `forbidden_key` — `server/settings.py:209-211`):
   - Sửa `tradingagents/default_config.py:206`: `"exec_live": False` →
     `"exec_live": True`;
   - hoặc để một script chạy **trong cùng process server** gọi
     `set_config({"exec_live": True})` (gọi từ script riêng ngoài server không tác
     động tới process đang chạy — config là state của process).
   - Khởi động lại backend nếu bạn sửa file mặc định.
2. **Kiểm tra nửa môi trường trước khi duyệt** — mở màn Cấu hình → thẻ "Cổng thực thi
   (chỉ đọc)": hàng `LIVE · cấu hình exec_live` phải là **MỞ**. Nhìn luôn hàng
   `LIVE · biến môi trường TRADINGAGENTS_EXEC_LIVE`:
   - Nếu env **ĐÓNG**: lần duyệt tiếp theo sẽ là **dry-fill** (ghi sổ mô phỏng) —
     an toàn để tập thao tác.
   - Nếu env **ĐÃ MỞ sẵn** (ai đó bật trong `.env` từ trước): lần duyệt tiếp theo
     là **LỆNH THẬT**. Mode bar sẽ chuyển sang "LIVE — LỆNH THẬT" sau khi restart
     backend — **đọc mode bar ngay trước khi bấm Duyệt**; đây là cảnh báo bắt buộc:
     việc arm nửa config biến lần duyệt kế tiếp thành lệnh thật, không phải dry-fill.
3. **Duyệt lại** kế hoạch — bây giờ server chấp nhận và đi qua RiskGuard như §3.4.

**Cách thu hồi:** đặt lại `"exec_live": False` và restart backend. Nửa env tắt bằng
cách bỏ/sửa `TRADINGAGENTS_EXEC_LIVE` trong `.env` rồi restart (env đọc lúc gọi —
`bridge.py:527-539`, một số đường đọc có thể nhận giá trị mới không cần restart, nhưng
đừng dựa vào điều đó).

### 4.9 Lỗi 503 `audit_log_unreadable` — hồi phục

**Triệu chứng:** màn Audit/Lịch sử báo lỗi 503 với mã này; màn Duyệt lệnh từ chối mọi
hành động (`server/approvals.py:252-254` fail-closed — server **cố ý** dừng khi không
đọc được log, vì RiskGuard phải đọc audit log để biết trạng thái halt/lỗ liên tiếp
trước khi cho phép lệnh).

**Quy trình hồi phục:**

1. Xác định đường dẫn file: màn **Cấu hình → thẻ Đường dẫn → "Nhật ký audit thực thi"**
   (mặc định `~/.tradingagents/execution/audit.jsonl` — `default_config.py:207`).
2. Kiểm tra file: còn tồn tại không, tiến trình nào đang khoá nó (trên Windows: một
   editor/backup tool giữ file mở là đủ lỗi), quyền đọc của process backend.
3. Nếu file bị mở sai encoding/bị hỏng một phần — **không tự sửa file audit** (append-
   only, là bằng chứng đối soát); sửa quyền/trình đọc, không đụng nội dung.
4. Bấm "Thử lại" trên UI. Log đọc được lại → mọi thứ tự phục hồi (không mất dữ liệu —
   chỉ là lỗi đọc).

### 4.10 Server sập giữa chừng lúc đang duyệt

**Nguy cơ thật:** nếu backend chết **sau** khi item đã được claim "approved" nhưng
**trước/trong** lúc gửi lệnh live, item có thể kẹt ở trạng thái `approved` trong
`server/data/approvals.jsonl` — **và lệnh có thể đã được gửi lên sàn**. Restart server
**không tự hồi** trạng thái này (`server/data/approvals.jsonl` là bền — hợp đồng §11.1).

**Phải làm ngay:**

1. **Đối soát trước mọi thao tác khác** — xác định lệnh đã đi chưa:
   `GET /api/audit?ticker=…&phase=execute` (màn Lịch sử & Audit → Nhật ký audit, lọc
   Giai đoạn = "Thực thi") và đối chiếu order id trên giao dịch của sàn.
2. **Tuyệt đối không bấm Duyệt lại** item đó — nếu lệnh đã tồn tại trên sàn, retry là
   lệnh trùng, mất tiền thật (cùng logic cờ đỏ "WAS placed" ở §3.4).
3. Lệnh chưa đi (không thấy dòng execute, sàn không có order) → item kẹt `approved`
   cần xử lý ở tầng file/server — nhờ kỹ thuật can thiệp `server/data/approvals.jsonl`
   có ghi chú đối soát trong audit; không có UI/API tự hồi trong v1.
4. Bản chất preventive: duyệt LIVE khi hệ thống/network không ổn định là rủi ro vận
   hành — làm khi cả backend và sàn đang ổn định.

---

## 5. Kiểm chứng đã qua + Giới hạn

### 5.1 Đã kiểm chứng trực tiếp trong phiên viết tài liệu này (2026-10-01)

Các lệnh dưới đây **đã chạy trên chính máy này**, kết quả nguyên văn:

- `.venv\Scripts\python.exe -m pytest -q tests/test_server_api.py` → **48 passed in 2.13s**
  (suite unit của server API — không mạng, không LLM; con số 28 test trong
  docs/ui-implementation.md đã cũ, suite đã lớn lên).
- Khởi động backend đúng lệnh §2.1 (`.venv\Scripts\python.exe -m uvicorn
  server.main:app --port 8000`) → `GET /api/health` trả **HTTP 200**:
  `exec_mode:"dry"`, `halted_today:false`, `pending_approvals:0`,
  `llm_key_configured` toàn false, `llm_wire_protocol:"chat"`.
- `POST /api/runs` mode mock (BTC-USD, 2026-09-30, crypto) → **201**, run
  `r_20260930_182318_btc-usd`, analysts `["market","social","news"]`; sau ~7s
  `GET /api/runs/{id}` → `status:"completed"`, `signal:"Underweight"`, replay từ
  `full_states_log_2026-09-29.json` (chứng tỏ đường replay file thật hoạt động).
- `POST /api/runs` **lặp lại khi run đang chạy** → **409** `conflict`, message
  "another run is already active", `details.run_id` đúng phiên đang chạy (kịch bản §4.2).
- `GET /api/approvals` → 200, hàng đợi rỗng; `GET /api/portfolio` → **200** (chế độ dry).
- Đọc file `web/components/settings/settings-screen.tsx` + kiểm byte trên đĩa →
  xác nhận **mojibake** thực có (chuỗi tiếng Việt double-encoded) — ghi ở §3.5.

Backend đã được tắt sạch sau khi kiểm chứng.

### 5.2 Đã kiểm chứng theo báo cáo triển khai (không chạy lại ở đây)

Theo docs/ui-implementation.md §5.1 — các lần smoke trong **phiên triển khai**, repo
không giữ lưu vết (log/screenshot) nên không đối chiếu lại được từ repo: build/lint/
typecheck frontend sạch; smoke 2 server song song (health, /, /pipeline, /approvals
đều 200); SSE mock replay 21 events đủ 11 loại; M6 đã e2e qua browser với backend thật.

### 5.3 Mock vs thật — và khi nào cần LLM key

| Kịch bản | Cần gì | Dữ liệu từ đâu |
|---|---|---|
| Pipeline **Mock** | Không cần key gì, không cần mạng | Replay state log thật nếu có; không có → fixture tổng hợp (`server/runs.py:203-247`). Signal từ file replay, ví dụ "Hold". |
| Pipeline **Live** | LLM key của `llm_provider` | Graph LangGraph thật + data vendor; mất nhiều phút; ghi báo cáo thật. |
| Quét **"Quét ngay"** (hiện là live) | LLM key | Job per-coin như Pipeline Live; plan qua RiskGuard thật. Chế độ mock của daily job (API `POST /api/daily` `mode:"mock"`) dùng stub venue với **số demo cố định** — giá 83 078, tiền mặt 9 500 (`server/daily.py:45-46`); plan mock mang `mock:true`, **không bao giờ duyệt được** (409). |
| **Duyệt lệnh** trên cài mới | Chưa được — cổng FR5 đóng → mọi lần duyệt trả 412 `gate_closed` (§4.8). | — |
| **Duyệt lệnh** dry-fill | Nửa config đã arm + env tắt (trạng thái trung gian, §4.8) | Dry-fill ghi sổ mô phỏng, không chạm sàn. |
| **Duyệt lệnh** LIVE | LLM key (đã dùng khi lập kế hoạch) + `BINANCE_API_KEY`/`BINANCE_SECRET` | `sync_portfolio` đọc số dư sàn; lệnh gửi qua ccxt (testnet nếu `exec_sandbox=true`). |

### 5.4 Chưa kiểm chứng / giới hạn đã biết

- **Frontend không có test tự động** ngoài lint + typecheck + build
  (docs/ui-implementation.md §3) — các màn M1/M3/M5 chưa từng chạy runtime với dữ liệu
  thật theo báo cáo triển khai (§5.3 của báo cáo đó).
- **Nút "Quét ngay" chưa có lựa chọn mock** trên UI — luôn chạy live; muốn demo không
  tốn LLM phải gọi API trực tiếp.
- **Không có cách huỷ run/job** và **không có nút reset halt** trên UI — đều là chủ đích
  thiết kế v1.
- **Duyệt LIVE trên sàn thật chưa được kiểm chứng qua control panel.** Kiểm chứng gần
  nhất về đường thực thi nằm ở tầng engine (lệnh testnet đã PASS trong quá khứ, thuộc
  engine/CLI chứ không phải UI này). Với tiền thật: bật `exec_sandbox=true` (testnet)
  trước khi cân nhắc LIVE.
- **Bảo mật:** CSRF guard ≠ xác thực — mọi endpoint tin tưởng localhost; server bind
  `127.0.0.1` và **không được phép mở `--host` ra LAN** trước khi thêm token auth
  (chưa hiện thực — `server/main.py:7-13`).
- **Job/approval restart:** job quét mất khi restart server (in-memory); hàng đợi duyệt
  (`server/data/approvals.jsonl`) và audit trail (`server/data/audit.jsonl`) là bền —
  item claim "approved" khi server sập giữa chừng phải đối soát qua audit + sàn, không
  tự hồi (hợp đồng §11.1) — quy trình ứng phó ở §4.10.
- **Sao lưu:** chưa có cơ chế tự động cho `server/data/` — copy thủ công trước khi nâng
  cấp code.
- Thiếu sót trung thực của P&L (hiển thị sẵn trên UI): vị thế mở trước khi audit log
  bắt đầu không có giá vốn; "Đã chốt hôm nay" theo UTC, khác con số RiskGuard dùng
  (§3.1).

---

## Changelog

- **rev 2 (2026-10-01)** — sửa theo góp ý đọc thử:
  - Sửa chỉ dẫn sai về 412 `gate_closed`: `exec_live` không đặt được qua `.env` (vắng
    khỏi `_ENV_OVERRIDES`, mặc định False, PUT settings trả 403) — nửa config chỉ mở
    bằng code; nửa env `TRADINGAGENTS_EXEC_LIVE` mới qua `.env`. Thêm quy trình mở
    cổng đầy đủ ở §4.8 kèm cảnh báo kiểm tra mode bar trước khi duyệt lại (nửa env mở
    sẵn → lần duyệt kế là lệnh thật, không phải dry-fill).
  - Sửa mô tả SANDBOX ở §1.2/§1.3/§3.0: cài mới → mọi duyệt bị 412, item giữ nguyên
    pending; dry-fill chỉ là trạng thái trung gian sau khi arm nửa config.
  - Sửa mô tả thẻ duyệt: `expires_at`/dòng "Hạn duyệt"/badge "Quá hạn duyệt"/badge
    "Hết hạn" **chưa bao giờ hiển thị** — server chưa trả trường này (chưa implement
    so với hợp đồng §4); thêm ngoại lệ: item quá hạn không duyệt được cũng không từ
    chối được (410).
  - §4.4: bổ sung halt tự hết khi sang ngày UTC mới và lệnh reset chính xác (Python
    `RiskGuard().reset_halt(<exec_log_path>)` — v1 không có CLI/API nào).
  - §3.3: cảnh báo job quét luôn phân tích theo `asset_type="crypto"` — mã cổ phiếu
    trong watchlist sẽ bị phân tích thiếu fundamentals.
  - §3.5: thẻ Cổng thực thi hiển thị đủ 6 hàng (3 cặp cổng), không phải 2 nửa; mở rộng
    ghi chú mojibake (cả badge/nút, không chỉ tiêu đề).
  - §4 mới: §4.8 (412 gate_closed), §4.9 (503 audit_log_unreadable — hồi phục),
    §4.10 (server sập giữa chừng lúc duyệt — đối soát audit + sàn, không retry).
