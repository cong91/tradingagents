# Web UI API Contract (`docs/ui-api-contract.md`)

updated: 2026-10-01 (rev 3: §4 approve fail-closed → rebuild plan + `execute_order` + audit hai tầng, bỏ policy dry-fill-on-gate-closed; §5 daily job có `mode: live|mock` — mock tái dùng cơ chế của `runs.py`, enqueue per-coin, không bao giờ tự thực thi; §6 mới GET /api/portfolio; approval item + `daily_job_id`/`expires_at`; endpoint 17→18 · rev 3.1: approve pipeline 4 bước ĐÃ IMPLEMENT — dead-end claim-only xoá, refusals 409-mock/409-rebuild trước claim nên item còn pending/reject được; item + `executed`/`mode`/`order_id` · rev 4: §5 POST /api/daily nhận `auto: true` + `top_n` — quét tự động 1-click, server tự sàng universe crypto (ccxt public, top theo khối lượng) + cổ phiếu Mỹ (yfinance.screen, fallback danh sách tĩnh) qua `tradingagents/dataflows/screening.py`, payload job thêm `asset_types` + `screening`; ứng viên stock chỉ advisory — có tín hiệu, không sinh lệnh; ĐÃ IMPLEMENT cả hai phía · rev 4.1: sàng lọc nâng cấp theo tiêu chí thực chiến đã research — universe 40/asset (crypto top khối lượng, cổ phiếu mạnh 52 tuần sort theo `fiftytwowkpercentchange`, cap floor $2B), metrics thêm RSI(14) + ATR(14)% (stockstats) + khoảng cách đỉnh 52 tuần + breakout 20 phiên kèm RVOL≥2 + dollar-volume floor 20 phiên (crypto $5M / stock $10M — gate loại bỏ, không chỉ trừ điểm) + RS vs benchmark (BTC-USD / ^GSPC); phạt đu đỉnh (RSI≥75, RVOL≥4, ATR>20%) · ĐÃ IMPLEMENT) · rev 5: §12 mới GET /api/performance — metrics trade replay từ engine audit log (fill phase=execute FIFO long/short per ticker; summary win/loss/expectancy, open_positions per lot mark qua price seam, brackets, recent_orders 50 dòng mới nhất; log thiếu/rỗng là empty state, không đọc được → 503 `audit_log_unreadable` như /api/audit); §13 mới autoloop + halt — GET/POST /api/autoloop (vòng lặp tự chạy theo interval 15–1440 phút, enable fail-closed 409 `gate_closed` qua cổng auto-confirm của engine: config literal + env flag), POST /api/autoloop/run-now (202), POST /api/halt + POST /api/halt/reset — kill switch không gate, line `halt`/`halt_reset` trong engine audit log mà RiskGuard của mọi process replay · ĐÃ IMPLEMENT cả hai phía · rev 6: đường futures trong autoloop — DerivativesExecutor qua 3 cổng (exec_derivatives config + env + confirm per-order của _handle_plan), leverage cap 3.0, bracket futures reduceOnly; gates() thêm 2 nửa exec_derivatives (observability) · ĐÃ IMPLEMENT cả hai phía · status: design + implementation song song (đã implement: §1–§4, §7–§9 — approve chạy đủ pipeline execute từ rev 3.1; thiết kế: §5 rev 4.1 đã implement auto mode; §6) · rev 7 (2026-10-05): **job/run/agent-log chuyển từ in-memory + file sang SQLite** (`server/db.py`, `job_store.py`, `run_store.py`; `server/data/control.db`, stdlib `sqlite3`, WAL) — §5 mới `GET /api/daily` (lịch sử + `active_job_id`) và `POST /api/daily/{id}/cancel`, `GET /api/daily/{id}` fallback DB; §3 mới `GET /api/runs` + `run_events` persist để replay run cũ + stream đọc từ DB; cả hai registry reconcile job/run mồ côi (`running` → `interrupted`) lúc khởi động nên không còn 409 vĩnh viễn; `status` thêm `cancelled`/`interrupted`; `error` cấp job. ĐÃ IMPLEMENT cả hai phía (UI: lịch sử + băng đang chạy + nút Dừng) — endpoint 24→26
audience: frontend + backend implementer của control-panel UI cho TradingAgents

Hợp đồng này bọc engine hiện có **mà không sửa mã lõi `tradingagents/`**. Mọi mapping
sang mã thật được trích `file:line` ở thời điểm viết. Biến thể so với rev 2 ghi ở từng
mục; danh sách thay đổi đầy đủ ở changelog cuối mục §11.

---

## 0. Conventions chung

- Base URL: `http://127.0.0.1:8000/api` (FastAPI bind localhost — `server/main.py:5`); web/ Next.js proxy `/api` → `127.0.0.1:8000` (`web/next.config.ts:7-9`).
- Content type: `application/json; charset=utf-8` (repo cố định UTF-8 — `.env.example`, `audit.py:4-6`).
- Timestamps: ISO-8601 UTC (`bridge.py:583` ghi audit bằng `datetime.now(timezone.utc).isoformat()`).
- Ngày: `YYYY-MM-DD`; engine từ chối ngày tương lai (`trading_graph.py:29-40`).
- Auth/CSRF: v1 không token (localhost only), nhưng **mọi phương thức đổi trạng thái (POST/PUT/DELETE) bắt buộc hai lớp chống CSRF** — một trang web lạ mở trong browser cùng máy có thể `fetch(..., {mode:'no-cors'})` vào localhost, và no-cors chỉ gửi được CORS-safelisted content types (`text/plain`, `application/x-www-form-urlencoded`, `multipart/form-data`), nên:
  1. `Content-Type: application/json` bắt buộc (body rỗng hợp lệ nhưng phải gửi `{}` với đúng content type); sai/thiếu → `415 unsupported_media_type`. Quy tắc áp cho mọi POST/PUT/DELETE trong tài liệu này.
  2. Kiểm tra Origin: `Origin` header tồn tại và không thuộc allowlist `{http://127.0.0.1:8000, http://localhost:8000, http://127.0.0.1:8420, http://localhost:8420, http://127.0.0.1:3000, http://localhost:3000}` → `403 csrf_rejected` (`server/main.py:26-33`); tương tự `Sec-Fetch-Site: cross-site` → `403`. GET và SSE không đổi trạng thái nên miễn trừ.
  - Nếu mở bind ra ngoài mạng sau này, bắt buộc thêm `Authorization: Bearer <token>` — chưa thiết kế trong bản này.
- Tất cả response (kể cả danh sách) mang `generated_at` để frontend đánh dấu **stale**.

### 0.1 Error shape thống nhất

```json
{
  "error": {
    "code": "not_found",
    "message": "run abc123 not found",
    "details": {}
  }
}
```

| HTTP | `code`                  | Khi nào |
|------|-------------------------|---------|
| 400  | `validation_error`      | body/query sai kiểu, ngày sai format, `asset_type` lạ |
| 403  | `risk_halted`           | RiskGuard đang halt hôm nay (`risk.py:163-173`) |
| 403  | `csrf_rejected`         | POST/PUT/DELETE với `Origin` lạ hoặc `Sec-Fetch-Site: cross-site` (§0) |
| 403  | `forbidden_key`         | PUT settings ghi key read-only/lạ (§1, `settings.py:211`) |
| 404  | `not_found`             | run/daily job/approval/backtest run/file không tồn tại |
| 409  | `conflict`              | run đang chạy, approval đã xử lý, duplicate watchlist, plan mock không duyệt được (§4) |
| 410  | `expired`               | approval quá hạn duyệt 24h (§4, `approvals.py:103-105`) |
| 412  | `gate_closed`           | hành động cần gate FR5 mở nhưng gate đóng (approve khi `exec_live` ≠ True — §4) |
| 415  | `unsupported_media_type`| POST/PUT/DELETE thiếu `Content-Type: application/json` (§0) |
| 422  | `coercion_error`        | giá trị không coerce được theo kiểu default (`default_config.py:60-80`) |
| 500  | `internal_error`        | lỗi không lường trước |
| 502  | `execute_failed`        | exchange từ chối/lỗi khi đặt lệnh thật qua approve (§4) |
| 503  | `audit_log_unreadable`  | `exec_log_path` tồn tại nhưng không đọc được |
| 503  | `exchange_unreachable`  | venue không đọc được balance/giá cho GET /api/portfolio (§6) |

### 0.2 Trạng thái frontend bắt buộc xử lý

Mỗi endpoint dưới đây ghi rõ 4 trạng thái. Định nghĩa chung:

- **loading** — request đang bay: skeleton/spinner.
- **empty** — 200 với danh sách rỗng: empty-state riêng, không coi là lỗi.
- **error** — 4xx/5xx/503 hoặc network fail: hiển thị `error.message`, có nút retry.
- **stale** — response cũ hơn `stale_after` giây hoặc `generated_at` khác lần refresh gần nhất: badge "cũ", không tự ghi đè UI khi user đang tương tác.

---

## 1. Settings — `GET /api/settings`, `PUT /api/settings`

Nguồn: `_ENV_OVERRIDES` đọc được từ `default_config.py`; `exec_base_url` được thêm vào GET payload nhưng read-only qua PUT.

Quy tắc bất di bất dịch:

1. **Không bao giờ trả nguyên văn API key/secret.** Mọi key có tên khớp `*_API_KEY` / `*_SECRET` chỉ trả `masked` (4 ký tự cuối) + `configured`. Audit của bridge cũng redact credential (`bridge.py:619-624`).
2. **Các env-gate FR5/FR-S2/FR-D là read-only**: `TRADINGAGENTS_EXEC_LIVE`, `TRADINGAGENTS_EXEC_AUTO_CONFIRM`, `TRADINGAGENTS_EXEC_DERIVATIVES` và config `exec_live`/`exec_auto_confirm`/`exec_derivatives` KHÔNG ghi được qua API. Chúng cố tình vắng khỏi `_ENV_OVERRIDES` (`default_config.py:33-36, 235-241`) và chỉ được đọc fail-closed lúc thực thi (`bridge.py:527-538`, `daily.py:67-76`). PUT nhận key này → `403 forbidden_key`.
3. `exec_base_url` là cấu hình venue read-only qua API (đổi trong `.env` + restart backend), vì nó quyết định host mạng và cặp credentials; `exec_watchlist` và `exec_symbol_overrides` vẫn có endpoint riêng/không thuộc settings.

### GET /api/settings

Response 200:

```json
{
  "generated_at": "2026-09-30T08:00:00Z",
  "settings": [
    {
      "key": "max_debate_rounds",
      "env_var": "TRADINGAGENTS_MAX_DEBATE_ROUNDS",
      "value": 1,
      "default": 1,
      "type": "int",
      "editable": true,
      "source": "default"
    },
    {
      "key": "temperature",
      "env_var": "TRADINGAGENTS_TEMPERATURE",
      "value": null,
      "default": null,
      "type": "float|null",
      "editable": true,
      "source": "default"
    },
    {
      "key": "exec_base_url",
      "env_var": "TRADINGAGENTS_EXEC_BASE_URL",
      "value": "https://demo-api.binance.com",
      "default": "https://demo-api.binance.com",
      "type": "str",
      "editable": false,
      "source": "default"
    },
    {
      "key": "exec_sandbox",
      "env_var": "TRADINGAGENTS_EXEC_SANDBOX",
      "value": false,
      "type": "bool",
      "editable": true,
      "source": "env"
    }
  ],
  "credentials": [
    { "env_var": "OPENAI_API_KEY", "configured": true, "masked": "••••abcd" },
    { "env_var": "BINANCE_DEMO_API_KEY", "configured": true, "masked": "••••wxyz" },
    { "env_var": "BINANCE_DEMO_SECRET", "configured": true, "masked": "••••1234" }
  ],
  "gates_readonly": {
    "exec_live_config":          false,
    "TRADINGAGENTS_EXEC_LIVE":          false,
    "exec_auto_confirm_config":  false,
    "TRADINGAGENTS_EXEC_AUTO_CONFIRM":  false,
    "exec_derivatives_config":   false,
    "TRADINGAGENTS_EXEC_DERIVATIVES":   false
  },
  "effective_exec_mode": "dry"
}
```

- `settings[]` phản ánh `_ENV_OVERRIDES`; `exec_base_url` xuất hiện read-only dù được đọc từ `TRADINGAGENTS_EXEC_BASE_URL`. PUT key này → `403 forbidden_key`. `exec_sandbox` chỉ dùng cho sandbox của sàn khác, không chọn Binance venue.
- `settings[]` có các row editable từ `_ENV_OVERRIDES` và các row read-only bổ sung (`exec_base_url` cùng các path); `type` suy từ kiểu default trong `DEFAULT_CONFIG`.
- Với Binance, `exec_base_url` chỉ chấp nhận HTTPS API host Demo hoặc production được allowlist. URL được chọn quyết định credential pair: Demo dùng `BINANCE_DEMO_API_KEY`/`BINANCE_DEMO_SECRET`; production dùng `BINANCE_API_KEY`/`BINANCE_SECRET`. Demo là default. Đổi profile cần sửa URL + đúng pair trong `.env` rồi restart; API GET chỉ expose URL và status/mask, PUT không đổi được URL.
- `effective_exec_mode` độc lập với venue URL: `live` chỉ khi `exec_live is True` **và** `TRADINGAGENTS_EXEC_LIVE=true`; chọn Demo không tự mở gate.
- Paths read-only (`results_dir`, `data_cache_dir`, `memory_log_path`, `exec_log_path`) và `exec_base_url` trả trong `settings[]` với `editable: false`.

Trạng thái: loading · empty (không xảy ra — luôn có defaults) · error 500 · stale sau 30s.

### PUT /api/settings

Request:

```json
{
  "settings": {
    "max_debate_rounds": 2,
    "risk_max_daily_loss_pct": 4.0,
    "exec_sandbox": true
  }
}
```

Xử lý:

1. Validate từng key: phải thuộc 30 keys `_ENV_OVERRIDES`; coerce theo kiểu default như `_coerce` (`default_config.py:60-80`) — sai kiểu → `422 coercion_error` kèm `details: {"env_var": "...", "message": "expected a boolean (true/1/yes/on/false/0/no/off), got 'treu'"}`.
2. Áp in-memory: `set_config({key: value})` (`dataflows/config.py`) — có hiệu lực với run mới tạo sau đó; run đang chạy không thấy (bridge chụp config lúc dựng — `bridge.py:110-121`).
3. Persist vào `.env` (upsert dòng `TRADINGAGENTS_…`, giữ nguyên các dòng khác và comment). Ghi `encoding="utf-8"`.
4. Nếu key là `exec_sandbox` → cảnh báo không cần (an toàn); nếu key là `risk_*` → nhớ rằng giá trị mới chỉ dùng cho guard dựng sau (`bridge.py:147-156`).

Response 200: cùng shape GET + `"applied_at"`. Restart không bắt buộc vì (2) đã áp in-memory.

Errors: 403 `forbidden_key` (key read-only hoặc key lạ), 422, 500 (không ghi được `.env`).

Trạng thái: loading · empty (n/a) · error · stale (n/a — action).

---

## 2. Watchlist — `GET /api/watchlist`, `POST /api/watchlist`, `DELETE /api/watchlist/{symbol}`

Nguồn: `exec_watchlist` default `["BTC-USD","ETH-USD"]` (`default_config.py:233-234`); chuẩn hóa biên giới của daily runner (`daily.py:95-111`).

Lưu trữ: engine đọc `exec_watchlist` từ config; vì key không env-overridable, API layer giữ file state riêng `~/.tradingagents/webui/watchlist.json` và sync vào `DEFAULT_CONFIG["exec_watchlist"]` khi khởi động server và sau mỗi ghi. Endpoint `run_daily(watchlist=…)` nhận watchlist tường minh (`daily.py:114-128`) nên truyền trực tiếp, không phụ thuộc file.

### GET /api/watchlist

Response 200:

```json
{
  "generated_at": "2026-09-30T08:00:00Z",
  "symbols": [
    { "symbol": "BTC-USD", "added_at": "2026-09-28T10:00:00Z" },
    { "symbol": "ETH-USD", "added_at": "2026-09-28T10:00:00Z" }
  ]
}
```

Trạng thái: loading · **empty** (`symbols: []` — daily runner sẽ bỏ qua tất cả) · error 500 (file hỏng → cố gắng khôi phục từ config default, kèm `warning`) · stale 60s.

### POST /api/watchlist

Request: `{ "symbol": "SOL-USD" }` — một symbol/lần. Server strip, uppercase form Yahoo (`BTC-USD`); reject rỗng → 400; trùng (không phân biệt hoa thường) → 409 `conflict`.

Response 201: GET-shape sau khi thêm.

### DELETE /api/watchlist/{symbol}

Path param là symbol đã normalize (vd `BTC-USD`). Không có → 404. Response 200: GET-shape sau khi xóa. Xóa symbol cuối vẫn cho phép (watchlist rỗng là hợp lệ).

Trạng thái DELETE: loading · error 404/500 · stale (n/a).

---

## 3. Runs — phân tích

Nguồn luồng thật: `cli/run.py:96-354` (stream graph thành event); graph API: `trading_graph.py:157-180` (`propagate`), `182-227` (checkpoint), `346-386` (`_log_state`); signal 5-tier + `REVIEW` (`trading_graph.py:167-171`); báo cáo trên đĩa: `reporting.py:13-101`.

> rev 7 (đã implement): run và **toàn bộ log agent** được ghi vào `control.db` (`server/run_store.py`). Mỗi event `_emit` phát ra được persist ngay (`run_events`), và mỗi lần đổi trạng thái ghi lại `runs`; nên UI **replay được bất kỳ run đã xong nào**, không chỉ run đang stream. `_runs` in-memory giữ nguyên vì condition variable không sống được trong database — nó là buffer sống mà SSE chờ đợi. Stream của một run không còn trong live set được phục vụ từ `run_events` rồi tự đóng. Run bị process chết bỏ lại ở `running` được set `interrupted` lúc khởi động (`runs.reconcile()`), cùng lý do như job (§5 rev 7). Trạng thái cuối được ghi **trước** khi phát event terminal, để reader không bao giờ thấy run đã xong trong khi bản ghi còn nói `running`.

Ràng buộc triển khai (quan trọng):

- **Runs chỉ phân tích, không đụng execution.** `POST /api/runs` chỉ stream graph như CLI; `cli/run.py` không import `ExchangeBridge`. Run phân tích không bao giờ sinh plan → không bao giờ đẩy hàng đợi duyệt; nguồn item của queue là `POST /api/daily` (§5).
- **Điểm gọi production của `plan_order` chỉ có hai, cùng một seam**: daily runner unattended (`daily.py:167`) và worker của `POST /api/daily` (§5 — server tái dùng `bridge.plan_order`, không có endpoint nào khác tạo plan, để không ai tự bịa ra một đường đặt lệnh ngầm, đúng thứ invariant 1 của §4 cấm).
- **Một run tại một thời điểm.** `TradingAgentsGraph.__init__` gọi `set_config` process-wide (`trading_graph.py:65`, `dataflows/config.py`) — hai graph dựng song song ghi đè config của nhau. `POST /api/runs` khi có run active → `409 conflict` kèm `run_id` đang chạy. Chạy nhiều run đồng thời cần tách process — ngoài scope v1. Lock này chia sẻ với `POST /api/daily` (§5).
- Server chạy graph trong worker thread duy nhất, stream chunk như CLI (`cli/run.py:226`) thành SSE; không gọi `propagate()` cho luồng UI vì nó blocking, không lộ event.

### POST /api/runs

Request:

```json
{
  "ticker": "BTC-USD",
  "trade_date": "2026-09-30",
  "asset_type": "crypto",
  "mode": "live",
  "checkpoint": false,
  "source_run": null
}
```

| field | bắt buộc | giá trị |
|-------|----------|---------|
| `ticker` | ✔ | pipeline ticker (`BTC-USD`, `NVDA`) |
| `trade_date` | ✔ | `YYYY-MM-DD`, ≤ hôm nay (`trading_graph.py:29-40`) |
| `asset_type` | ✔ | `"stock"` \| `"crypto"` (CLI tự dò, API yêu cầu tường minh — `trading_graph.py:157-160`) |
| `mode` | ✗ | `"live"` (mặc định: gọi LLM thật) \| `"mock"` (replay, xem dưới) |
| `checkpoint` | ✗ | bool; bỏ qua → theo config (`cli/run.py:89-93`) |
| `source_run` | ✗ | chỉ dùng với `mode:"mock"`: `run_id` cũ để replay; null → tự chọn run gần nhất cùng ticker |

Validation lỗi → `400 validation_error` (ticker/date/asset_type), `409 conflict` (run active).

Response 201:

```json
{
  "run_id": "r_20260930_081512_btcusd",
  "status": "queued",
  "mode": "live",
  "ticker": "BTC-USD",
  "trade_date": "2026-09-30",
  "asset_type": "crypto",
  "created_at": "2026-09-30T08:15:12Z",
  "events_url": "/api/runs/r_20260930_081512_btcusd/events"
}
```

### Mock replay (bắt buộc — UI chạy không cần LLM key)

`mode: "mock"` tạo run thật về mặt protocol nhưng không gọi LLM/data vendor. Server phát chuỗi event bằng cách:

1. **Replay ưu tiên**: đọc `full_states_log_<date>.json` của ticker (shape đã xác minh, 11 keys: `company_of_interest, trade_date, market_report, sentiment_report, news_report, fundamentals_report, investment_debate_state{bull,bear,history,current_response,judge}, trader_investment_decision, risk_debate_state{aggressive,conservative,neutral,history,judge}, investment_plan, final_trade_decision` — `trading_graph.py:346-386`; dữ liệu thật tại `<results_dir>/<ticker>/TradingAgentsStrategy_logs/`). Chuyển từng phần thành event theo thứ tự pipeline (analyst → debate → trader → risk → pm → decision), mỗi event cách nhau ~400ms để UI thấy tiến trình.
2. **Synthetic fallback**: không có file nào → fixture nội tuyến (bản rút gọn: mỗi analyst 1 đoạn ngắn, 1 vòng debate, rating `"Hold"`) — đảm bảo UI luôn demo được.
3. Mọi event mock mang `"replay": true` (xem SSE schema). Kết thúc `run_completed` như run thật, nhưng `report_paths` chỉ có khi chọn replay từ file thật.

### GET /api/runs

Lịch sử run, mới nhất trước (rev 7). Query: `status` (một trong 6 trạng thái; giá trị lạ → 400), `page` (≥1), `page_size` (1–100).

```json
{
  "generated_at": "2026-10-05T09:11:30Z",
  "items": [
    {
      "run_id": "r_20261005_081512_btcusd",
      "status": "completed", "mode": "live", "ticker": "BTC-USD",
      "trade_date": "2026-10-05", "asset_type": "crypto",
      "created_at": "2026-10-05T08:15:12Z", "finished_at": "2026-10-05T08:27:40Z",
      "signal": "Buy", "is_review": false, "error": null,
      "report_paths": { "complete_report": "...", "state_log": "..." },
      "analysts": ["market", "social", "news"],
      "events_url": "/api/runs/r_20261005_081512_btcusd/events",
      "events_count": 42
    }
  ],
  "page": 1, "page_size": 20, "total": 7,
  "active_run_id": null
}
```

- `active_run_id` đọc từ live registry của process này (như `active_job_id` ở §5).
- `events_count` là số event agent đã ghi cho run đó — cột "agent đã làm gì" của lịch sử.

### GET /api/runs/{id}

Live state nếu run thuộc process này, **ngược lại đọc từ database** (rev 7). Response 200:

```json
{
  "run_id": "r_20260930_081512_btcusd",
  "status": "completed",
  "mode": "live",
  "ticker": "BTC-USD",
  "trade_date": "2026-09-30",
  "asset_type": "crypto",
  "created_at": "2026-09-30T08:15:12Z",
  "finished_at": "2026-09-30T08:27:40Z",
  "signal": "Underweight",
  "is_review": false,
  "error": null,
  "report_paths": {
    "complete_report": "C:/Users/mrc/.tradingagents/logs/reports/BTC-USD_20260930_082740/complete_report.md",
    "state_log": "C:/Users/mrc/.tradingagents/logs/BTC-USD/TradingAgentsStrategy_logs/full_states_log_2026-09-30.json"
  },
  "analysts": ["market", "social", "news"]
}
```

- `status`: `queued | running | completed | failed | cancelled`.
- `signal`: 5-tier hoặc `"REVIEW"` (`trading_graph.py:167-171`, `cli/run.py:361`).
- `report_paths` tương ứng `save_reports` (`trading_graph.py:245-258`) và `_log_state` (`trading_graph.py:376-386`).

Errors: 404.

Trạng thái: loading · error 404/500 · stale — poll `status` mỗi 2s khi SSE rớt (fallback cho `stale` SSE).

### GET /api/runs/{id}/events — SSE stream

`Content-Type: text/event-stream`. Event SSE `data` là JSON:

```json
{
  "seq": 12,
  "ts": "2026-09-30T08:16:02Z",
  "type": "debate_update",
  "role": "researcher",
  "stage": "analyst|researcher|trader|risk|pm|system",
  "agent": "Bull Researcher",
  "replay": false,
  "payload": { "field": "bull_history", "content": "..." }
}
```

Danh sách `type` (phát sinh từ chính các nhánh xử lý chunk trong `cli/run.py:226-323`):

| type | role/stage | payload |
|------|-----------|---------|
| `run_started` | system | `{analysts: [...]}` |
| `message` | theo `classify_message_type` | `{msg_type, content}` — nội dung agent + tool call log |
| `tool_call` | system | `{tool, args}` |
| `analyst_status` | analyst | `{agent, status: in_progress\|completed}` (Market/Sentiment/News/Fundamentals Analyst) |
| `debate_update` | researcher | `{field: bull_history\|bear_history, content}` |
| `research_manager` | researcher | `{content}` — `judge_decision` |
| `trader_update` | trader | `{content}` — `trader_investment_plan` |
| `risk_update` | risk | `{field: aggressive\|conservative\|neutral, content}` |
| `pm_decision` | pm | `{content}` — risk `judge_decision` |
| `run_completed` | system | `{signal, is_review, report_paths}` — terminal |
| `run_failed` | system | `{error: {code, message}}` — terminal |

Quy tắc:

- Mỗi event có `seq` tăng dần; client reconnect gửi header `Last-Event-ID: <seq>` → server replay từ seq+1 (buffer trong bộ nhớ của run, hoặc từ `run_events` khi run không còn trong live set — rev 7).
- Terminal event (`run_completed`/`run_failed`) rồi server đóng stream.
- Run ở trạng thái completed khi client mở stream → server phát lại toàn bộ buffer rồi đóng (UI refresh trang vẫn xem lại được).
- rev 7: run **đã xong từ lâu** (không còn trong `_runs`) vẫn mở được stream — server đọc `run_events` từ database, phát lại rồi đóng. Đây là đường để UI xem lại "agent đã làm gì" của một lần chạy cũ.
- `replay: true` trên mọi event của run mock.

Trạng thái: loading (trước event đầu) · empty (không xảy ra — luôn có `run_started`) · error (network đứt → tự reconnect bằng `Last-Event-ID`; run failed → event `run_failed`) · stale (không nhận event > 60s trong khi status vẫn `running` → badge "đang treo").

---

## 4. Approvals — hàng đợi duyệt lệnh

Nguồn: engine **không có** hàng đợi duyệt — daily runner unattended chỉ audit `awaiting_approval` rồi trả về (`daily.py:186-201, 219-222`). Queue là state của API layer: persist JSONL atomically tại `server/data/approvals.jsonl` (mỗi item một dòng snapshot, rewrite tmp+replace nên crash không để lại queue dở — `approvals.py:65-73`), hook `approvals.enqueue(plan)` (`approvals.py:109-138`) là cổng nhập duy nhất. **Đặt lệnh thật vẫn chỉ đi qua `ExchangeBridge.execute_order`** (`bridge.py:265-328`) — RiskGuard re-check fail-closed chạy bên trong (`bridge.py:311-325`), không có đường nào gửi lệnh ngoài nó.

> rev 3.1 (2026-10-01): pipeline approve 4 bước **đã implement** trong `server/approvals.py:241-333` (trước đó là claim-only — item kẹt ở `approved` vĩnh viễn vì `_pending_item` chỉ nhận `pending`; reviewer phát hiện, đã xoá dead-end). Refusals 409-mock / 409-rebuild diễn ra **trước** claim flip nên item còn `pending` và reject được. Policy `dry_fill_on_gate_closed` của rev 2 vẫn bỏ.

Invariants (bắt buộc, verify khi code review):

1. **Không tồn tại endpoint đặt lệnh.** Approve là đường duy nhất đưa plan vào `execute_order(confirm=True)`; reject không đụng exchange; daily job (§5) chỉ enqueue, không bao giờ execute.
2. **Approve fail-closed theo nửa config của FR5.** `POST approve` chỉ mở khi `exec_live` là literal True trong config (`approvals.py:245-250`); `exec_live` ≠ True → `412 gate_closed`, **không có dry-fill thay thế**. `412` không đụng item — item còn `pending`, operator arm gate rồi approve lại được. Mode dry/live vẫn do `execute_order` quyết định lúc thực thi theo cả hai nửa (`bridge.py:472-486`): config mở + `TRADINGAGENTS_EXEC_LIVE` env false → dry-fill ghi sổ; cả hai mở → lệnh thật. Approve không tự arm live — một nửa gate không bao giờ đương nhiên mở nửa kia.
3. **Approve khi `exec_live=True` chạy đủ bốn bước** (đã code — `approvals.py:241-333`): (a) dựng lại `PlannedOrder` từ item, (b) RiskGuard double-check, (c) `bridge.execute_order(order, confirm=True, portfolio)`, (d) ghi audit cả hai tầng — chi tiết ở "Xử lý server" dưới.
4. **Mọi hành động ghi audit.** Server trail (`server/data/audit.jsonl` — `server/audit.py:26-32`): `approval_granted` trước execute, `approval_executed` / `approval_execute_failed` sau; reject ghi `approval_denied` vào server trail (`approvals.py:357-358`). Tầng engine tự audit nhánh execute kể cả lỗi (`bridge.py:20-21, 449-451`) — reject không chạm engine nên chỉ có một tầng server trail (item không giữ object plan để rebuild).
5. RiskGuard halted hôm nay → mọi approve trả `403 risk_halted` kèm reason (`risk.py:472-477, 587-589`).
6. **Queue chỉ được nuôi bởi `POST /api/daily` (§5).** `DerivativesExecutor` (`derivatives.py:60`, ba lớp gate FR-D tại `derivatives.py:125-132`) không được kích hoạt từ bất kỳ endpoint v1 nào; `POST /api/runs` chỉ phân tích (§3). Plan sinh từ job `mode:"mock"` mang `mock: true` và **không approve được** (409 `mock_not_executable` — `approvals.py:260-266`, trước claim nên item còn `pending`, vẫn reject được) — dữ liệu tổng hợp không bao giờ thành lệnh thật.

### GET /api/approvals

Response 200 (rev 3: thêm `daily_job_id`, `expires_at`, `mock` — M4 hiển thị đủ):

```json
{
  "generated_at": "2026-09-30T08:00:00Z",
  "items": [
    {
      "id": "ap_0182",
      "created_at": "2026-09-30T08:15:40Z",
      "expires_at": "2026-10-01T08:15:40Z",
      "daily_job_id": "d_20260930_081512",
      "run_id": null,
      "ticker": "BTC-USD",
      "ccxt_symbol": "BTC/USDT",
      "signal": "Buy",
      "side": "buy",
      "quantity": 0.0012,
      "price_est": 83078.0,
      "cost": 99.69,
      "market": "spot",
      "leverage": 1.0,
      "mode_at_plan": "dry",
      "plan_reason": "risk-approved sizing",
      "mock": false,
      "status": "pending",
      "resolved_at": null,
      "resolution": null,
      "executed": null,
      "mode": null,
      "order_id": null
    }
  ],
  "pending_count": 1
}
```

- `daily_job_id` — job `POST /api/daily` (§5) đã sinh plan; `run_id` giữ để nối tương lai với một run phân tích, hiện luôn `null` (producer duy nhất là daily job). `expires_at` = `created_at + 24h` (`approvals.py:38` — hằng số `EXPIRY_HOURS`, chưa có API cấu hình).
- `mock: true` → plan từ job `mode:"mock"`: approve bị từ chối (409 `mock_not_executable`, item còn `pending`), reject hoạt động bình thường.
- `status`: `pending | approved | executed | execute_failed | rejected`. `approved` là trạng thái *claim*: đã qua gate/halt/mock/rebuild check và đang/khả năng đã gửi lệnh (hàng rào idempotency — `approvals.py:257-272`; process chết tại đây phải đối soát qua audit + venue, không tự hồi). `expired` không phải trạng thái lưu — là **badge suy ra** trên item còn `pending` mà `expires_at` đã qua; server trả `410` khi cố resolve item đó.
- `executed`/`mode`/`order_id` — điền khi approve resolve: thành công → `executed` (true khi live), `mode` dry/live, `order_id` từ exchange (dry → null); thất bại → `status: execute_failed` + `resolution` = reason. Trước resolve cả ba là `null` (khởi tạo tại `enqueue` — `approvals.py:132-134`).
- Nguồn item: **chỉ** worker `POST /api/daily` (§5) — enqueue **per-coin**, ngay khi coin xong (không đợi cả watchlist như rev 2): plan có `side != null` → `approvals.enqueue({daily_job_id, ticker, ccxt_symbol, signal, side, quantity, price_est, cost, market, leverage, mode_at_plan, plan_reason, mock})` + `bridge.audit_plan(plan, action="awaiting_approval", …)` (đúng seam `daily.py:219-222`). Plan đã tự execute (auto-confirm của engine) không xảy ra trên đường UI — invariant 1 của §5.
- Query (đã implement — `approvals.py:226-231`): `?status=pending&limit=50&offset=0`.

Trạng thái: loading · **empty** (`items: []` — khi chưa có plan nào) · error 500 · stale 15s (đây là màn approval gate — phải auto-poll; item biến mất khi đã resolve bởi nơi khác).

### POST /api/approvals/{id}/approve

Request bắt buộc `Content-Type: application/json` (quy tắc CSRF §0 — đây là endpoint nguy hiểm nhất của hệ thống, không có ngoại lệ); body tối thiểu `{}`:

```json
{ "portfolio": null }
```

`portfolio: null` → server tự `bridge.sync_portfolio()` (`bridge.py:330-356`, cần venue đọc được; nếu không đọc được → vẫn tiếp tục với `portfolio=null`: percent-limits fail-open như `risk.py:512-520`, response mang `warning` và server audit ghi warning — chấp nhận đúng hành vi engine; xem risk §11.15).

Xử lý server (rev 3.1 — thứ tự bắt buộc, đã code tại `approvals.py:241-333`):

1. **Gate check — fail-closed, trước khi đụng item.** `config.exec_live is not True` → `412 gate_closed` với message guide tường minh:
   > `"approving requires exec_live=True in config; the FR5 gate is closed — plans stay pending. Arm the gate (operator action outside this API) and retry."`
   Hành vi khi gate đóng (guide phải nói rõ cho user): **không có đường duyệt nào** — mọi item cứ `pending` chờ; UI phải hiển thị banner gate + guide này thay vì nút approve vô tác dụng. `412` không đổi trạng thái item (`approvals.py:245-250`).
2. **Halt check — fail-closed.** `audit_log.halt_state()` (`approvals.py:251-253`, `server/audit.py:68-81`): log không đọc được → `503 audit_log_unreadable`; halted hôm nay → `403 risk_halted` (kèm `details.reason`).
3. **Claim (idempotency) dưới store lock — các refusal diễn ra TRƯỚC flip** để item còn `pending` và reject/lần approve sau vẫn được (`approvals.py:257-272`): item phải tồn tại (404), `pending` (409 `conflict` — `approvals.py:100-102`), chưa quá hạn (410 `expired` — `approvals.py:103-105`); item `mock: true` → 409 `mock_not_executable` (`approvals.py:260-266`); item không rebuild được (thiếu `side`/`quantity` hợp lệ — `_rebuildable`, `approvals.py:154-160`) → 409 `conflict` (`approvals.py:267-270`). Tất cả check xong mới flip `status: "approved"` + save atomically — flip là bước cuối của khối lock; tab khác bấm cùng lúc → 409 vì status đã rời `pending`.
4. **Audit `approval_granted` + (a) rebuild plan từ item.** Server trail `approval_granted` (kèm `approval_id, ticker, side, quantity, daily_job_id` — `approvals.py:274-276`); rồi `_rebuild_plan` (`approvals.py:163-180`): `PlannedOrder(ticker, ccxt_symbol, signal, side, quantity, estimated_price=price_est, cost, market, leverage, needs_review=False, reason=None, mode="dry")` — `mode` chỉ informational (`sizing.py:49-53`), mode thật do `execute_order` tính lúc này.
5. **Dựng venue + portfolio.** `_build_bridge(config)` fail (venue không dựng được) → item `execute_failed` + `502 execute_failed` (`approvals.py:278-282`) — không để item kẹt ở claim. `portfolio: null` → `bridge.sync_portfolio()`; venue không đọc được → vẫn tiếp tục với `portfolio=null`: percent-limits fail-open như `risk.py:512-520`, response mang `warning` và server trail ghi `approval_portfolio_sync_failed` (`approvals.py:284-290` — chấp nhận đúng hành vi engine; xem risk §11.15).
6. **(b)+(c) RiskGuard double-check + `bridge.execute_order(order, confirm=True, portfolio)`** (`approvals.py:294-295`, `bridge.py:265-328`). Là MỘT lượt gọi: bên trong, guard re-check **ngay trước khi gửi** với audit log đọc fail-closed (`bridge.py:311-315`, `risk.py:444-470` — halt/streak không thể verify → từ chối), rồi đi dry-fill (`bridge.py:358-369`) hoặc live (`bridge.py:371-470` — fresh price, min-cost sau precision, `create_order`).
7. **(d) Audit tầng hai + trạng thái item cuối** (`approvals.py:296-333`, `_resolve` — `approvals.py:184-211`, `_fail_execute` — `approvals.py:213-217`): thành công → item `executed` (+ `executed`/`mode`/`order_id` trên item) và server trail `approval_executed {mode, executed, order_id}`; guard từ chối → 403 `risk_halted` + item `execute_failed` + trail `approval_execute_failed`; exchange lỗi → 502 `execute_failed`. Tầng engine đã tự audit mọi nhánh trong `execute_order` (rejected_by_risk / rejected_at_execute / execute_denied / dry-fill / buy-sell — `bridge.py:20-21, 317-325, 449-451`). Khối đặc biệt: "order WAS placed but audit write failed" (`bridge.py:464-469`) → item `executed` kèm `order_id` parse từ message + `warning` red-flag — **không được retry**, order id có thật (retry = lệnh trùng, mất tiền thật — `bridge.py:18-21`).

Kết quả ánh xạ từ `execute_order`:

- dry-fill (config mở + env đóng) → `status: executed`, `executed: false`, `mode: "dry"`, `order_id: null` (`bridge.py:358-369`);
- live thành công → `executed: true`, `mode: "live"`, `order_id` từ exchange;
- guard từ chối → item `execute_failed`, HTTP 403 `risk_halted` (engine đã audit `rejected_by_risk`, `bridge.py:317-325`);
- `PreTradeRejection` (giá fresh không có, cost dưới minimum sau precision — `bridge.py:411-441`) → item `execute_failed`, HTTP 502 `execute_failed`;
- exchange NetworkError/ExchangeError/thiếu credentials → item `execute_failed`, HTTP 502 `execute_failed` (engine đã audit trước khi raise — `bridge.py:385-397, 447-451`).

Response 200 (dry-fill):

```json
{
  "id": "ap_0182",
  "status": "executed",
  "executed": false,
  "mode": "dry",
  "order_id": null,
  "risk": { "allowed": true, "halted": false, "reason": null },
  "audit_action": "buy",
  "warning": null
}
```

(live: `executed: true`, `mode: "live"`, `order_id` từ exchange; `warning` mang text khi portfolio-sync fail hoặc audit-write-fail red-flag.)

### POST /api/approvals/{id}/approve — lỗi

| HTTP | code | ý nghĩa |
|------|------|---------|
| 403 | `risk_halted` | pre-check (bước 2) hoặc re-check trong `execute_order` từ chối (kèm `details.reason`) — item → `execute_failed` |
| 409 | `conflict` | item không còn `pending` (đã resolve bởi tab khác) hoặc không rebuild được plan (`approvals.py:267-270` — **trước claim**, item còn `pending`) |
| 409 | `mock_not_executable` | item mang `mock: true` — plan demo từ job mock không thể thành lệnh thật (`approvals.py:260-266` — **trước claim**, item còn `pending`, vẫn reject được) |
| 410 | `expired` | quá hạn duyệt 24h |
| 412 | `gate_closed` | `exec_live` ≠ True — fail-closed, item giữ `pending`, guide arm-gate (bước 1) |
| 502 | `execute_failed` | exchange từ chối/lỗi sau khi execute bắt đầu (`PreTradeRejection`, NetworkError/ExchangeError, thiếu credentials, venue không dựng được — `approvals.py:278-282, 300-317`) — item → `execute_failed`, reason trong `details.reason` |
| 503 | `audit_log_unreadable` | pre-check hoặc execute-time re-check fail-closed (`bridge.py:311-315`, `risk.py:122-147`) |

Idempotency: approve **không được retry mù quáng** khi đã nhận 200 với `order_id` khác null — lệnh có thật; chỉ retry được khi item vẫn `pending` và chưa có line `buy/sell` phase-execute trong audit cho item đó (đối soát `GET /api/audit?ticker=…&phase=execute`, §7).

### POST /api/approvals/{id}/reject

Request bắt buộc `Content-Type: application/json` (quy tắc CSRF §0), body tối thiểu `{}`:

```json
{ "reason": "không duyệt mức giá này" }
```

Xử lý: item phải `pending` (409) / chưa hết hạn (410); ghi audit server trail `approval_denied` kèm reason (`server/audit.py:26-32`, `approvals.py:357-358`); **không** gọi exchange, **không** cần `exec_live` — từ chối luôn hợp lệ kể cả khi gate đóng; item `mock: true` cũng reject được (chỉ approve mới bị chặn với mock). Trạng thái cuối: `rejected` + `resolution` = reason.

Response 200: item sau cập nhật. Errors: 409/410 như trên (không 503 — reject không đọc audit log để quyết định).

Trạng thái POSTs: loading (execute live có thể mất vài giây — disable nút, đợi) · error · stale (n/a — action; sau action poll lại GET).

---

## 5. Daily pipeline — `POST /api/daily`, `GET /api/daily`, `GET /api/daily/{job_id}`, `POST /api/daily/{job_id}/cancel`

Nguồn: `run_daily(watchlist, config)` (`daily.py:114-174`) — bề mặt lập lịch của engine cho chuỗi *phân tích → sync portfolio → plan qua RiskGuard → confirm policy*. Đây là **nút bấm "Chạy pipeline hằng ngày ngay"** trên UI và là nguồn duy nhất của hàng đợi duyệt (§4, invariant 6).

> rev 3: worker server **không gọi `run_daily` trực tiếp nữa** — nó tự chạy vòng lặp per-coin trên đúng các seam của engine, vì ba lý do: (1) enqueue phải xảy ra **ngay khi coin xong** (rev 2 đợi cả job `completed` — risk 13 cũ), (2) `mode:"mock"` cần tái dùng cơ chế replay của `runs.py` thay vì dựng graph, (3) **job bấm từ UI không bao giờ tự thực thi** — confirm policy của engine (`_handle_plan`, `daily.py:177-216`) chỉ thuộc surface unattended (cron → `run_daily`); job từ UI luôn resolve qua approval gate vì human đang ở màn đó. `run_daily` không đổi, vẫn là đường cron.

> rev 7 (đã implement): **job là một dòng trong database, không còn là dict chết cùng process.** Mọi thay đổi trạng thái ghi vào `server/data/control.db` (`server/db.py` + `server/job_store.py`), nên (a) UI **list được lịch sử job** và đọc lại từng mã agent đã làm gì sau khi restart, (b) job bị process chết bỏ lại ở `running` được **reconcile thành `interrupted` lúc khởi động** (`server/main.py:_open_state_db`) thay vì chặn worker vĩnh viễn bằng 409, (c) có endpoint `cancel` để operator thoát khỏi job treo. `_jobs` in-memory giờ chỉ là live working set (handle của worker + fast path cho poll).


### POST /api/daily

Request bắt buộc `Content-Type: application/json` (quy tắc CSRF §0); body tối thiểu `{}`:

```json
{ "tickers": ["BTC-USD", "SOL-USD"], "mode": "live" }
```

hoặc quét tự động (rev 4) — **một nút bấm, server tự tìm mã**:

```json
{ "auto": true, "top_n": 5, "mode": "live" }
```

- `tickers` bỏ qua/null → dùng `exec_watchlist` hiện hành (`daily.py:127-128`); normalize tại biên giới (strip, drop rỗng, dedupe giữ thứ tự — như `_normalize_watchlist`, `daily.py:95-111`); ticker rỗng sau strip → 400.
- `auto` (rev 4, đã implement): `true` → **bỏ qua watchlist**. Worker chạy bước sàng lọc rẻ trước (`server/daily.py:_screen_candidates` → `tradingagents/dataflows/screening.py`):
  - **Universe crypto** (rev 4.1): ccxt public (`fetch_tickers` trên `exec_exchange_id`, mặc định binance — không cần API key), **top 40 theo `quoteVolume` 24h** (mẫu VolumePairlist của freqtrade — lọc stablecoin USDC/USD1/FDUSD/…, floor 5M USDT), map về dạng pipeline `BASE-USD`. Chọn theo khối lượng, không theo vốn hóa — universe chứa cả mover ít người biết.
  - **Universe cổ phiếu** (rev 4.1): `yfinance.screen` với `region=us + intradaymarketcap>2B + fiftytwowkpercentchange>0`, **sort theo `fiftytwowkpercentchange` giảm dần** (tilt George-Hwang 52-week-high: chỉ lấy mã đang mạnh theo năm, không phải danh sách vốn hóa lớn nhất); screener lỗi → fallback danh sách tĩnh lớn-cap (~38 mã liquid) nên outage không chặn auto-scan.
  - **Chấm điểm** (rev 4.1, `screen_universe` — mỗi tiêu chí đều có nguồn research):
    - Momentum 20 phiên (≥+5% cộng dồn có trần 2.0; ≤−5% cộng quá bán có trần 1.5) — factor backbone có bằng chứng học thuật cả crypto (15–35d) lẫn cổ phiếu (Jegadeesh-Titman).
    - Trend stack: trên SMA20 +1.0, trên cả SMA50 +0.5 (Minervini Stage-2 rút gọn).
    - RS vs benchmark: momentum 20P cao hơn BTC-USD (crypto) / ^GSPC (stock) ≥3 điểm % → +0.75.
    - RVOL (volume phiên cuối / trung bình 20 phiên): ≥2 → cộng tối đa 1.5; ≥4 → **trừ 0.75** (cực hạn, nguy cơ hồi — altFINS); 1.5–2 → +0.5.
    - RSI(14) (stockstats): 45–70 +0.5 (vùng sức mạnh còn dư địa); ≥75 **−0.75** (đu đỉnh); <30 +0.5 (quá bán).
    - Đỉnh 52 tuần: sát đỉnh ≤5% +1.0; ≤15% +0.5 (George-Hwang 2004: tín hiệu 52w-high mạnh hơn raw momentum, ít crash hơn).
    - Breakout: close > đỉnh High 20 phiên trước **và** RVOL≥2 → +1.0 ("bắt sớm, không chạy đuổi").
    - ATR(14)%: trong dải tradability (crypto 3–10%, stock 2–8%) +0.5; >20% **−0.75** (casino tier).
  - **Gate loại bỏ** (không phải trừ điểm): dollar-volume trung bình 20 phiên < $5M (crypto) / < $10M (stock) → loại khỏi universe; history < 60 phiên → bỏ (không đủ tính SMA50/RSI/52w).
  - Top `top_n` (mặc định 5, cap 20) đi tiếp vào pipeline LLM; kết quả sàng gắn lên job dưới `screening`.
  - `mode:"mock"` → sàng cố định fixture (BTC-USD/ETH-USD/NVDA) **không đụng network** (hằng số `MOCK_AUTO_CANDIDATES`).
- `mode` (rev 3): `"live"` (mặc định — phân tích LLM thật) | `"mock"` (tái dùng cơ chế mock của `runs.py` — **không cần LLM key, không network**).
- Chạy trong cùng worker thread duy nhất với `POST /api/runs` (lock `set_config` — §3); worker bận → `409 conflict`. Mock vẫn chiếm lock (đơn giản hoá; job không quá vài giây).

Vòng lặp worker — mỗi coin, coin fail không dừng watchlist (`daily.py:170-173`):

1. **Phân tích**:
   - `mode:"mock"` — tái dùng đúng machinery mock của `server/runs.py`: `_find_state_log` → `full_states_log_<date>.json` của ticker (mới nhất nếu thiếu đúng ngày — `runs.py:232-247`), không có → `_synthetic_state` (`runs.py:203-229`); signal = `parse_rating(state["final_trade_decision"])` (`runs.py:270`). Không dựng graph, không gọi LLM, không vendor call.
   - `mode:"live"` — như `daily.py:147-164`: `TradingAgentsGraph(config, selected_analysts=("market","social","news"))` + `graph.propagate(coin, today, asset_type=…)`, tối đa 3 lần thử với backoff 20s/40s, mỗi lần dựng graph mới (checkpoint state hỏng giữa pipeline không dính vào lần sau). rev 4: `asset_type` theo ứng viên (`crypto` giữ nguyên; `stock` thêm analyst `fundamentals` như `runs.py:_analysts_for`).
2. **Plan**: `bridge.plan_order(coin, signal, portfolio)` (`bridge.py:192-263`) — tự audit plan line phase=`plan` (`bridge.py:255-262`) và tự chạy RiskGuard plan-time. `portfolio` = `bridge.sync_portfolio()`. Venue: mock → **stub venue inject** qua `exchange_factory` (`bridge.py:126-129, 137-145` — cùng seam test inject của engine): `fetch_ticker`/`fetch_balance`/`load_markets` trả giá/balance fixture cố định ⇒ không network; live → venue thật (lệch `exec_sandbox`/`exec_exchange_id` theo config).
   - rev 4: **ứng viên stock chỉ advisory** — `plan_order` của bridge chỉ nhận crypto (`symbol_for` raise với mã khác, `bridge.py:174-186`), nên kết quả stock ghi `asset_type: "stock"`, `plan: null`, `reason: "advisory only — …"`; tín hiệu vẫn là output của pipeline.
3. **Plan actionable → ENQUEUE, không execute** (dù `exec_auto_confirm` có arm hay không — invariant dưới): plan có `side != null` → `approvals.enqueue(plan_dict)` (§4) + `bridge.audit_plan(plan, action="awaiting_approval", reason="UI daily job: plan awaits approval")` (đúng seam `daily.py:219-222`) → `results[]` ghi `approval_id`. Plan không tradeable (`side is None` — Hold/REVIEW/sizing refusal `sizing.py:104-143`, hoặc risk rejection `bridge.py:240-254`) → chỉ ghi `reason = plan.reason`, `approval_id: null`.
4. Audit vòng lặp: server trail ghi `daily_job_created` / `daily_job_completed` (kèm `job_id, mode, tickers`; rev 4 thêm `auto, top_n`).

Invariants của daily job:

- **UI daily job không bao giờ execute** — không gọi `execute_order` dưới bất kỳ cờ nào; auto-confirm armed chỉ có ý nghĩa với cron `run_daily`. Trên UI, mọi lệnh chỉ đi qua approve (§4) — nơi `exec_live` fail-closed chặn.
- **Plan mock là demo**: item vào queue mang `mock: true`, `mode_at_plan: "dry"`, giá/balance là fixture — approve trả `409 mock_not_executable` (§4), reject bình thường; `results[]` luôn `executed: false`.
- Một job tại một thời điểm (dùng chung lock với `POST /api/runs` — §3).

Response 202:

```json
{
  "job_id": "d_20260930_090001",
  "status": "queued",
  "mode": "live",
  "tickers": ["BTC-USD", "SOL-USD"],
  "created_at": "2026-09-30T09:00:01Z",
  "detail_url": "/api/daily/d_20260930_090001"
}
```

Errors: 400 `validation_error` (ticker rỗng sau strip, `mode` lạ; rev 4: `auto` không phải bool, `top_n` ngoài 1–20, `tickers` đi cùng `auto`, `top_n` không nguyên), 409 `conflict` (worker bận, kèm `details.run_id` hoặc `details.job_id` đang chạy), 415/403 CSRF (§0).

### GET /api/daily

Lịch sử job, mới nhất trước (rev 7) — màn hình mở ra để trả lời "có gì đang chạy, và lần trước ra sao?". Query: `status` (tuỳ chọn, một trong 6 trạng thái; giá trị lạ → 400), `page` (≥1), `page_size` (1–100, mặc định 20).

```json
{
  "generated_at": "2026-10-05T09:11:30Z",
  "items": [
    {
      "job_id": "d_20261005_090001",
      "status": "running",
      "mode": "live",
      "auto": true,
      "top_n": 5,
      "created_at": "2026-10-05T09:00:01Z",
      "finished_at": null,
      "tickers": ["BTC-USD", "SOL-USD"],
      "results_count": 1,
      "approval_count": 1,
      "error": null
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 12,
  "active_job_id": "d_20261005_090001"
}
```

- `active_job_id` đọc từ **live registry của chính process này**, nên job của process khác không bao giờ bị báo nhầm là đang chạy. Đây là nguồn duy nhất cho băng "đang chạy" của UI — trước rev 7 người dùng chỉ biết có job khi bị từ chối 409.
- `items[]` là **summary**: không kèm `results` (payload nằm ở `GET /api/daily/{job_id}`), kèm `results_count`/`approval_count` để hiển thị dòng lịch sử.

### POST /api/daily/{job_id}/cancel

Dừng job đang chạy (rev 7). **Không gate** — tắt một thứ là hướng an toàn. Trả 202 với payload job đã `cancelled`.

- Job được đánh dấu terminal **ngay**, nên worker lock được giải phóng tức thì; worker nhận ra giữa hai coin và dừng vòng lặp. Coin đang trong một lệnh LLM **chạy nốt** (Python không kill thread an toàn được) — cancel giải phóng worker, không giải phóng network.
- Coin đã chạy xong trước đó vẫn nằm trong `results[]`; coin đang chạy dở không được ghi.
- 404 khi job không tồn tại; 409 khi job đã terminal (`details.status`).

### GET /api/daily/{job_id}

Live state nếu job thuộc process này, **ngược lại đọc từ database** (rev 7). `results[]` điền dần theo từng coin (không đợi cả job xong). Response 200:

```json
{
  "generated_at": "2026-09-30T09:11:30Z",
  "job_id": "d_20260930_090001",
  "status": "running",
  "mode": "live",
  "created_at": "2026-09-30T09:00:01Z",
  "finished_at": null,
  "tickers": ["BTC-USD", "SOL-USD"],
  "asset_types": {},
  "screening": null,
  "results": [
    {
      "ticker": "BTC-USD",
      "asset_type": "crypto",
      "signal": "Buy",
      "reason": "awaiting approval",
      "plan": {
        "side": "buy", "quantity": 0.0012, "price_est": 83078.0, "cost": 99.69,
        "ccxt_symbol": "BTC/USDT", "market": "spot", "leverage": 1.0,
        "mode_at_plan": "dry", "plan_reason": "risk-approved sizing"
      },
      "approval_id": "ap_0183",
      "error": null
    },
    {
      "ticker": "SOL-USD",
      "asset_type": "crypto",
      "signal": null,
      "reason": "PermissionError: risk guard halted trading: 3 consecutive losing trades reached the limit 3; trading halted for today",
      "plan": null,
      "approval_id": null,
      "error": null
    }
  ]
}
```

- Mỗi coin: `signal` (5-tier hoặc `REVIEW` — `trading_graph.py:167-171`), `reason` (lý do không trade / `"awaiting approval"` / `"<ExceptionType>: <message>"` khi coin fail), `plan` = **chi tiết đủ cho M4** (`side, quantity, price_est, cost, ccxt_symbol, market, leverage, mode_at_plan, plan_reason` — đúng field `enqueue` nhận, §4), `approval_id` tham chiếu `GET /api/approvals` (null khi plan không tradeable), `error` (lỗi hệ thống khác reason nghiệp vụ). rev 4 thêm `asset_type` per coin (`"crypto"` mặc định; job cũ trước rev 4 không có field này — frontend đọc `asset_types`/`screening` null an toàn).
- Job `mode:"mock"`: thêm `mock_source` per coin — `"replay:<path>"` (đã replay state log thật) hoặc `"synthetic"` (fixture nội tuyến); `signal` đến từ file replay, ví dụ `"Hold"` → không sinh plan; `plan.price_est` là giá fixture của stub venue.
- Job `auto` (rev 4): `tickers` là danh sách ứng viên post-sàng; `screening` non-null với shape `{universe_count, universe_crypto, universe_stock, candidates[], mock?}`; `candidates[]` mỗi phần tử `{ticker, asset_type, score, last_close, momentum_pct, trend_pct, volume_ratio, rsi14, atr_pct, dist_52w_pct, breakout, avg_dollar_volume, reasons[]}` (rev 4.1: thêm `rsi14`/`atr_pct` nullable, `dist_52w_pct` ≤0, `breakout` bool, `avg_dollar_volume`). Bước sàng xong trước vòng lặp coin nên UI thấy bảng ứng viên ngay khi poll lần đầu sau khi worker start.
- `status`: `queued | running | completed | failed | cancelled | interrupted` (rev 7 thêm hai trạng thái cuối; `failed` chỉ khi worker văng ngoài vòng lặp coin; coin fail riêng chỉ là `reason` trong `results[]`; `interrupted` = process chết khi job đang chạy, được set lúc khởi động lại). Poll mỗi 2–5s khi `queued/running`; ngừng poll khi terminal. Đã có `generated_at` để đánh dấu stale.
- `error`: lý do **job** thất bại (screen/bridge), khác `reason`/`error` của từng coin trong `results[]`. rev 7.
- Coin fail giữa pipeline KHÔNG dừng vòng lặp (`daily.py:170-173`); coin skip propagate sau 3 lần thử cũng ghi `reason` như vậy (`daily.py:155-164`).

Errors: 404 (job không tồn tại ở cả live set lẫn database). Trạng thái: loading · empty (n/a — job luôn có ≥1 coin) · error 404/500 · stale (poll là cơ chế chính; job không chuyển trạng thái > 15 phút → gợi ý bấm "Dừng").

---

## 6. Portfolio — `GET /api/portfolio` (màn Tổng quan)

Nguồn: `bridge.sync_portfolio()` (`bridge.py:330-356` — `fetch_balance` → `PortfolioContext`, position theo pipeline ticker `BTC-USD`, bỏ quote) cho **positions**; và **realized P&L** từ replay FIFO của `risk.py` trên audit log: khớp sell↔buy FIFO per ticker trên `price_est`, bỏ qua line `phase="plan"` và line không phase có `confirmed=False` (chỉ line execute mới đếm — `risk.py:271-281`); sell không có lot mở = close basis-không-rõ, không tính vào P&L (`risk.py:15-17`); derivatives sell dư mở lot short, buy khớp ngược (`risk.py:296-338`). Server đọc log qua `server/audit.read_events()` (503 khi không đọc được — nhất quán execute-time fail-closed), rồi tái dùng `_replay` / `_consecutive_losses` / `_halted_today` của `risk.py` (seam private — risk §11.14; `_halted_today` đã được import sẵn ở `server/audit.py:74`).

Request: `GET /api/portfolio` (không tham số bắt buộc).

Response 200:

```json
{
  "generated_at": "2026-09-30T08:00:00Z",
  "currency": "USDT",
  "cash": 9500.0,
  "equity_est": 10225.0,
  "positions": [
    {
      "ticker": "BTC-USD",
      "quantity": 0.005,
      "average_price": 82000.0,
      "basis_source": "fifo",
      "marked_price": 83100.0,
      "market_value": 415.5,
      "unrealized_pnl": 5.5
    }
  ],
  "realized_pnl": {
    "total": 12.34,
    "today": -1.2,
    "by_ticker": { "BTC-USD": 12.34 },
    "closed_trades": 9,
    "unknown_basis_closes": 1,
    "consecutive_losses": 1
  },
  "halted_today": false,
  "warnings": [],
  "stale_after": 30
}
```

Định nghĩa từng field:

- `cash`, `currency`, `positions[]` — từ `sync_portfolio` (`bridge.py:330-356`): `quantity` thực từ `balance.total`, `cash` từ `balance.free` của quote; `cash: null` khi balance không trả quote. Balance currency không map được vào bộ base crypto bị bỏ qua (`bridge.py:345-348`) — tính vào `warnings` của response.
- `marked_price` = `bridge.fetch_price(ticker)` (`bridge.py:514-516`); null khi venue không mark được. Position không mark được tính **0** trong `equity_est` — cùng rule `_equity` (`risk.py:381-403`), ghi warning trong `warnings`.
- `equity_est` = `cash + Σ quantity × marked_price`; `null` khi `cash` null và không có position (fail-open như guard — `risk.py:512-520`).
- `average_price` + `basis_source`: từ **FIFO open lots** của `_replay` (`risk.py:248-339`) — trung bình có trọng số giá các lot dài còn mở của ticker; `basis_source: "fifo"` khi log có lot khớp, `null` khi không (position mở trước khi audit log bắt đầu = basis không xác định — `risk.py:15-17`); `unrealized_pnl` chỉ tính khi có basis + mark, ngược lại `null`.
- `realized_pnl.total` — toàn bộ closed P&L có basis; `today` — chỉ closed trade của **UTC hôm nay** (khác con số guard dùng: guard cộng thêm unrealized của lot mở hôm nay — `risk.py:405-442`; UI phải ghi chú khác biệt này); `by_ticker` — replay từng ticker riêng (FIFO là per-ticker nên subset từng ticker cho kết quả đúng); `closed_trades` — số close đã khớp; `unknown_basis_closes` — close không có lot để khớp (bị loại khỏi P&L, `risk.py:15-17`); `consecutive_losses` = `_consecutive_losses` (`risk.py:342-359`) — cross-ticker, mới nhất trước.
- `halted_today` — như `_halted_today` (`risk.py:163-173`); banner đỏ khi true (tham chiếu §7 audit).
- `stale_after: 30` — màn Tổng quan auto-poll 30s; `cash`/`marked_price` là snapshot venue tại `generated_at`.

Errors:

| HTTP | code | ý nghĩa |
|------|------|---------|
| 503 | `exchange_unreachable` | `fetch_balance`/`fetch_ticker` fail — venue chậm/đứt; không fail-open hiển thị (khác approve: approve fail-open là chủ ý của engine, còn màn Tổng quan phải thấy rõ dữ liệu không có thật) |
| 503 | `audit_log_unreadable` | `exec_log_path` không đọc được — nhất quán với execute-time fail-closed (`risk.py:122-147`) |

Trạng thái: loading · **empty** (`cash: null`, `positions: []`, `realized_pnl.total: 0` — chưa có dữ liệu venue/log; hiển thị empty-state riêng, không coi là lỗi) · error 503 (retry) · stale 30s + auto-poll.

Thiếu sót trung thực (ghi trên UI): position mở trước khi audit log bắt đầu có basis không xác định (`average_price: null`, `unrealized_pnl: null`) — replay chỉ thấy những gì log đã ghi; `equity_est` hạ thấp khi có position không mark được (`risk.py:381-403`).

## 7. Audit — `GET /api/audit`

Nguồn: `exec_log_path` mặc định `~/.tradingagents/execution/audit.jsonl` (`default_config.py:207`), append-only JSONL (`audit.py:16-21`). Shape một dòng (từ `bridge.py:565-606`, đối chiếu sample thật trong `~/.tradingagents/execution/audit.jsonl`):

```json
{
  "timestamp": "2026-09-29T04:02:00.509077+00:00",
  "ticker": "BTC-USD",
  "ccxt_symbol": "BTC/USDT",
  "signal": "Buy",
  "action": "rejected_by_risk",
  "qty": null,
  "price_est": 83078.0,
  "mode": "live",
  "confirmed": false,
  "order_id": null,
  "phase": "plan",
  "market": "spot",
  "leverage": 1.0,
  "reason": "position BTC-USD would reach 85.22% of equity ..."
}
```

`action` đã thấy: `buy | sell | no_order | rejected_by_risk | rejected_at_execute | execute_denied | awaiting_approval | approval_granted | approval_denied`; và dòng điều khiển `{"event": "halt" | "halt_reset"}` (`risk.py:587-596`) không có `action`.

Request: `GET /api/audit?ticker=BTC-USD&action=rejected_by_risk&phase=execute&from=2026-09-28&to=2026-09-30&page=1&page_size=50`

Response 200:

```json
{
  "generated_at": "2026-09-30T08:00:00Z",
  "items": [ /* các dòng JSONL trên, mới nhất trước, đúng nguyên văn (trừ được bổ sung `_line_no`) */ ],
  "page": 1,
  "page_size": 50,
  "total_matching": 7,
  "total_lines": 7,
  "halted_today": false,
  "halt_reason": null,
  "last_halt": null,
  "last_halt_reset": null,
  "skipped_lines": 0
}
```

- Response khớp implementation hiện tại (`server/audit.py:135-147`): `halt_reason` là reason của line `halt` gần nhất khi halted, `skipped_lines` đếm dòng JSON hỏng (không im lặng).

- Đọc toàn file rồi lọc trong bộ nhớ (file append-only, cân nhắc index khi > vài MB — risk §11.7).
- `halted_today` tính như `_halted_today` (`risk.py:163-173`): UI approval gate hiển thị banner đỏ khi true; chỉ server mới được gọi `RiskGuard.reset_halt` (`risk.py:591-596`) — v1 **không** có endpoint reset (tránh nút kill-switch-reset trên UI); ghi vào risk §11.8.
- Dòng JSON hỏng bị bỏ qua và đếm vào `details.skipped_lines` (`risk.py:148-157` cũng làm vậy).

Errors: **503 `audit_log_unreadable`** khi file tồn tại nhưng không đọc được (không fail-open hiển thị — execute-time đã fail-closed ở `risk.py:122-147`, UI phải nhất quán).

Trạng thái: loading · **empty** (file chưa tồn tại = log mới — `risk.py:140-141`) · error 503 · stale 10s (audit là dữ liệu phán xét — hiển thị `generated_at` rõ).

---

## 8. History & Backtest

### GET /api/history

Ghép hai nguồn đọc-only (không ghi gì):

1. **Decision log** — `memory_log_path` mặc định `~/.tradingagents/memory/trading_memory.md` (`default_config.py:99`); parse bằng `_parse_entry` (`decision_log.py:286-317`):
   `{date, ticker, rating, pending, raw, alpha, holding, resolved, decision, reflection}`.
2. **Run tree** — `<results_dir>/<ticker>/<date>/reports/*.md` + `message_tool.log` (`cli/run.py:122-127`) và `<results_dir>/<ticker>/TradingAgentsStrategy_logs/full_states_log_<date>.json` (`trading_graph.py:376-386`); báo cáo đã save: `<results_dir>/reports/<ticker>_<stamp>/complete_report.md` (`cli/run.py:374-384`, `reporting.py:98-101`).

Request: `GET /api/history?ticker=BTC-USD&pending_only=false&limit=50&offset=0`

Response 200:

```json
{
  "generated_at": "2026-09-30T08:00:00Z",
  "items": [
    {
      "date": "2026-09-29",
      "ticker": "BTC-USD",
      "rating": "Underweight",
      "pending": true,
      "raw": null,
      "alpha": null,
      "holding": null,
      "resolved": null,
      "decision_excerpt": "**Rating**: Underweight ...",
      "reflection": null,
      "run_artifacts": {
        "state_log": ".../BTC-USD/TradingAgentsStrategy_logs/full_states_log_2026-09-29.json",
        "reports_dir": null,
        "saved_report": null
      }
    }
  ],
  "total": 2,
  "by_ticker": { "BTC-USD": 2 }
}
```

- `pending_only=true` → chỉ entry `pending` (chưa settle — `decision_log.py:69-71`).
- `decision_excerpt` giới hạn ~500 ký tự; UI mở chi tiết qua `run_artifacts.state_log` (endpoint đọc file markdown/json chi tiết là phần mở rộng tương lai — ngoài scope 18 endpoint này, xem §11).

Trạng thái: loading · **empty** (log trống hoặc chưa có run nào) · error 500 (file hỏng → trả phần parse được + `warning` liệt kê dòng lỗi) · stale 60s.

### GET /api/backtest

Nguồn: sweep ghi `results_dir/backtest/<run_id>/trading_memory.md` (`backtest.py:145-149`), summary bằng `summarize` (`backtest.py:178-208` → `BacktestSummary{resolved, pending, by_rating{count,hit_rate,mean_alpha}, unscored, holding}`).

Request: `GET /api/backtest` — list; `GET /api/backtest?run_id=20260928_101500` — chi tiết 1 run.

Response 200 (list):

```json
{
  "generated_at": "2026-09-30T08:00:00Z",
  "runs": [
    {
      "run_id": "20260928_101500",
      "log_path": "C:/Users/mrc/.tradingagents/logs/backtest/20260928_101500/trading_memory.md",
      "entries": 12,
      "resolved": 9,
      "pending": 2,
      "unscored": 1,
      "holding": "5 trading days",
      "by_rating": {
        "Buy":       { "count": 4, "hit_rate": 0.5,  "mean_alpha": 0.012 },
        "Underweight": { "count": 5, "hit_rate": 0.6, "mean_alpha": -0.004 }
      }
    }
  ]
}
```

Response 200 (detail, `?run_id=…`): thêm `entries: [decision-entry shape như /api/history]` của log run đó.

- Run không có log (mọi cell fail — `backtest.py:180-181`) → `entries: 0, resolved: 0 …`, kèm `warning: "run wrote no decision log"`.
- Alpha/percent trả dạng số (parse từ chuỗi `%` trong log như `_alpha`, `backtest.py:64-74`).

Errors: 404 `not_found` khi `run_id` không có (`summarize` raise FileNotFoundError — `backtest.py:184-185`).

Trạng thái: loading · **empty** (chưa sweep nào) · error 404/500 · stale 60s.

---

## 9. Health — `GET /api/health`

Response 200:

```json
{
  "status": "ok",
  "version": "0.5.x",
  "generated_at": "2026-09-30T08:00:00Z",
  "exec_mode": "dry",
  "halted_today": false,
  "audit_log_readable": true,
  "results_dir": "C:/Users/mrc/.tradingagents/logs",
  "active_run_id": null,
  "pending_approvals": 1,
  "llm_key_configured": { "openai": true, "google": false, "anthropic": false },
  "llm_wire_protocol": "chat"
}
```

- `status`: `ok` | `degraded` (audit log unreadable, hoặc key LLM chưa configure khi không có mock).
- `exec_mode` = `effective_mode()` (`bridge.py:480-486`) — `live` chỉ khi cả hai gate FR5 mở; UI hiển thị mode này trên mode bar.
- Không bao giờ trả giá trị key — chỉ boolean configured.

Trạng thái: loading · empty (n/a) · error 500 · stale 15s + auto-poll.

---

## 10. Bảng tổng hợp endpoint

| # | Method | Path | Ghi đè engine? |
|---|--------|------|----------------|
| 1 | GET   | `/api/settings`                  | đọc config + env, mask credential |
| 2 | PUT   | `/api/settings`                  | set_config in-memory + upsert `.env`; cấm key gate |
| 3 | GET   | `/api/watchlist`                 | file state riêng, sync `exec_watchlist` |
| 4 | POST  | `/api/watchlist`                 | " |
| 5 | DELETE| `/api/watchlist/{symbol}`        | " |
| 6 | POST  | `/api/runs`                      | dựng `TradingAgentsGraph` + stream như CLI; chỉ phân tích |
| 7 | GET   | `/api/runs/{id}`                 | state nội bộ + artifacts trên đĩa |
| 8 | GET   | `/api/runs/{id}/events` (SSE)    | stream chunks như `cli/run.py:226-323` |
| 9 | POST  | `/api/daily`                     | worker per-coin (§5) — phân tích + `plan_order` + enqueue; nguồn duy nhất của plan/queue, không execute |
| 10| GET   | `/api/daily`                     | lịch sử job (database) + `active_job_id` — băng "đang chạy" của UI (§5 rev 7) |
| 10a| GET  | `/api/daily/{job_id}`            | poll state job + `DailyResult`-shaped results; live hoặc từ database |
| 10b| POST | `/api/daily/{job_id}/cancel`     | dừng job đang chạy, giải phóng worker lock ngay (§5 rev 7) |
| 11| GET   | `/api/portfolio`                 | `sync_portfolio` + replay FIFO audit log (§6) |
| 12| GET   | `/api/approvals`                 | queue mới của API layer |
| 13| POST  | `/api/approvals/{id}/approve`    | rebuild plan → `execute_order(confirm=True)` — duyệt đường chính thống, duy nhất đặt lệnh (§4) |
| 14| POST  | `/api/approvals/{id}/reject`     | audit-only, không đụng exchange |
| 15| GET   | `/api/audit`                     | đọc `exec_log_path` phân trang |
| 16| GET   | `/api/history`                   | decision log + run tree |
| 17| GET   | `/api/backtest`                  | `results_dir/backtest/*` + summarize |
| 18| GET   | `/api/health`                    | mode/halt/key status |
| 19| GET   | `/api/performance`               | đọc `exec_log_path` FIFO long-only (§12) |
| 20| GET   | `/api/autoloop`                  | gates (thêm 2 nửa futures — chỉ quan sát) + state file `autoloop.json` (§13, §14) |
| 21| POST  | `/api/autoloop`                  | fail-closed 409 gate_closed (§13) |
| 22| POST  | `/api/autoloop/run-now`          | 1 chu kỳ screen→propagate→plan→`_handle_plan`; crypto qua `DerivativesExecutor` khi `exec_derivatives` bật — futures long/short, đòn bẩy ≤3× (§14) |
| 23| POST  | `/api/halt`                      | append `{"event": "halt"}` — RiskGuard mọi process chặn hôm nay |
| 24| POST  | `/api/halt/reset`                | append `{"event": "halt_reset"}` |
| 25| GET   | `/api/runs`                      | lịch sử run (database) + `active_run_id` (§3 rev 7) |

**endpointCount = 26.**

---

## 11. Rủi ro thiết kế còn mở

1. **Approval queue chưa tồn tại trong engine** — `daily.py:186-201` chỉ audit `awaiting_approval` rồi thoát; queue là state mới, persist thật tại `server/data/approvals.jsonl` (tmp+replace — `approvals.py:65-73`). Khi server restart giữa chừng, item `pending` khôi phục từ file; item đang ở `approved` (claimed, §4) khi sập **có thể đã gửi lệnh thật** — đối soát qua engine audit + venue, không tự hồi. Pipeline approve→execute đã implement từ rev 3.1 (`approvals.py:241-333`) — trước đó approve chỉ claim rồi dừng, để item ở dead-end `approved` vĩnh viễn (review finding, đã xoá).
2. **Job/run là state trong database, không phải in-memory** (rev 7 — đã sửa). `server/data/control.db` (SQLite, stdlib `sqlite3`, WAL) lưu `jobs`/`job_results`/`runs`/`run_events`. Restart server không mất lịch sử: `GET /api/daily/{id}` và `GET /api/runs/{id}` đọc lại từ DB, và stream của một run đã xong replay được từ `run_events`. Job/run bị process chết bỏ lại ở `running` được set `interrupted` lúc khởi động (`main.py:_open_state_db` → `daily.reconcile()` / `runs.reconcile()`, mỗi cái ghi một audit line) — vì worker lock suy ra từ status, không reconcile thì job mồ côi sẽ trả 409 cho mọi job sau, vĩnh viễn và vô hình. Engine audit (`exec_log_path`) **vẫn là JSONL**: đó là contract replay của RiskGuard xuyên process, không phải state của API layer.
3. **`exec_watchlist` không env-overridable** (`default_config.py:38-41`) — persistence phải là file riêng của API layer, không phải `.env`; mọi nơi đọc watchlist phải đi qua layer này hoặc sync vào `DEFAULT_CONFIG` lúc khởi động, nếu không CLI và UI sẽ trôi về default `["BTC-USD","ETH-USD"]` (đã làm ở `watchlist.py:39-44`).
4. **Env-gates phải read-only trong API** — nếu PUT settings cho phép ghi `TRADINGAGENTS_EXEC_LIVE`/`_AUTO_CONFIRM`/`_DERIVATIVES` thì double-gate FR5 (`bridge.py:33-36, 527-538`) sụp thành một công tắc. Contract cấm; cần test chặn.
5. **`.env` ghi từ API** có xung đột với format/comment hiện có và với việc user sửa tay cùng lúc; coercion phải chạy trước khi ghi để không sinh `.env` làm process chết lúc startup (`_apply_env_overrides` raise — `default_config.py:83-93`). Cân nhắc lock/ghi-đổi-tên.
6. **`set_config` process-wide** (`dataflows/config.py`, gọi tại `trading_graph.py:65`) — v1 serialize 1 job/run qua worker lock; muốn song song phải tách process (subprocess per job) — quyết định để sau v1.
7. **Mock phụ thuộc artifact cũ + stub venue lệch thật**: `full_states_log_*.json` chỉ tồn tại sau run hoàn tất (`trading_graph.py:346`); repo không có `results/` (dữ liệu thật ở `~/.tradingagents/logs`) — synthetic fallback là bắt buộc. Giá/balance của stub venue là fixture — số liệu mock không phản ánh venue thật; plan mock chỉ là demo (`mock: true`, approve bị chặn 409 `mock_not_executable`) — cần test chặn để dữ liệu tổng hợp không bao giờ thành lệnh thật.
8. **`/api/audit` scan toàn file**: file append-only không giới hạn; khi lớn cần index/mmap — chưa thiết kế. Dòng JSON hỏng bị bỏ qua im lặng (như `risk.py:156-157`) — UI cần thấy `skipped_lines`.
9. **Không có endpoint reset halt trong v1** — `RiskGuard.reset_halt` (`risk.py:591-596`) là hành động operator nhạy cảm; để ngoài UI tránh nút one-click mở lại trading trong ngày bị halt.
10. **Web framework là dependency mới**: pyproject hiện không có fastapi/uvicorn (đã thêm qua `web` extra theo task control-panel — ghi rõ trong PR).
11. **Approve → execute đồng bộ + idempotency**: live order có thể chậm (network) — UI disable nút + timeout riêng. Claim `approved` dưới store lock là hàng rào idempotency (409 cho tab khác); nhưng **không được retry sau 502** khi order_id có thể đã tồn tại — live order trùng là mất tiền thật (`bridge.py:18-21, 464-469`); đối soát `GET /api/audit?phase=execute` (§7) trước mọi thao tác kế tiếp.
12. **`POST /api/daily` chiếm worker lock lâu (live mode)** — mỗi coin là một full LLM pipeline + tối đa 3 lần propagate với backoff 20s/40s (`daily.py:147-164`): watchlist 5 coin có thể khoá worker 10–30+ phút, chặn luôn `POST /api/runs`; mock mode chỉ vài giây/coin. rev 7 thêm `POST /api/daily/{id}/cancel`: đánh dấu job terminal ngay (giải phóng lock) và worker dừng giữa hai coin; coin đang trong lệnh LLM chạy nốt vì Python không kill thread an toàn được — cancel giải phóng worker, không giải phóng network.
13. **CSRF guard ≠ auth**: Content-Type + Origin/`Sec-Fetch-Site` chặn trang web lạ trong browser, nhưng không chống được process local hoặc malware trên cùng máy — mọi endpoint vẫn tin tưởng localhost; token auth chỉ bắt buộc khi mở bind ngoài `127.0.0.1` (§0).
14. **Portfolio endpoint phụ thuộc seam private của `risk.py`** (`_replay`, `_consecutive_losses`, `_halted_today` — `_halted_today` đã import ở `server/audit.py:74`): engine đổi replay semantics (ví dụ thêm field mới vào audit line) → P&L của UI lệch im lặng. Cân nhắc public hoá helper replay trong engine (M5) thay vì giữ import private.
15. **Approve với venue không đọc được fail-closed**: `portfolio` không lấy được → server trả `503 exchange_unreachable`, giữ item ở `pending` và không chạy `RiskGuard`/gửi lệnh. Điều này tránh bỏ qua các giới hạn phần trăm khi venue không cung cấp equity; UI/operator có thể thử lại sau khi venue hồi phục.
16. **Enqueue per-coin giữa job chạy**: mỗi enqueue rewrite cả `approvals.jsonl` (atomic — an toàn), nhưng v1 không có cancel job: job 20 coin có thể sinh tới 20 item; `daily_job_id` trên item (§4) là chìa khoá đối soát queue↔job khi user huỷ muốn dừng chuỗi.

### Changelog rev 3 (so với rev 2)

- §4: approve fail-closed được giữ đúng như đã code (hiện `approvals.py:245-250`) và bổ sung pipeline 4 bước (rebuild → double-check → `execute_order` → audit hai tầng) khi `exec_live=True`; **bỏ** policy `dry_fill_on_gate_closed`; thêm lỗi `mock_not_executable`/502 `execute_failed`; item + `daily_job_id`/`expires_at`/`mock`; trạng thái `approved` rõ nghĩa claim. *Rev 3.1: pipeline đã implement, refusals trước claim.*
- §5: worker server tự chạy vòng lặp per-coin trên seam engine (không gọi `run_daily`); `mode: "mock"` tái dùng machinery `runs.py`; enqueue per-coin (risk 13 cũ về enqueue-at-completion được giải quyết — xoá); job không bao giờ tự execute.
- §6 mới: `GET /api/portfolio` (positions + realized P&L FIFO replay) — endpoint 18.
- §7–§11: renumber từ §6–§10; thêm mã lỗi 403 `forbidden_key`, 410 `expired`, 502 `execute_failed`, 503 `exchange_unreachable` vào §0.1; Base URL/origin §0 khớp code thật (`server/main.py:5,26-33`, `web/next.config.ts:7-9`).

---

## 12. Performance — `GET /api/performance` (rev 5, màn Tổng quan)

Nguồn: replay fill từ engine audit log (`exec_log_path`) qua `audit.read_events()` (`server/performance.py:202-205`) — KHÔNG đụng venue, trừ mark giá cho position mở.

Định nghĩa **fill**: dòng audit có `action ∈ {buy, sell}`, `phase == "execute"`, ticker khác rỗng và `qty > 0`, `price_est > 0` (`performance.py:71-81`) — dòng plan, risk rejection, awaiting-approval không bao giờ dịch chuyển position.

- **FIFO theo ticker, hỗ trợ long và futures short** (`performance.py:84-127`): buy khớp short futures trước rồi phần dư mở long; sell khớp long trước, và phần dư chỉ mở short nếu `market="derivatives"`. Spot sell không có long basis tiếp tục bị bỏ khỏi P&L. Short hiển thị `qty` âm; realized/unrealized return tính theo hướng short.

### GET /api/performance

Response 200:

```json
{
  "generated_at": "2026-10-02T08:00:00Z",
  "summary": {
    "closed_trades": 2,
    "wins": 1,
    "losses": 1,
    "win_rate_pct": 50.0,
    "avg_win_pct": 10.0,
    "avg_loss_pct": -5.0,
    "expectancy_pct": 2.5
  },
  "open_positions": [
    {
      "ticker": "BTC-USD",
      "qty": 0.002,
      "entry_price": 50000.0,
      "current_price": 52500.0,
      "unrealized_pct": 5.0,
      "mode": "dry",
      "entry_at": "2026-10-01T03:00:00Z"
    }
  ],
  "brackets": { "placed": 1, "failed": 1 },
  "recent_orders": [
    {
      "timestamp": "2026-10-01T07:00:00Z",
      "ticker": "BTC-USD",
      "action": "rejected_by_risk",
      "qty": null,
      "price_est": 50000.0,
      "mode": "dry",
      "order_id": "order-2026-10-01T07:00:00Z"
    }
  ]
}
```

- `summary` — win/loss trên round-trip đóng: `wins` = pnl > 0, `losses` = pnl ≤ 0 (loss giữ dấu âm), `expectancy_pct` = trung bình toàn bộ round-trip ⇒ `win_rate × avg_win + loss_rate × avg_loss == expectancy` (`performance.py:116-130`).
- `open_positions[]` — một row per lot signed chưa khớp: lot long có `qty` dương, futures short có `qty` âm; `current_price` mark qua seam `price_fetcher` (default lazy `ExchangeBridge`, dựng lại khi exchange/url/sandbox config đổi — `performance.py:49-68`); không mark được → null. `mode`/`entry_at` lấy từ fill gốc.
- `brackets` — đếm dòng `bracket_placed` / `bracket_failed` trên toàn log (`performance.py:156-161`) — sức khoẻ SL/TP giao venue.
- `recent_orders[]` — tối đa 50 dòng (`RECENT_ORDERS_LIMIT`, `performance.py:27`) `phase == "execute"` mới nhất, mới nhất trước; shape tường minh 7 field, không lộ `_line_no` provenance (`performance.py:164-180`).

Errors: `503 audit_log_unreadable` (log tồn tại nhưng không đọc được — nhất quán `/api/audit`); **log thiếu hoặc rỗng là empty state 200 toàn số 0, không phải lỗi** (`performance.py:202-205`).

Trạng thái: loading · **empty** (chưa có fill nào — `summary` toàn 0, danh sách rỗng) · error 503 (retry) · stale 60s (badge "cũ" — `web/components/overview/performance-card.tsx:45`).

---

## 13. Autoloop + halt — vòng lặp tự chạy & kill switch (rev 5)

Nguồn: `server/autoloop.py`. Scheduler là daemon thread re-read state file (`server/data/autoloop.json` — `paths.AUTOLOOP_PATH`, `server/paths.py:17`) mỗi wake, nên đổi interval/enabled có hiệu lực ở wake kế tiếp, không cần restart (`autoloop.py:152-172`). Một cycle = sàng universe (30 mã/asset → top 8, như daily auto scan) → pipeline LLM per candidate → **mọi plan resolve qua confirm policy của engine `_handle_plan`** (`tradingagents/execution/daily.py`) — plan không execute chỉ được audit; candidate stock chỉ advisory (không sinh plan). Cycle khi run/job active → skip + audit `autoloop_cycle_skipped` (`autoloop.py:198-218`).

- **Enable fail-closed** (`autoloop.py:88-105, 373-381`): `all_open` = config `exec_auto_confirm is True` **và** env `TRADINGAGENTS_EXEC_AUTO_CONFIRM` true — đúng hai nửa đầu của confirm policy; hai nửa `exec_live` chỉ hiển thị để quan sát, không mở API này (live là quyết định của engine policy theo FR5). Rev 6: `gates` thêm 2 nửa `exec_derivatives_config`/`exec_derivatives_env` — cũng chỉ quan sát (futures là quyết định của executor policy, §14).
- **Kill switch không gate** (`autoloop.py:405-432`): `POST /api/halt` ghi `{"event": "halt", "timestamp", "reason"}` vào engine audit log — RiskGuard của MỌI process replay line này và từ chối phần còn lại của hôm nay cho tới reset hoặc UTC day roll (sticky, day-scoped — `risk.py:163-173`; timestamp là thứ làm halt day-scoped — `risk.py:111-119`). `halt_reset` hôm nay gỡ sticky cho tới breach/halt mới. Đổi so với §11.9 (risk cũ "không có endpoint reset halt trong v1"): rev 5 đưa reset thành endpoint tường minh `/api/halt/reset`.

### GET /api/autoloop

Response 200:

```json
{
  "generated_at": "2026-10-02T08:00:00Z",
  "enabled": false,
  "interval_minutes": 1440,
  "running_cycle": false,
  "gates": {
    "auto_confirm_config": false,
    "auto_confirm_env": false,
    "exec_live_config": false,
    "exec_live_env": false,
    "exec_derivatives_config": false,
    "exec_derivatives_env": false,
    "all_open": false
  },
  "last_cycle": null
}
```

- `interval_minutes` hợp lệ trong `[15, 1440]`, mặc định 1440; file hỏng/giá trị lạ → rơi về default, loop coi như tắt (`autoloop.py:111-141`).
- `running_cycle` — một cycle đang giữ lock (in-memory, `autoloop.py:346`).
- `last_cycle` — tóm tắt cycle hiện tại hoặc gần nhất, chỉ in-memory (mất khi restart): `{started_at, finished_at, status: "running"|"completed"|"timeout"|"failed"|"skipped_busy", candidates[], results[], error, telemetry?, universe?: {crypto, stock}, reason?}`. Summary được publish ngay khi cycle bắt đầu và cập nhật theo stage; `finished_at` là null khi chạy. `results[]` mỗi phần tử `{ticker, signal, executed, order_id, reason, stop_loss, take_profit}`.
- `last_cycle.telemetry` — `{active_stage, active_ticker, active_elapsed_ms, active_node, active_path[], active_spans[], last_stage, current_ticker, events[], dropped_events}`. Các giá trị active có thể null; `active_path` giữ chuỗi stage cha để phân biệt LLM/tool/vendor trong node nào. `active_elapsed_ms` được tính lại mỗi GET khi chạy; sau timeout snapshot giữ nguyên, worker muộn không được ghi đè. `active_spans[]` gồm `{stage, ticker, node, started_at, elapsed_ms}`; timestamps UTC ISO-8601, thời gian dùng monotonic clock và đơn vị milliseconds.
- `telemetry.events[]` giữ tối đa 100 sự kiện gần nhất: `{event: "started"|"finished"|"failed", stage, ticker, node, started_at, duration_ms?, error_type?}`. Event hoàn thành/thất bại có duration; lỗi chỉ giữ tên loại exception. `dropped_events` đếm số event cũ bị loại. Không lưu prompt, tool arguments/output, API key, URL hoặc nội dung exception trong telemetry.
- Stage bao gồm screening/universe, venue setup, futures market context, graph build/state preparation, từng LangGraph node, LLM, tool, từng vendor attempt, planning/portfolio sync/bracket/execution policy và finalization. Timeout nêu ticker cùng stage/node đang hoạt động; engine deadline/cancellation guard vẫn chặn lệnh mới từ worker đã hết hạn. Chu kỳ timeout cũng ghi snapshot timing vào server audit. UI dùng nhãn `Đang chạy` và `Hết thời gian`, không hiển thị hai trạng thái này là `Hoàn tất`.

Trạng thái: loading · empty (n/a) · error 500 · stale 30s, poll 5s khi `running_cycle` (`web/components/settings/autoloop-card.tsx:34-36`).

### POST /api/autoloop

Request bắt buộc `Content-Type: application/json` (§0):

```json
{ "enabled": true, "interval_minutes": 60 }
```

- `enabled` bắt buộc boolean; `interval_minutes` tuỳ chọn int `[15, 1440]` (bool không tính là int) — sai → `400 validation_error` (`autoloop.py:357-372`).
- Bật khi `all_open` đóng → `409 gate_closed` kèm `details.gates`; không persist gì, scheduler không arm (`autoloop.py:373-381`). **Tắt không bao giờ gate** (điều kiện gate chỉ chặn `enabled: true` — `autoloop.py:374`).
- Thành công: persist `{enabled, interval_minutes, updated_at}` atomically (tmp+replace), audit `autoloop_updated`, arm scheduler (idempotent), wake loop. Response 200: payload GET sau khi ghi.

Trạng thái: loading · error 400/409/415 · stale (n/a — action).

### POST /api/autoloop/run-now

Body `{}` (vẫn phải `application/json` — §0). Chạy 1 cycle trên worker thread; nếu run/daily job đang active, hint trả `skipped_busy` và cycle tự re-check + audit `autoloop_cycle_skipped` (`autoloop.py:390-402`).

Response 202:

```json
{ "generated_at": "2026-10-02T08:00:00Z", "status": "started" }
```

- `status`: `started` | `already_running` (cycle đang giữ lock) | `skipped_busy` (run hoặc daily job active).

### POST /api/halt · POST /api/halt/reset

Body `{}`. `halt` → 200 `{ "generated_at": "…", "halted": true }`; `halt/reset` → 200 `{ "generated_at": "…", "halted": false }`. Cả hai ghi line điều khiển vào engine audit log (§7) và audit server trail `autoloop_halt` / `autoloop_halt_reset`; sau action, `GET /api/health` → `halted_today` (§9) phản ánh ngay.

Trạng thái POSTs: loading · error 415/403 CSRF (§0) · stale (n/a — action).

---

## 14. Đường futures trong autoloop (rev 6)

Ứng viên crypto của 1 chu kỳ autoloop đi qua `DerivativesExecutor` thay vì spot bridge khi config `exec_derivatives` là literal True — executor được dựng 1 lần/cycle (`autoloop.py:253-254`, seam test `_build_derivatives_executor` — `autoloop.py:338-344`); ứng viên stock vẫn chỉ advisory như §13.

**3 cổng — UI không giữ cổng nào:**

1. **Config** — `exec_derivatives is True`: chặn lúc cycle quyết định dùng executor (`autoloop.py:254`) và chặn lần nữa trong `plan_order`/`execute_order` của executor (`derivatives.py:125-131, 191`).
2. **Env** — `TRADINGAGENTS_EXEC_DERIVATIVES` đọc tại call time, fail-closed (`derivatives.py:45`, gate ở `derivatives.py:132, 198`): thiếu → plan bị refuse (audit `derivatives_denied`), candidate ghi refusal vào `reason`, cycle vẫn hoàn thành (`tests/test_server_autoloop.py::test_run_cycle_derivatives_gates_closed_refuse_planning`).
3. **Confirm per-order** — mọi plan vẫn resolve qua `_handle_plan` của engine (`daily.py:177-216`, gọi ở `autoloop.py:310`): hai gate auto-confirm (config literal + env flag) quyết định dry-fill, live bị chặn bởi FR5 của bridge mà executor compose (`effective_mode` delegate — `derivatives.py:415`). Autoloop không thêm cổng mới, chỉ đi qua cổng cũ của engine.

Hai nửa (1)+(2) hiển thị trong `gates()` là `exec_derivatives_config`/`exec_derivatives_env` — **chỉ quan sát**, không tham gia `all_open` (`autoloop.py:116-118`); enable autoloop vẫn chỉ cần 2 nửa auto-confirm.

**Đòn bẩy:** cycle xin `min(risk_max_derivatives_leverage, MAX_AUTO_LEVERAGE = 3.0)`; thiếu config entry → 1× (`_auto_leverage` — `autoloop.py:62, 347-353`). RiskGuard với `risk_max_derivatives_leverage` vẫn là gate có thẩm quyền (từ chối được dưới trần hệ thống); executor pin leverage trên venue cho mỗi lệnh (`derivatives.py:321-324`).

**Position state (rev 8):** after a live derivatives entry is acknowledged and bracketed, the executor persists an initial `position_states` row keyed by unified symbol, side, and entry order id. It records planned entry, sent quantity, stop, target, and order provenance. `PositionManager` uses the DB CAS revision independently of the venue revision and deletes its snapshot when its adapter reconciles the venue flat. This is durable policy state, not a background polling service: the current autoloop does not instantiate a venue `PositionAdapter` for automatic trailing/partial management; exchange-side bracket and bracket recovery remain the active automatic protection path.

**Venue Binance:** mặc định chọn Demo qua `TRADINGAGENTS_EXEC_BASE_URL=https://demo-api.binance.com`; ccxt tự route futures tới `demo-fapi.binance.com`. Chuyển production bằng cách thay URL thành `https://api.binance.com`, dùng `BINANCE_API_KEY`/`BINANCE_SECRET`, rồi restart backend. Bridge chỉ nhận đúng hai profile HTTPS allowlist này và chọn key pair tương ứng. Các gate `exec_live` + `TRADINGAGENTS_EXEC_LIVE` vẫn độc lập; Demo order vẫn là live execution trên tài khoản Demo, không phải dry-run.
