---
title: Sổ tay vận hành Control Panel — TradingAgents
updated: 2026-11-09 (rev 3 — cập nhật hướng dẫn màn Cấu hình; changelog cuối tài liệu)
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

**Phạm vi mục này là job quét thủ công và hàng đợi duyệt.** Autoloop có đường
thực thi tự động riêng, như mô tả bên dưới.

- Pipeline phân tích (`POST /api/runs`) chỉ phân tích. Job quét (`POST /api/daily`)
  phân tích và lập kế hoạch Spot, nhưng không tự đặt lệnh; kế hoạch có thể giao dịch
  được đưa vào hàng đợi để người điều hành duyệt hoặc từ chối.
- **Auto Spot** (`auto_spot_enabled`) và **Auto Futures** (`auto_futures_enabled`) là
  hai công tắc độc lập trong **Cấu hình → Tự động → Chế độ tự động**, mặc định tắt trên cài
  mới. Thay đổi qua PUT cần xác nhận + lý do và được ghi audit; reset qua API đưa một
  công tắc về mặc định tắt. Trên màn, đổi công tắc rồi lưu kèm xác nhận + lý do.
  Tắt một thị trường chặn công việc phân tích, lập kế hoạch
  và vào lệnh mới của chế độ tự động đó; không tắt thị trường còn lại. Với autoloop
  và job quét, một call đang chạy không bị huỷ tức thời: backend kiểm tra lại trước
  các bước tiếp theo. Pipeline phân tích riêng `POST /api/runs` không bị hai công tắc
  này chi phối.
- **Nâng cấp từ cấu hình cũ:** nếu chưa có dòng mode mới, các giá trị cũ đã lưu
  `exec_auto_confirm=true`, `exec_derivatives=true`, `exec_live=true` chỉ bật Auto
  Futures; đổi `exec_derivatives` thành `false` chỉ bật Auto Spot. Nếu `exec_live`
  cũ thiếu hoặc không phải boolean `true`, cả hai mode đều tắt để tránh biến chạy
  dry thành giao dịch thật; `exec_auto_confirm` thiếu hoặc không phải `true` cũng
  tắt cả hai. Những dòng mode mới đã tồn tại được giữ nguyên, kể cả khi quyền LIVE
  cũ tắt; nếu chỉ có một dòng mode mới thì dòng còn thiếu được khởi tạo `false`,
  không suy ra từ cấu hình cũ. Kiểm tra từng công tắc trước khi chạy.
- Job `POST /api/daily` (kể cả mock) cần Auto Spot; nếu tắt, server trả 409
  `mode_closed`. Approve chỉ hỗ trợ kế hoạch Spot và cần Auto Spot; công tắc tắt
  hoặc authorization đã cũ trả 412 `mode_closed`, kế hoạch vẫn chờ và có thể từ chối.
  Plan mock không duyệt được; plan thị trường khác trả 409 `unsupported_market`.
- Autoloop chạy từng thị trường đang bật, theo thứ tự Spot rồi Futures, trong cùng
  giới hạn thời gian. Khi đọc được state scheduler, `enabled` được suy ra từ ít nhất
  một công tắc bật. Tắt cả hai công tắc để dừng scheduler; gửi `enabled: false` tới
  `/api/autoloop` không giữ scheduler tắt nếu một công tắc vẫn bật. Bật lại một mode
  cho phép scheduler chạy trở lại. **Lệnh managed của autoloop và approve đi tới
  venue đã cấu hình**, không cần bật riêng `exec_live`; kiểm tra Demo/production,
  credential và RiskGuard trước khi bật mode hoặc duyệt.
- Thư viện/CLI chạy độc lập có thể dùng `exec_live` để chọn dry/live; không dùng
  trạng thái đó làm bằng chứng lệnh managed sẽ được mô phỏng. `run_daily` tạo bridge
  với config tại lúc khởi tạo; việc tắt mode không huỷ tức thời call đang chạy.
- Tắt Auto Futures không tự đóng vị thế mở và không tắt cơ chế bảo vệ đã bật. Khi
  `risk_derivatives_profit_protection_enabled` là `true`, Futures protection và bracket
  recovery không cần Auto Futures hoặc `exec_live`. Chúng chỉ được phép mutate sau khi
  xác minh task thường `bracket:*`: venue khớp, account identity đã xác thực và khớp,
  đúng một durable position state liên kết chính xác symbol/side/entry/task, sổ vị thế
  venue hiện tại đầy đủ với đúng position cùng phía, contracts/entry/mark hợp lệ, rồi
  qua các kiểm tra BracketManager trong symbol operation lock. Entry-intent, unbound,
  missing/ambiguous, uncertain và emergency không vượt qua cổng này: chỉ đọc, cần đối
  soát thủ công hoặc deferred.

### 1.3 Luồng một ngày làm việc

```
1. CẤU HÌNH (/settings)      — kiểm tra key API, ngưỡng rủi ro, venue và các mode tự động
2. WATCHLIST/SCANNER (/scanner) — thêm/bớt mã, bấm "Quét ngay"
3. PIPELINE (/pipeline)       — (tùy chọn) chạy một phiên phân tích riêng cho 1 mã, đọc stream agent
4. HÀNG ĐỢI DUYỆT (/approvals) — kế hoạch lệnh từ bước 2 xuất hiện ở đây, kèm lý do RiskGuard
5. DUYỆT hoặc TỪ CHỐI          — duyệt: server kiểm tra Auto Spot và rủi ro rồi gửi tới venue đã cấu hình.
                                  Cài mới: Auto Spot tắt → trả 412 `mode_closed` (§4.8).
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

LLM API key được cấu hình trên màn **Cấu hình → Mô hình AI → Nhà cung cấp & mô hình LLM**:
chọn **Nhanh** hoặc **Sâu**, chọn provider, rồi nhập key vào ô write-only và bấm **Lưu khóa**.
Key được mã hóa bằng vault trước khi lưu PostgreSQL; API chỉ trả trạng thái và 4 ký tự
cuối. Có thể lưu key khác nhau cho cùng provider ở hai tầng.

Credential sàn vẫn là deployment secret trong `.env`:

1. Thêm/sửa credential Binance trong `.env`:
   - Binance Demo mặc định: `BINANCE_DEMO_API_KEY=...` và `BINANCE_DEMO_SECRET=...`.
   - Binance production: `BINANCE_API_KEY=...`, `BINANCE_SECRET=...` và
     `TRADINGAGENTS_EXEC_BASE_URL=https://api.binance.com`.
2. Không trộn cặp key giữa hai endpoint. Backend kiểm tra URL host và chỉ đọc đúng
   cặp credential tương ứng.
3. Khởi động lại backend sau khi đổi venue URL hoặc credential sàn.
4. Vào Settings để kiểm tra trạng thái provider/model và quyền chạy. Lưu API key LLM
   trong UI không cần ghi `.env` hay restart.

Chuyển Demo sang production chỉ đổi `TRADINGAGENTS_EXEC_BASE_URL` cùng cặp API key/secret
phù hợp rồi restart backend. Việc này không tự bật **Auto Spot** hay **Auto Futures**;
nhưng nếu một mode đã bật, lệnh managed sau restart có thể được gửi tới venue mới.
Hai mode được đổi trong **Cấu hình → Tự động → Chế độ tự động** với xác nhận + lý do.

Key nào cần khi nào? Pipeline Live cần credential LLM của tier đang dùng; thực thi cần
thêm đúng cặp credential sàn.

---

## 3. Từng màn hình

Sidebar có chỉ báo trạng thái tự động dẫn tới màn Cấu hình (`web/components/exec-status-chip.tsx`).

### 3.0 Chỉ báo trạng thái tự động

Sidebar đọc `gates_readonly` từ `GET /api/settings`: hiện **Tự động đang bật** nếu Auto
Spot hoặc Auto Futures bật, **Tự động đang tắt** nếu cả hai tắt; khi tải/lỗi, UI hiển
thị trạng thái riêng và cho phép thử lại. Chip dẫn tới nhóm **Tự động**, phần cấu hình mode.

Chip không xác định venue/account profile. Trường API `effective_exec_mode` hiện được
tính bằng bridge **không có managed authorization**, nên phản ánh `exec_live` của đường
standalone. Vì vậy **`dry` không chứng minh lệnh managed bị chặn hoặc mô phỏng**; approve
và autoloop có thể gửi lệnh tới venue đã cấu hình khi mode tương ứng bật.

Binance venue được chọn bằng `TRADINGAGENTS_EXEC_BASE_URL`: Demo là mặc định;
production cần URL và credentials production. Demo là lệnh live trên tài khoản Demo,
không phải paper/dry-run. Trước khi bật Auto Spot/Futures, xác minh profile venue,
credentials, RiskGuard và khả năng chịu rủi ro.

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
- **Auto Spot phải bật** trước khi tạo job, kể cả qua API ở chế độ mock. Nếu tắt
  trong lúc phân tích, worker kiểm tra lại trước khi lập kế hoạch.
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

**Mục đích:** phê duyệt kế hoạch do job quét tạo; từ chối không đụng sàn. Autoloop
có thể thực thi tự động theo chế độ thị trường, không đi qua hàng đợi này.

**Banner cổng (đỉnh màn, đọc `/api/health` mỗi 15s):**
- Đỏ "RiskGuard đã dừng giao dịch hôm nay — mọi phê duyệt sẽ bị server từ chối (403).
  Việc mở lại halt chỉ do operator thực hiện phía server — màn này cố ý không có nút
  reset" (`gate-banner.tsx:67-84`).
- Vàng khi không đọc được health.

Lưu ý: banner nói thao tác mở lại ở phía server và màn Duyệt lệnh không có nút reset;
điều khiển gỡ dừng nằm ở **Cấu hình → Tự động** (§4.4).

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
   đề xuất (`approve-dialog.tsx:44-93`). Lệnh được duyệt có thể được gửi tới venue
   đã cấu hình ngay cả khi chỉ báo standalone là dry; kiểm tra venue và số liệu.
2. Bấm **"Xác nhận duyệt"** → nút chuyển "Đang duyệt…" (lệnh live có thể mất vài giây —
   đừng tắt trang). Kết quả hiện banner:
   - Xanh: "Đã duyệt ap_xxxx. Mã lệnh trên sàn: <order_id>" khi gửi thành công
     tới venue đã cấu hình.
   - **Cờ đỏ đặc biệt:** nếu server trả `warning` nghĩa là *"order ĐÃ được đặt nhưng
     ghi audit thất bại"* → banner đỏ "Tuyệt đối không duyệt lại kế hoạch này — lệnh có
     thể đã tồn tại trên sàn" (`approvals-screen.tsx:70-92`). Retry ở đây = lệnh trùng,
     mất tiền thật.

Server chỉ hỗ trợ approve Spot; trước claim kiểm tra Auto Spot và halt, rồi dựng lại
kế hoạch, đồng bộ portfolio và chạy RiskGuard. Authorization được kiểm tra lại trước
khi gửi; nếu bị thu hồi, item trở lại `pending`. Đường managed gửi tới venue đã cấu
hình, không dựa vào `exec_live` và không thay thế bằng dry-fill.

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
| `412 mode_closed` | **Auto Spot tắt hoặc authorization của yêu cầu đã cũ.** Item vẫn "Chờ duyệt". | Kiểm tra Auto Spot và venue; chỉ bật lại với xác nhận + lý do khi đã sẵn sàng, hoặc từ chối item. |
| `409 unsupported_market` | Chỉ hỗ trợ approve Spot; item thị trường khác vẫn chờ. | Không thử gửi lại qua approve; từ chối item nếu phù hợp. |
| `403 risk_halted` | RiskGuard đã dừng giao dịch hôm nay (kèm lý do). | Xem §4.4 — halt tự hết khi sang ngày UTC mới; cân nhắc rủi ro trước khi chủ động gỡ sớm. |
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

Chọn một trong năm nhóm trên màn; phần đang chọn hiển thị các điều khiển tương ứng:

- **Tự động** — Auto Spot/Futures, STOP khẩn cấp, lịch và trạng thái autoloop.
  **Chạy ngay** có thể gửi lệnh tới venue đã cấu hình, không phải chạy thử mô phỏng.
  STOP chặn lệnh mới trong ngày UTC hiện tại; không đóng vị thế hoặc huỷ lệnh hiện hữu.
- **Mô hình AI** — provider và model cho tầng nhanh/sâu; mở **Cài đặt nâng cao** để
  chỉnh các tham số ít dùng như số vòng tranh luận, temperature, giới hạn token và ngôn
  ngữ đầu ra. Nhiều vòng tranh luận hơn có thể làm phiên chậm và tốn token hơn.
- **Rủi ro** — trần vận hành, bảo vệ vị thế và các giới hạn bổ sung. Mở các mục thu
  gọn để xem thêm các ngưỡng bảo vệ và nâng cao.
- **Sàn & nguồn dữ liệu** — ID sàn, đồng tiền báo giá, URL venue chỉ đọc, sandbox cho
  sàn khác Binance và cookie Reddit/X. Binance mặc định Demo; URL và cặp credential
  quyết định Demo/production.
- **Giao diện & nâng cao** — ngôn ngữ, chủ đề, các thông số ít dùng và **Đường dẫn
  hệ thống** chỉ đọc cho kết quả, cache và nhật ký. Ngôn ngữ/chủ đề được lưu trên
  trình duyệt này.

**Lưu theo từng phần**, không có một nút lưu chung cho cả màn:

- Các ô engine dùng `GET/PUT /api/settings`, lưu bằng **Lưu cài đặt** trong nhóm
  tương ứng. Thay đổi được áp in-memory và lưu PostgreSQL cho lần khởi động sau;
  `.env` không bị ghi lúc runtime. Chỉ các ô đã sửa trong nhóm đó được gửi đi;
  lỗi dữ liệu có thể được đánh dấu tại ô và nút lưu.
- Mode và trần rủi ro vận hành dùng `/api/operator-config`, với nút **Lưu** riêng
  cho mode và rủi ro. Đổi mode cần xác nhận và lý do. Trường rủi ro có trong cả hai
  nguồn chỉ được sửa qua operator; các giới hạn bổ sung khác dùng nút lưu engine riêng.
  Nếu không tải được cấu hình operator, thử lại trước khi chỉnh rủi ro.
- Khoảng cách autoloop dùng **Áp dụng** riêng; key LLM dùng **Lưu khóa** riêng qua
  `/api/llm-credentials`, cookie nguồn dữ liệu dùng **Lưu cookie** riêng qua
  `/api/data-sources`. Lưu cài đặt engine không lưu key hoặc cookie.
- Giá trị mặc định có thể được hiển thị làm gợi ý cho ô nhập, không phải nút tự đặt lại.
  Với override rủi ro đã đặt, nút xoá override đưa giá trị về mặc định engine; cần lưu
  hoặc hoàn tác các thay đổi operator chưa lưu trước khi xoá override. Đừng nhầm thao
  tác này với **Huỷ thay đổi**, vốn chỉ bỏ bản nháp chưa lưu.
- Khi đổi nhóm, kiểm tra và lưu hoặc hoàn tác bản nháp ở đúng phần. Nếu màn báo
  **Dữ liệu cũ**, làm mới và kiểm tra trạng thái trước khi thao tác. Nếu máy chủ có dữ
  liệu mới hơn, đọc cảnh báo trước khi nạp lại: **Nạp dữ liệu máy chủ** có thể bỏ các
  thay đổi engine chưa lưu và sẽ hỏi trước; phần operator có lựa chọn nạp dữ liệu mới
  và giữ bản nháp. Nếu chưa xác định được kết quả lưu, kiểm tra dữ liệu trước khi thử lại.

Các ngưỡng **RiskGuard** dùng để chặn lệnh. Tác động cụ thể:

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
  - *Rủi ro tối đa mỗi lệnh* (`risk_derivatives_max_risk_fraction`, mặc định 0.02) và
    *Ký quỹ tối đa mỗi lệnh* (`risk_derivatives_max_margin_fraction`, mặc định 0.5): trần
    cứng cho đề xuất của agent. Đề xuất vượt trần bị **từ chối**, không bị tự hạ xuống.
  - *Rủi ro/Ký quỹ mặc định mỗi lệnh* (`risk_derivatives_risk_fraction` 0.01,
    `risk_derivatives_margin_fraction` 0.25): chỉ dùng cho lệnh CLI/script không có đề xuất
    của agent. Autoloop luôn cần `FuturesExecutionProposal` từ Trader/Portfolio Manager.
  - *Tự khóa lợi nhuận Futures* (`risk_derivatives_profit_protection_enabled`): policy cho
    vị thế bền vững đã xác minh; mặc định bật. Tắt Auto Futures không tự đóng vị thế,
    nhưng không tắt cơ chế bảo vệ đã bật. Khi policy bật, position monitor và bracket
    recovery không cần Auto Futures hoặc `exec_live`; mutation chỉ được phép sau khi
    task thường `bracket:*` qua xác minh venue và account identity, đúng một durable
    position state liên kết chính xác symbol/side/entry/task, sổ vị thế hiện tại đầy đủ
    cùng phía, contracts/entry/mark hợp lệ và các kiểm tra BracketManager trong symbol
    operation lock. Entry-intent, unbound, missing/ambiguous, uncertain và emergency
    vẫn chỉ đọc, manual hoặc deferred. Khi điều kiện đóng, đối soát vị thế và bracket
    trên venue; policy không phải bảo đảm lợi nhuận.
  - Chính sách dùng `R = khoảng cách entry thực tế đến stop ban đầu`: bộ mặc định mới là
    `break_even_r` **0.5R** dời SL về mốc hòa vốn (đã cộng phí, xem dưới); `lock_profit_r`
    **1R** khóa tối thiểu 1R; từ `trail_start_r` **1.5R**, SL bám mark cách
    `trail_distance_r` **0.75R**; TP vượt qua được gia hạn từng `target_extension_r` 1R.
    Stop chỉ đi theo hướng giảm rủi ro, không bao giờ bị nới ngược. `min_stop_move_r`
    (mặc định 0.1R) bỏ qua các bước dời SL quá nhỏ để không huỷ/tạo conditional order mỗi
    vòng poll. Ba ngưỡng đầu **phải tăng dần** (`break_even_r` < `lock_profit_r` <
    `trail_start_r`); policy từ chối cấu hình vi phạm.
  - *Time stop* (`risk_derivatives_max_hold_hours`, mặc định 72 giờ; `0` = tắt): vị thế quá
    hạn mà chưa đạt ngưỡng `risk_derivatives_time_stop_min_r` (mặc định 1R) được xem xét
    đóng theo policy. Kết quả còn phụ thuộc funding, phí, điều kiện hoãn và phân tích vị thế;
    không nên giả định vị thế đã đóng chỉ vì hết hạn. Khi policy quyết định đóng, hệ thống
    xử lý đóng vị thế và huỷ các leg của bracket; cần đối soát trên venue. Thiếu `held_hours`
    (không xác định được thời điểm mở) thì time stop không kích hoạt.
  - *Hòa vốn dịch theo phí khứ hồi* (`risk_derivatives_round_trip_fee_rate`, mặc định
    0.001 = 0.1%): mỗi vị thế trả phí vào **và** ra, nên dời SL đúng về `entry` vẫn lỗ ròng
    đúng bằng phần phí đó. Vì vậy khi chạm `break_even_r`, policy đặt SL cao hơn entry một
    khoảng `round_trip_fee_rate` (long) hoặc thấp hơn tương ứng (short). `0` = không bù phí,
    SL hòa vốn đúng bằng entry như trước.
  - Bốn khoá điều chỉnh ngưỡng, ba khoá chi phí/thời gian ở trên cùng năm khoá của cơ chế
    vượt trần và trần heat dưới đây nằm trong **Rủi ro → Trần rủi ro vận hành**, lưu qua
    `/api/operator-config` (không phải cổng thực thi — không cần `confirm`):

    | Khoá | Mặc định | Ý nghĩa |
    |---|---|---|
    | `risk_derivatives_break_even_r` | 0.5 | Đạt mốc R này thì dời SL về hòa vốn đã cộng phí |
    | `risk_derivatives_lock_profit_r` | 1.0 | Đạt mốc R này thì SL khóa tối thiểu 1R |
    | `risk_derivatives_trail_start_r` | 1.5 | Từ mốc R này SL bắt đầu bám mark |
    | `risk_derivatives_trail_distance_r` | 0.75 | SL cách mark bao nhiêu R khi đang trailing |
    | `risk_derivatives_max_hold_hours` | 72.0 | Hạn giữ để xem xét time stop; `0` = tắt |
    | `risk_derivatives_time_stop_min_r` | 1.0 | Ngưỡng R tối thiểu dùng khi xét time stop |
    | `risk_derivatives_round_trip_fee_rate` | 0.001 | Phí khứ hồi dùng để nâng SL hòa vốn lên trên entry; `0` = không bù phí |
    | `risk_max_derivatives_exposure_hard_pct` | 0 | Trần cứng cho cơ chế vượt trần phái sinh: chỉ lệnh đạt đủ điều kiện override mới được đẩy tổng phơi nhiễm tới mức này; `0` = tắt |
    | `risk_derivatives_override_min_r` | 2.0 | R tối thiểu (lời mục tiêu / rủi ro stop) của đề xuất để đủ điều kiện vượt trần |
    | `risk_derivatives_override_max_risk_fraction` | 0.01 | Risk-at-stop tối đa cho lệnh vượt trần (0.01 = 1%), chặt hơn trần thường `risk_derivatives_max_risk_fraction` |
    | `risk_derivatives_override_max_per_day` | 0 | Ngân sách lệnh vượt trần mỗi ngày UTC; `0` = không bao giờ vượt trần |
    | `risk_max_open_heat_pct` | 0 | Trần tổng rủi ro đang mở (heat): tổng phần equity mất nếu mọi stop khớp cùng lúc; `0` = TẮT (khác các trần anh em), khuyến nghị 6 |

  - **Hai nấc trần phơi nhiễm và cơ chế vượt trần (soft-cap override).** Trần *Phơi nhiễm phái
    sinh tối đa* (`risk_max_derivatives_exposure_pct`, vận hành thường đặt 25%) vẫn là nấc mặc
    định; khi đặt `risk_max_derivatives_exposure_hard_pct` > 0 (khuyến nghị 35), một lệnh phái
    sinh bị từ chối vì vượt nấc thường có thể vẫn được duyệt nếu đáp ứng đủ **năm điều kiện tất
    định** — mọi điều kiện do executor tự đo, không hỏi LLM: kịch bản có confirmation trên cả
    hai khung 1h và 4h; R của đề xuất (lời mục tiêu / rủi ro stop) đạt
    `risk_derivatives_override_min_r`; risk-at-stop của lệnh gắt hơn
    `risk_derivatives_override_max_risk_fraction` (chặt hơn trần mỗi lệnh thường); funding
    không bất lợi; còn ngân sách trong ngày — tối đa `risk_derivatives_override_max_per_day`
    lệnh vượt trần mỗi ngày UTC. Lệnh được duyệt qua cơ chế này được đẩy tổng phơi nhiễm tới
    tối đa nấc cứng, và mỗi lần dùng ghi một sự kiện audit `derivatives_override_used`.
  - **Trần tổng rủi ro đang mở (heat).** `risk_max_open_heat_pct` chặn lệnh phái sinh mới khi
    tổng phần equity mất nếu mọi stop đang mở khớp cùng lúc (heat) vượt trần. Lưu ý ngữ nghĩa
    **khác các trần anh em**: `0` = **TẮT** (không phải chặn hết), > 0 = bật — khuyến nghị 6.
    Mặc định các khoá mở soft-cap override đều `0` (`hard_pct`, `override_max_per_day`), nên
    cơ chế tắt và hành vi không đổi so với trước; hai ngưỡng điều kiện `override_min_r` /
    `override_max_risk_fraction` chỉ có tác dụng khi cổng đã mở. Trần heat độc lập cũng mặc
    định `0` nên đang tắt.
  - Monitor chỉ mutate khi đọc được position mode và account đang **One-way**; Hedge Mode trả
    `hedge_mode_unsupported` vì Binance không nhận `reduceOnly` ở chế độ đó. Mark được đọc riêng
    từ endpoint mark price; mark cũ hơn `mark_max_age_seconds` (mặc định 120s) trả `mark_stale`
    và không thay lệnh. Trigger của conditional leg dùng `workingType=MARK_PRICE`, cùng nguồn giá
    với policy.
  - *Chốt lời nhiều tầng* (`risk_derivatives_scale_out_enabled`, mặc định tắt): khi bật, mỗi lệnh
    mới đặt **hai mức chốt theo R** (`scale_out_first_r`/`scale_out_second_r`) với tỷ lệ khối
    lượng **của vị thế ban đầu** (`scale_out_first_fraction`/`scale_out_second_fraction`, tổng
    phải nhỏ hơn 1). Phần còn lại là runner do trailing stop quản lý. Khi một tầng khớp, stop
    được thu khối lượng về đúng phần còn lại; các tầng khác **không** bị huỷ. Tắt (mặc định) thì
    dùng một TP duy nhất như trước. Đây là scale-out có chủ đích, khác với ladder do nhiều entry
    tích tụ.
  - Entry/khối lượng dùng fill thật từ venue khi có; monitor đọc lại Futures position/mark mỗi 30s.
    Replacement đặt leg mới, lưu ID, rồi mới hủy leg cũ; timeout/cancel lỗi giữ state để reconcile
    sau restart. Nếu DB có nhiều entry-state cùng mã/hướng trong khi Binance báo một net position,
    hệ thống fail-closed và không tự chia khối lượng.
  - Đây là cơ chế khóa lợi nhuận có điều kiện, không phải bảo đảm lợi nhuận: gap, slippage,
    liquidation, mất mạng/API hoặc venue outage vẫn có thể khiến fill khác mức stop.
  - Lưu ý: giá trị mới chỉ áp cho guard/monitor được dựng sau khi lưu (`types.ts:120`).
Chỉ báo sidebar chỉ cho biết có mode nào bật, không cho biết profile venue; trường API
dry/live standalone không quyết định đường managed (§3.0).

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
   Banner đỏ khi halted hôm nay. Thông điệp trên màn nói việc mở lại chỉ thực hiện
   được trên máy chủ và không có nút reset trên UI; thông điệp này đã cũ. Xem điều khiển
   gỡ dừng ở **Cấu hình → Tự động** (§4.4).
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
2. **Mở lại sớm (reset)**: chỉ operator thực hiện sau khi kiểm tra nguyên nhân dừng,
   venue và rủi ro; việc gỡ có thể cho phép lệnh mới theo cấu hình hiện tại và các kiểm
   tra rủi ro. Vào **Cấu hình → Tự động → STOP khẩn cấp**, kiểm tra trạng thái dừng,
   chọn **Gỡ dừng hôm nay** và xác nhận. Nếu không xác định được trạng thái, thử lại
   và xác minh trước khi gỡ. Màn Duyệt lệnh không có nút reset riêng. Backend ghi thêm
   dòng `halt_reset` vào audit log (append-only, không xoá gì).

   Đường dẫn audit chính xác xem ở **Cấu hình → Giao diện & nâng cao → Đường dẫn hệ
   thống → "Nhật ký audit thực thi"** (mặc định `~/.tradingagents/execution/audit.jsonl`
   — `default_config.py:207`).

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
trong thẻ kết quả. **Cấu hình → Mô hình AI** cho biết trạng thái key theo tầng/provider;
`/api/health` cũng cung cấp trạng thái key. Phân tích Mock không bao giờ dính lỗi này.

### 4.8 Duyệt trả 412 `mode_closed` — Auto Spot đang tắt

> Liên kết cũ có thể dùng tiêu đề `gate_closed`; mã lỗi và quy trình hiện hành ở dưới.

**Hướng dẫn hiện hành:** approve Spot cần Auto Spot, không cần bật riêng `exec_live`.
Nếu approve trả `412 mode_closed`, item vẫn "Chờ duyệt":

1. Kiểm tra Auto Spot trong **Cấu hình → Tự động → Chế độ tự động**. Chỉ bật lại với xác nhận
   + lý do sau khi đã kiểm tra venue Demo/production, credentials và rủi ro.
2. Duyệt lại hoặc từ chối item. Backend kiểm tra authorization, halt, venue preflight
   và RiskGuard; lệnh managed không được chuyển thành dry-fill khi authorization tắt.
3. Không dựa vào chỉ báo tự động hoặc trường dry/live standalone để xác định profile
   venue hay giả định rằng lệnh managed được mô phỏng.

Tắt Auto Futures không tự đóng vị thế. Khi policy bảo vệ bật, position monitor và bracket
recovery không cần Auto Futures hoặc `exec_live`; mutation chỉ được phép sau khi task thường
`bracket:*` qua đủ xác minh venue/account identity, đúng một durable position state liên kết
symbol/side/entry/task, position cùng phía đầy đủ với contracts/entry/mark hợp lệ và các
kiểm tra BracketManager trong symbol operation lock. Entry-intent, unbound,
missing/ambiguous, uncertain và emergency vẫn chỉ đọc, manual hoặc deferred.

### 4.9 Lỗi 503 `audit_log_unreadable` — hồi phục

**Triệu chứng:** màn Audit/Lịch sử báo lỗi 503 với mã này; màn Duyệt lệnh từ chối mọi
hành động (`server/approvals.py:252-254` fail-closed — server **cố ý** dừng khi không
đọc được log, vì RiskGuard phải đọc audit log để biết trạng thái halt/lỗ liên tiếp
trước khi cho phép lệnh).

**Quy trình hồi phục:**

1. Xác định đường dẫn file: màn **Cấu hình → Giao diện & nâng cao → Đường dẫn hệ thống →
   "Nhật ký audit thực thi"**
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
  từng ghi nhận **mojibake** trong phiên đó; không phải kết quả kiểm chứng phiên hiện tại.

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
- **Pipeline Live** | Credential LLM của tier đang dùng | Graph LangGraph thật + data vendor; mất nhiều phút; ghi báo cáo thật.
| Quét **"Quét ngay"** (hiện là live) | LLM key | Job per-coin như Pipeline Live; plan qua RiskGuard thật. Chế độ mock của daily job (API `POST /api/daily` `mode:"mock"`) dùng stub venue với **số demo cố định** — giá 83 078, tiền mặt 9 500 (`server/daily.py:45-46`); plan mock mang `mock:true`, **không bao giờ duyệt được** (409). |
| Duyệt lệnh trên cài mới | Chưa được — Auto Spot mặc định tắt (`412 mode_closed`). | — |
| Duyệt lệnh managed (Demo/production) | Credential LLM của tier + cặp Binance phù hợp với URL đang chọn: `BINANCE_DEMO_API_KEY`/`BINANCE_DEMO_SECRET` cho Demo, hoặc `BINANCE_API_KEY`/`BINANCE_SECRET` cho production | URL Binance chỉ nhận host Demo hoặc production đã cho phép; lệnh managed tới venue đã cấu hình. Demo là live trên tài khoản Demo. |

### 5.4 Chưa kiểm chứng / giới hạn đã biết

- **Frontend không có test tự động** ngoài lint + typecheck + build
  (docs/ui-implementation.md §3) — các màn M1/M3/M5 chưa từng chạy runtime với dữ liệu
  thật theo báo cáo triển khai (§5.3 của báo cáo đó).
- **Nút "Quét ngay" chưa có lựa chọn mock** trên UI — luôn chạy live; muốn demo không
  tốn LLM phải gọi API trực tiếp.
- **Không có cách huỷ run/job** trên UI. Màn Duyệt lệnh không có nút reset halt;
  thao tác gỡ dừng ở **Cấu hình → Tự động** được mô tả trong §4.4.
- **Live execution trên Binance Demo đã được kiểm tra end-to-end** bằng `python demo_flow_check.py`: spot fill + OCO bracket và futures fill + reduceOnly conditional legs; script dừng nếu API host không phải Binance Demo và dọn vị thế/orders sau test. Không chạy flow này với URL production.

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

> Các ghi chú rev 2 dưới đây ghi lại hành vi tại thời điểm viết; mô tả nửa env và
> gate cũ không còn là hướng dẫn vận hành hiện tại. Xem §1.2 và §4.8.

- **rev 3 (2026-11-09)** — cập nhật đường dẫn theo năm nhóm Cấu hình, phân biệt các
  phần lưu riêng và chỉ dẫn STOP/gỡ dừng; chỉnh mô tả time stop theo chính sách hiện có.
  Chưa kiểm chứng runtime trong lần sửa này.

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
