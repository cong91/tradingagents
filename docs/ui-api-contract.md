# Web UI API Contract (`docs/ui-api-contract.md`)

updated: 2026-10-17 (rev 9: Futures currency metadata per account/position field, USD-equivalent Bybit Unified and OKX aggregates, explicit unsupported venue routing, Bybit settleCoin scope, and accurate stale activity timestamps)
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
| 503 | `exchange_unreachable` | Spot venue không đọc được balance/mark trong `/api/portfolio`; Futures read lỗi riêng được biểu diễn trong section `futures` và không làm hỏng Spot snapshot |

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
2. **Execution gates là operator settings.** `exec_live`, `exec_auto_confirm` và `exec_derivatives` được ghi qua `/api/operator-config`, cần `confirm=true` + `reason`, và được áp dụng trong process. Các biến môi trường cùng tên không còn là gate runtime.
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
    "exec_live_config": false,
    "exec_auto_confirm_config": false,
    "exec_derivatives_config": false
  },
  "effective_exec_mode": "dry"
}
```

- `settings[]` phản ánh `_ENV_OVERRIDES`; `exec_base_url` xuất hiện read-only dù được đọc từ `TRADINGAGENTS_EXEC_BASE_URL`. PUT key này → `403 forbidden_key`. `exec_sandbox` chỉ dùng cho sandbox của sàn khác, không chọn Binance venue.
- `settings[]` bao gồm các numeric policy `risk_derivatives_*`: `profit_protection_enabled` (default true, chỉ quản lý state do bot mở), `break_even_r`=1, `lock_profit_r`=2, `trail_start_r`=3, `trail_distance_r`=1, `target_extension_r`=1, `min_stop_move_r`=0.1, `mark_max_age_seconds`=120. Scale-out: `scale_out_enabled`=false, `scale_out_first_r`=1, `scale_out_first_fraction`=0.33, `scale_out_second_r`=2, `scale_out_second_fraction`=0.33. Chúng không thuộc `gates_readonly` và không tự mở gate.
- Profit-protection thresholds là bội số của R (entry thực tế tới initial stop). Backend yêu cầu các R dương, `break_even < lock_profit < trail_start`; giá trị sai không gọi venue và trả state `invalid_policy_config` ở monitor. `min_stop_move_r` chặn thay SL khi chênh lệch quá nhỏ; `mark_max_age_seconds` khiến mark cũ trả `mark_stale` và không mutate. Monitor cũng từ chối khi account không phải One-way (`hedge_mode_unsupported`) hoặc không đọc được position mode (`position_mode_unavailable`).
- Với Binance, `exec_base_url` chỉ chấp nhận HTTPS API host Demo hoặc production được allowlist. URL được chọn quyết định credential pair: Demo dùng `BINANCE_DEMO_API_KEY`/`BINANCE_DEMO_SECRET`; production dùng `BINANCE_API_KEY`/`BINANCE_SECRET`. Demo là default. Đổi profile cần sửa URL + đúng pair trong `.env` rồi restart; API GET chỉ expose URL và status/mask, PUT không đổi được URL.
- `effective_exec_mode` độc lập với venue URL: `live` khi `exec_live is True`; chọn Demo không tự mở quyền nếu gate đang đóng.
- Paths read-only (`results_dir`, `data_cache_dir`, `memory_log_path`, `exec_log_path`) và `exec_base_url` trả trong `settings[]` với `editable: false`.

Trạng thái: loading · empty (không xảy ra — luôn có defaults) · error 500 · stale sau 30s.

### GET /api/settings/events

SSE phát **gợi ý vô hiệu hoá** (hints), không phát dữ liệu — giống `GET /api/snapshot/events`. Mục đích: chip cổng thực thi không cần poll `/api/settings` theo đồng hồ.

- Luồng mở: phát `event: reset` (`{"reason":"connect"}`) ngay khi kết nối — client refetch một lần rồi giữ giá trị.
- `PUT`/`DELETE /api/operator-config` thành công → server phát `event: changed` (`{"id":N,"type":"changed","reason":"operator_config_updated|operator_config_reset"}`) cho mọi kết nối; client refetch.
- Restart máy chủ làm đứt stream: EventSource tự nối lại → nhận `reset` mới → refetch (che cả các thay đổi env đòi restart).
- Client vẫn refetch khi tab quay lại hiển thị (`visibilitychange`) và khi mạng hồi (`online`) — không có polling theo đồng hồ ở chip.
- Nguồn: `server/settings_events.py`; publish gọi từ `server/operator_config.py`.

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

1. Validate từng key: phải thuộc `_ENV_OVERRIDES` (trừ `exec_base_url` read-only); coerce theo kiểu default như `_coerce` (`default_config.py:60-80`) — sai kiểu → `422 coercion_error` kèm `details: {"env_var": "...", "message": "expected a boolean (true/1/yes/on/false/0/no/off), got 'treu'"}`.
2. Áp in-memory: `set_config({key: value})` (`dataflows/config.py`) — có hiệu lực với run mới tạo sau đó; run đang chạy không thấy (bridge chụp config lúc dựng — `bridge.py:110-121`).
3. Áp in-memory: `set_config({key: value})` (`dataflows/config.py`) — có hiệu lực cho run/bridge mới; bridge/executor kiểm tra gate runtime hiện hành để một lần tắt quyền chặn lệnh mới.
4. Persist vào PostgreSQL, bảng `control_engine_settings` (migration `0006`) — một transaction cho cả batch. `.env` **không bao giờ bị ghi lúc runtime**: nó là cấu hình triển khai (bí mật, venue URL) và chỉ là tầng default dưới giá trị lưu UI. Lúc khởi động server nạp lại: env → engine settings (bảng này) → operator overrides, nên giá trị UI sống qua restart và operator override vẫn thắng.

Response 200: cùng shape GET + `"applied_at"`. Restart không bắt buộc vì (2) đã áp in-memory.

Errors: 403 `forbidden_key` (key read-only hoặc key lạ), 422, 500 (không ghi được persistence).

Trạng thái: loading · empty (n/a) · error · stale (n/a — action).

### Operator overrides — `GET /api/operator-config`, `PUT /api/operator-config`, `DELETE /api/operator-config/{key}`

Nguồn: `server/operator_config.py`. Đây là các key mà `PUT /api/settings` **cố ý từ chối**: ba quyền vận hành (`exec_live`, `exec_auto_confirm`, `exec_derivatives`) và các trần rủi ro vận hành (`risk_max_derivatives_leverage`, `risk_max_derivatives_exposure_pct`, `risk_max_derivatives_exposure_hard_pct`, `risk_derivatives_override_min_r`, `risk_derivatives_override_max_risk_fraction`, `risk_derivatives_override_max_per_day`, `risk_derivatives_scale_out_enabled`, `risk_max_open_heat_pct`).

Lưu trữ: **PostgreSQL** — bảng `control_operator_settings` (`server/migrations/0003_operator_settings.sql`), đọc lại lúc startup bởi `server/main.py:_apply_operator_config`. Không còn file `operator_config.json`; `server/data/` đã rỗng và không được mount vào container.

GET trả `settings[]` với `key, type, group, label, hint, gate, value, default, override, stored_value, updated_at, updated_by`, cộng `gates`, `effective_exec_mode`, `allowlist`.

PUT body: `{"settings": {key: value}, "confirm": bool, "reason": str, "actor": str}`.

1. Key ngoài allowlist → `403 forbidden_key` (kèm `details.rejected` và `details.allowlist`).
2. Sai kiểu → `422 coercion_error`.
3. Có key thuộc nhóm cổng → bắt buộc `confirm: true` (`412 gate_confirmation_required`) **và** `reason` không rỗng (`422 reason_required`).
4. Ghi PostgreSQL → `set_config` in-memory → audit `operator_config_updated` với `changes: {key: {from, to}}`, `gate_changes`, `reason`, `actor`.

DELETE `/{key}` xoá override để quay về default engine (`DEFAULT_CONFIG`), audit `operator_config_reset`. Key không thuộc allowlist → 403; không có override → 404.

Các quyền này là config-only runtime switches; thay đổi được xác nhận, audit và áp dụng cho quyết định thực thi mới. Không có env gate thứ hai.

Trạng thái: loading · empty (n/a) · error · stale (n/a — action).

---

## 2. Watchlist — `GET /api/watchlist`, `POST /api/watchlist`, `DELETE /api/watchlist/{symbol}`

Nguồn: `exec_watchlist` default `["BTC-USD","ETH-USD"]` (`default_config.py:233-234`); chuẩn hóa biên giới của daily runner (`daily.py:95-111`).

Lưu trữ: engine đọc `exec_watchlist` từ config; vì key không env-overridable, API layer giữ state trong PostgreSQL (bảng `control_watchlist`, `server/control_store.py`) và sync vào `DEFAULT_CONFIG["exec_watchlist"]` khi khởi động server và sau mỗi ghi. Endpoint `run_daily(watchlist=…)` nhận watchlist tường minh (`daily.py:114-128`) nên truyền trực tiếp, không phụ thuộc file.

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

- `status`: `queued | running | completed | failed | cancelled | interrupted`.
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
| `run_interrupted` | system | `{error: {code: "interrupted", message}}` — terminal; server restart/orphan reconciliation |

Quy tắc:

- Mỗi event có `seq` tăng dần; client reconnect gửi header `Last-Event-ID: <seq>` → server replay từ seq+1 (buffer trong bộ nhớ của run, hoặc từ `run_events` khi run không còn trong live set — rev 7).
- Terminal event (`run_completed`/`run_failed`/`run_interrupted`) rồi server đóng stream.
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
2. **Approve fail-closed theo quyền Lệnh thật.** `POST approve` chỉ mở khi `exec_live` là literal True trong config; `exec_live` ≠ True → `412 gate_closed`, **không có dry-fill thay thế**. `412` không đụng item — item còn `pending`, operator mở quyền rồi approve lại được. Mode dry/live do `execute_order` quyết định tại thời điểm thực thi; approve không tự mở quyền.
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

Nguồn: **Spot cash/equity/positions** lấy từ `bridge.sync_portfolio()` khi effective live, còn dry tái dựng positions từ audit; section **Futures** đọc độc lập từ cùng bridge/client. Snapshot Futures không chứng minh đầy đủ scope; `observed_at` chỉ ghi thời điểm lời đọc vị thế trả về. Spot realized P&L replay FIFO từ audit không gồm derivatives; Futures P&L không cộng vào Spot. Lỗi Spot hoặc audit trong live mode không che dữ liệu Futures khả dụng; P&L và halt không xác định được trả null. Lỗi Futures báo riêng trong section Futures. Khi bị giới hạn, các lượt đọc hiển thị tiếp theo có thể bị hoãn theo cooldown.

Request: `GET /api/portfolio` (không tham số bắt buộc).

Response 200 (số liệu giả định, không phải dữ liệu tài khoản):

```json
{
  "generated_at": "2026-09-30T08:00:00Z",
  "currency": "USDT",
  "cash": 9500.0,
  "equity_est": 9915.5,
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
  "positions_source": "venue",
  "realized_pnl": {
    "total": 12.34,
    "today": -1.2,
    "by_ticker": { "BTC-USD": 12.34 },
    "closed_trades": 9,
    "unknown_basis_closes": 1,
    "consecutive_losses": 1
  },
  "futures": {
    "equity": 2100.0,
    "wallet_balance": 2000.0,
    "available_balance": 1800.0,
    "unrealized_pnl": 100.0,
    "venue_profile": "demo",
    "equity_currency": null,
    "wallet_balance_currency": null,
    "available_balance_currency": null,
    "unrealized_pnl_currency": null,
    "account_scope": "derivatives",
    "positions": [{
      "symbol": "BTC/USDT:USDT",
      "side": "short",
      "contracts": 0.01,
      "quantity": -0.01,
      "entry_price": 100000.0,
      "mark_price": 99000.0,
      "liquidation_price": null,
      "unrealized_pnl": 10.0,
      "notional": 990.0,
      "margin": 330.0,
      "leverage": 3.0,
      "margin_mode": "isolated",
      "price_currency": "USDT",
      "notional_currency": "USDT",
      "margin_currency": "USDT",
      "unrealized_pnl_currency": "USDT"
    }],
    "positions_read": { "status": "partial", "skipped": 0, "issues": [{ "symbol": null, "reason": "scope_unverified" }], "observed_at": "2026-09-30T08:00:00.123Z" },
    "status": "ok",
    "error": null
  },
  "halted_today": false,
  "warnings": [],
  "stale_after": 30
}
```

Định nghĩa từng field:

- `cash`, `currency`, `positions[]`, `equity_est` — chỉ Spot; khi effective live, `sync_portfolio` lấy `quantity` từ `balance.total`, `cash` từ `balance.free` của quote; `cash: null` khi balance không trả quote. `equity_est = cash + Σ quantity × marked_price`; Futures wallet/equity/notional không được cộng vào đây. Khi một position có `quantity: null`, số lượng chưa xác định được giữ nguyên; `market_value`, `unrealized_pnl` của position và `equity_est` đều null, kèm warning. Không coi số lượng chưa biết là 0.
- `positions_source`: `"venue"` khi đọc Spot live thành công, `"unavailable"` khi lỗi hoặc snapshot không khả dụng, `"audit"` khi dry. `realized_pnl` và `halted_today` là null khi audit không đọc được; không suy ra P&L bằng 0 hoặc trạng thái không halt. Ở dry, Spot positions được tái dựng từ open FIFO lots trong audit, có thể gồm giao dịch mô phỏng; không phải snapshot vị thế live. `cash`, `marked_price`, `market_value` và `unrealized_pnl` không khả dụng; không suy ra equity thực từ `equity_est` (có thể là 0 khi có lots không được mark).
- Spot `marked_price` = `bridge.fetch_price(ticker)`; null khi venue không mark được. Position có quantity đã biết nhưng không mark được tính 0 trong `equity_est` và sinh warning; position có `quantity: null` khiến `market_value`, `unrealized_pnl` và `equity_est` là null vì không thể tính an toàn.
- `average_price` + `basis_source` lấy từ open FIFO lots trong Spot audit; `unrealized_pnl` chỉ tính khi có basis + mark, ngược lại null. Futures fills không ảnh hưởng `realized_pnl`, `by_ticker`, `closed_trades` hay `consecutive_losses` của Spot section.
- `realized_pnl.total` là Spot P&L đã chốt có basis theo replay audit; `today` chỉ gồm lệnh đóng theo UTC hôm nay, khác con số RiskGuard (có thể cộng thêm unrealized của lot mở trong ngày). `by_ticker`, `closed_trades`, `unknown_basis_closes` và `consecutive_losses` đều tính trên Spot-only FIFO replay. Giá vốn và P&L này là số liệu suy ra từ audit, có thể chứa mô phỏng hoặc ước tính, không phải kết quả đã đối soát với sàn.
- `futures.status`: `ok` khi account/balance đã đọc hợp lệ, kể cả khi đọc positions bị lỗi thông thường hoặc chỉ được một phần; `error` khi account/balance thất bại, venue không hỗ trợ, hoặc vị thế trả 418/429; `unavailable` khi chưa vào effective live mode hoặc read bị hoãn trong cooldown (không gọi futures private endpoints). Trong các trường hợp `error`/`unavailable`, account fields null, `positions: []` và `positions_read` unavailable; không coi đây là tài khoản không có vị thế (`server/portfolio.py`).
- `futures.positions_read` có shape `{ "status": "complete" | "partial" | "unavailable", "skipped": number | null, "issues": [{ "symbol": string | null, "reason": string }], "observed_at": string | null }`. Binance USD-M dùng `fapiPrivateV3GetPositionRisk({})` sau khi kiểm tra profile, routes và account mode; Bybit/OKX dùng `fetch_positions`. Chứng minh hoàn chỉnh: Binance trả list mà **mọi dòng đều diễn giải được** (không dòng lỗi, không `skipped`) **và** client không bị thay giữa chừng lần đọc — cùng hợp đồng certification raw-V3 mà monitor bảo vệ vị thế dùng để xóa state — khi đó status là `complete` và issues rỗng. Portfolio còn đối chiếu danh sách đã chứng minh với các vị thế monitor đang quản lý có binding trùng exchange + venue profile: vị thế bị thiếu trả issue `{ "symbol": "<mã vị thế>", "reason": "monitor_position_missing" }` (status quay lại `partial`); không đọc được sổ vị thế để đối chiếu trả `{ "symbol": null, "reason": "monitor_state_unreadable" }`. Các read chưa chứng minh (sàn khác Binance, dòng lỗi, client đổi giữa chừng, `skipped > 0`) giữ status `partial` với `{ "symbol": null, "reason": "scope_unverified" }` ở đầu issues. `skipped` đếm riêng các dòng lỗi (0 nếu không có); issue dùng mã lý do đã làm sạch và không để lộ symbol thô. `observed_at` là UTC sau khi lời gọi vị thế trả về, trước normalization; nếu venue trả non-list thì timestamp vẫn có vì đã nhận response. Nếu lời gọi ném lỗi thông thường, trạng thái `partial`, `skipped: null`, issue `read_failed`, `observed_at: null`; lỗi 418/429 được truyền tới portfolio để bật cooldown chung và phần Futures có status `error`. Binance Portfolio Margin chưa được hỗ trợ: balance có envelope list không gửi request USD-M V3 và position issue là `account_type_unverified`. `invalid_response` được dùng khi lời gọi không ném lỗi nhưng trả non-list. `unavailable` với `skipped: null`, `issues: []`, `observed_at: null` dùng khi chưa đọc snapshot hoặc account lỗi (`server/futures_snapshot.py`, `server/portfolio.py`). Trên Binance, `status: "complete"` với `positions: []` xác nhận sàn chứng minh tài khoản phẳng; ở các sàn chưa chứng minh, `positions: []` không xác nhận tài khoản không có vị thế, kể cả khi `skipped: 0`.
- `futures.positions[]`: `side` là hướng venue; `quantity` có dấu khi xác định được (long dương, short âm), `contracts` là độ lớn không dấu. Thiếu metadata hợp đồng đáng tin cậy hoặc thiếu entry hợp lệ của inverse có thể khiến `quantity: null`. `notional` luôn là magnitude tuyệt đối, không phải cash inflow hay P&L; `margin` là collateral/initial margin nếu venue trả, không suy ra từ notional khi thiếu; `unrealized_pnl` là số liệu venue báo, chưa chốt. Các trường `price_currency`, `notional_currency`, `margin_currency`, `unrealized_pnl_currency` gắn đơn vị riêng cho từng đại lượng; null nghĩa là venue/CCXT không xác định chắc chắn và UI không được suy ra từ Spot quote. OKX có thể trả notional USD trong khi margin/P&L theo settlement coin. `entry_price`, `mark_price`, `liquidation_price`, `leverage`, `margin_mode` cũng có thể null nếu venue không cung cấp.
- `futures.equity_currency`, `wallet_balance_currency`, `available_balance_currency`, `unrealized_pnl_currency` lần lượt mô tả đơn vị của từng tổng account; không dùng chung một currency khi nguồn khác nhau. Bybit Unified `totalEquity`, `totalWalletBalance`, `totalAvailableBalance` và `totalPerpUPL` là giá trị quy đổi USD; OKX `totalEq`/`upl` là USD, còn wallet detail chỉ có currency khi một coin duy nhất rõ ràng (`ccy`). Những tổng này không trộn vào Spot metrics. Bybit positions được yêu cầu theo `settleCoin=exec_quote_currency`; các sàn khác ngoài Binance/Bybit/OKX trả section Futures error rõ ràng vì chưa có routing đã xác minh. `venue_profile` là `demo`/`production` khi xác định được từ Binance URL, nếu không thì null.
- Chi phí giữ vị thế (display-only): `round_trip_fee` = `|notional| × risk_derivatives_round_trip_fee_rate`, kèm `round_trip_fee_currency` là currency của notional đã xác minh trong snapshot; thiếu notional/rate/currency → null. `opened_at`, `held_hours`, `funding`, `funding_currency` hiện luôn null: position state không có ràng buộc account bền vững (venue_key chỉ định danh venue/profile, không phải account) nên UI không được ghép vị thế venue với `opened_at` hoặc cửa sổ funding lưu trong state; funding cũng không được cộng thành 0 khi coverage/cửa sổ/currency chưa được chứng minh. Khôi phục các số này cần lượt account-binding được phê duyệt riêng.
- `stale_after: 30` — màn Tổng quan poll snapshot mỗi 30 giây. `generated_at` chỉ là thời điểm tạo HTTP response, không phải thời điểm dữ liệu tại sàn; `futures.positions_read.observed_at` là thời điểm client nhận xong lời đọc vị thế, không chứng minh snapshot nguyên tử với balance hay tuổi dữ liệu nguồn. UI giữ bản cũ kèm cảnh báo khi poll lỗi; danh sách `partial` không bao giờ được trình bày như danh sách đầy đủ hay tài khoản phẳng.
- Khi bật đồng bộ hiển thị (mặc định TẮT, cần cả `TRADINGAGENTS_UI_SNAPSHOT_SYNC=true` và `TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS=true`), response thêm `snapshot` với context HMAC theo credential của client, epoch, generation, pending, dirty, cache, các timestamp request/response gốc và `freshness: "unknown"`, `atomic: false`. Dockerfile mặc định một worker nhưng cấu hình không chứng minh chỉ một replica; operator phải xác nhận topology trước khi bật. Mỗi process gộp read đồng thời, giới hạn tối đa 4 read đang bay, khoảng cách 5 giây giữa hai lần bắt đầu cùng key xuyên generation và tối đa 12 lần bắt đầu/phút/context; TTL 25 giây, retention 300 giây, timeout 8 giây. Đây là giới hạn **bắt đầu loader UI**, không phải tổng HTTP, request weight, các đường giao dịch/monitor, process khác hay tổng lưu lượng IP Binance. Khi bị chặn có thể trả cache cũ với `cache: "stale"`, dirty/error và timestamp gốc; không có cache thì unavailable. Read đang bay bị event chen ngang bị từ chối; read không bị chen ngang sau mutation có thể xóa dirty nhưng không chứng minh read-after-write hay tuổi dữ liệu nguồn. Các mutation chỉ là hint, không chứng minh fill; TP/SL thực thi tại sàn chỉ được phát hiện qua monitor hint có bằng chứng hoặc polling định kỳ. Cooldown dùng chung với lịch sử hiển thị khi thấy 418/429; không áp dụng lên đường giao dịch.
- `GET /api/snapshot/events` (SSE, GET-only, miễn CSRF) phát hint invalidation `{id, epoch, type: "dirty" | "reset", context_id, generation, resource, symbol, operation, phase, outcome, occurred_at}`. Mỗi kết nối bắt đầu bằng reset rồi theo dõi hint mới, không hứa replay xuyên restart; UI debounce GET portfolio, đóng listener khi rời màn và vẫn poll định kỳ. `pending`/`outcome` mô tả lời gọi mutation, không phải bằng chứng fill.

Errors:

| HTTP | code | ý nghĩa |
|------|------|---------|
| 503 | `exchange_unreachable` | Không tạo được bridge khi cần live; lỗi Spot read đã khởi tạo bridge được báo trong response 200 |
| 503 | `audit_log_unreadable` | Audit không đọc được trong dry mode; live mode trả audit unknown để vẫn giữ Futures |

Trạng thái: loading · **empty** (`cash: null`, `positions: []`, `realized_pnl.total: 0` — chưa có dữ liệu venue/log; hiển thị empty-state riêng, không coi là lỗi) · error 503 (retry) · stale 30s + auto-poll.

Thiếu sót trung thực (ghi trên UI): position mở trước khi audit log bắt đầu có basis không xác định (`average_price: null`, `unrealized_pnl: null`) — replay chỉ thấy những gì log đã ghi; position không mark được làm `equity_est` thấp hơn và sinh warning; quantity không rõ làm `equity_est` không xác định (`null`) thay vì tính vị thế là 0.

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
| 2 | PUT   | `/api/settings`                  | set_config in-memory + upsert PostgreSQL `control_engine_settings`; cấm key gate |
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
| 19| GET   | `/api/performance`               | đọc execution audit: brackets, sự kiện execute gần đây, skipped_lines (§12) |
| 19a| GET  | `/api/performance/fills`          | đọc một trang/mã Binance USD-M Futures trong phạm vi audit-selected (§12) |
| 20| GET   | `/api/autoloop`                  | gates (thêm 2 nửa futures — chỉ quan sát) + state file `autoloop.json` (§13, §15) |
| 21| POST  | `/api/autoloop`                  | fail-closed 409 gate_closed (§13) |
| 22| POST  | `/api/autoloop/run-now`          | 1 chu kỳ screen→propagate→plan→`_handle_plan`; crypto qua `DerivativesExecutor` khi `exec_derivatives` bật — futures long/short, đòn bẩy ≤3× (§15) |
| 23| POST  | `/api/halt`                      | append `{"event": "halt"}` — RiskGuard mọi process chặn hôm nay |
| 24| POST  | `/api/halt/reset`                | append `{"event": "halt_reset"}` |
| 25| GET   | `/api/runs`                      | lịch sử run (database) + `active_run_id` (§3 rev 7) |

**endpointCount = 28.**

---

## 11. Rủi ro thiết kế còn mở

1. **Approval queue chưa tồn tại trong engine** — `daily.py:186-201` chỉ audit `awaiting_approval` rồi thoát; queue là state mới, persist thật tại `server/data/approvals.jsonl` (tmp+replace — `approvals.py:65-73`). Khi server restart giữa chừng, item `pending` khôi phục từ file; item đang ở `approved` (claimed, §4) khi sập **có thể đã gửi lệnh thật** — đối soát qua engine audit + venue, không tự hồi. Pipeline approve→execute đã implement từ rev 3.1 (`approvals.py:241-333`) — trước đó approve chỉ claim rồi dừng, để item ở dead-end `approved` vĩnh viễn (review finding, đã xoá).
2. **Job/run là state trong database, không phải in-memory** (rev 7 — đã sửa). `server/data/control.db` (SQLite, stdlib `sqlite3`, WAL) lưu `jobs`/`job_results`/`runs`/`run_events`. Restart server không mất lịch sử: `GET /api/daily/{id}` và `GET /api/runs/{id}` đọc lại từ DB, và stream của một run đã xong replay được từ `run_events`. Job/run bị process chết bỏ lại ở `running` được set `interrupted` lúc khởi động (`main.py:_open_state_db` → `daily.reconcile()` / `runs.reconcile()`, mỗi cái ghi một audit line) — vì worker lock suy ra từ status, không reconcile thì job mồ côi sẽ trả 409 cho mọi job sau, vĩnh viễn và vô hình. Engine audit (`exec_log_path`) **vẫn là JSONL**: đó là contract replay của RiskGuard xuyên process, không phải state của API layer.
3. **`exec_watchlist` không env-overridable** (`default_config.py:38-41`) — persistence phải là file riêng của API layer, không phải `.env`; mọi nơi đọc watchlist phải đi qua layer này hoặc sync vào `DEFAULT_CONFIG` lúc khởi động, nếu không CLI và UI sẽ trôi về default `["BTC-USD","ETH-USD"]` (đã làm ở `watchlist.py:39-44`).
4. **Operator gates không thuộc settings engine.** Các quyền `exec_live`, `exec_auto_confirm`, `exec_derivatives` chỉ ghi qua `/api/operator-config`, có confirm + reason và audit; không có env gate runtime thứ hai.
5. **`.env` không còn bị ghi từ API** (migration `0006`): settings PUT persist vào PostgreSQL `control_engine_settings`, nạp lại lúc boot giữa tầng env và tầng operator override; `.env` thuần cấu hình triển khai (bí mật, venue URL) và chỉ đổi kèm restart. Lỗi persistence giờ là lỗi PostgreSQL thông thường (500).
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
- §7–§11: renumber từ §6–§10; thêm mã lỗi 403 `forbidden_key`, 410 `expired`, 502 `execute_failed`, 503 `exchange_unreachable` vào §0.1; Base URL/origin §0 khớp code thật (`server/main.py:5,26-33`, `web/next.config.ts:7-9`).

---

## 12. Performance — `GET /api/performance`, `GET /api/performance/fills` (màn Tổng quan)

Performance tách ba nguồn: sự kiện audit từ `/api/performance`, vị thế Futures từ snapshot venue và vị thế Spot theo `positions_source` của `/api/portfolio` (§6), và khớp lệnh Futures do sàn báo cáo từ `/api/performance/fills`. Không dùng audit replay để xác nhận vị thế hiện tại, thống kê thắng/thua hay lịch sử P&L; audit có thể thiếu giao dịch, chứa ước tính hoặc giao dịch mô phỏng.

### GET /api/performance

Đọc `exec_log_path` qua `audit.read_events()`, không gọi venue (`server/performance.py:37-70`). Không có tham số bắt buộc. Response 200 ví dụ giả định:

```json
{
  "generated_at": "2026-11-02T12:00:00Z",
  "skipped_lines": 1,
  "summary": null,
  "open_positions": null,
  "brackets": { "placed": 0, "failed": 0 },
  "recent_orders": [
    {
      "timestamp": "2026-11-02T11:00:00Z",
      "ticker": "ASSET-USD",
      "ccxt_symbol": null,
      "action": "rejected_by_risk",
      "qty": null,
      "price_est": null,
      "market": "spot",
      "leverage": null,
      "cost": null,
      "mode": "dry",
      "order_id": null
    }
  ]
}
```

- `summary` và `open_positions` luôn là `null`: chưa đủ dữ liệu đối soát để xác nhận thống kê giao dịch đóng hoặc vị thế mở. Hiển thị chưa xác định, không thay bằng số 0 hay danh sách rỗng. Vị thế venue và chất lượng đọc nằm ở `/api/portfolio`, bao gồm `positions_source` và `futures.positions_read` (§6).
- `recent_orders` là tối đa 50 sự kiện audit có `phase="execute"` và `action` khác rỗng, theo thứ tự dòng mới nhất trước, không sắp xếp theo timestamp. Bao gồm cả dry/live, từ chối, thất bại và bracket; **không phải danh sách fill đã xác nhận**. `qty`, `price_est`, `leverage`, `cost` chỉ giữ số hữu hạn, các giá trị sai/thiếu là `null`; các trường text sai kiểu là `null`, riêng `market` mặc định `"spot"` khi thiếu, rỗng hoặc sai kiểu. `price_est` và `cost` không chứng minh giá khớp hay P&L.
- `brackets.placed`/`failed` đếm các sự kiện `bracket_placed`/`bracket_failed` trong toàn audit, không phân biệt phase/mode; đây là số lần ghi nhận trong lịch sử, không xác nhận bảo vệ hiện tại trên sàn.
- `skipped_lines` là số dòng JSON không hợp lệ hoặc không phải object bị bỏ qua; dòng trống không được tính (`server/audit.py:34-64`). Hiển thị cảnh báo nếu lớn hơn 0.

Log thiếu hoặc rỗng trả 200 với `summary`/`open_positions` null, `recent_orders: []`, số đếm bracket và `skipped_lines` bằng 0; điều này không chứng minh tài khoản không có giao dịch. Log tồn tại nhưng không đọc được trả `503 audit_log_unreadable`. UI xử lý loading, danh sách audit rỗng, lỗi/retry và dữ liệu cũ; Overview poll audit performance mỗi 30 giây (`web/components/overview/dashboard.tsx:45`).

### GET /api/performance/fills

Endpoint **read-only**, không tham số bắt buộc, chỉ đọc khớp lệnh Binance USD-M Futures từ venue; không đặt/hủy lệnh, đổi cấu hình hay ghi audit. Đây không phải lịch sử Spot hoặc toàn bộ tài khoản (`server/performance_history.py:276-391`, `server/futures_trade_history.py:267-416`).

Điều kiện và phạm vi:

- Gate effective live phải mở: `exec_live` là boolean `true` trong config. Khi quyền đóng trả 200 `status="unavailable"`, `source="unavailable"`, `fills=[]`, kèm warning và không gọi venue; đây không phải xác nhận không có fill. Khi `exec_live` là `true` nhưng cấu hình sàn khác Binance, endpoint trả `unsupported`.
- Chỉ chọn tối đa 12 `ccxt_symbol` Futures chính xác, duy nhất từ event `phase="execute"`, `mode="live"`, `market="derivatives"` và action `buy`, `sell`, `bracket_placed`, `bracket_failed` hoặc `time_stop_closed`; ưu tiên event audit có `venue_key` khớp venue hiện tại, mới nhất theo timestamp trong mỗi nhóm. Legacy event thiếu `venue_key` vẫn có thể gợi ý mã để hỏi sàn, nhưng luôn kèm warning và đứng sau mọi mã đã xác minh; không dùng chúng làm bằng chứng fill hay bot ownership. Event có venue key khác bị loại. `time_stop_closed` chỉ giúp chọn mã cần hỏi sàn; chính nó không được trình bày như fill. Chọn mã từ toàn audit, không chỉ sự kiện trong cửa sổ 7 ngày; timestamp không hợp lệ được xếp cuối và có warning. Không đoán mã từ ticker. Dòng hỏng, mã thiếu/sai hoặc vượt giới hạn cũng được cảnh báo. Không có mã đủ điều kiện trả `unavailable`, không gọi lịch sử sàn (`server/performance_history.py:97-137,211-255`).
- Cửa sổ UTC cố định 7 ngày `[start_at, end_at)`: tính cả đầu, không tính cuối; `end_at` là thời điểm bắt đầu xử lý request, chính xác đến mili giây. Chỉ giữ fill trong cửa sổ và các mã đã chọn. Reader chỉ đọc **một trang/mã**, tối đa 1000 fill; trang đầy có thể đã bỏ sót fill cũ hơn, kể cả cùng timestamp, và được đánh dấu partial. Deadline phối hợp 15 giây kiểm tra giữa các lời gọi, không bảo đảm ngắt I/O/retry đang chạy; timeout client 5 giây (`server/performance_history.py:349-352`, `server/futures_trade_history.py:219-285`).
- Cache response: sau khi qua effective live gate và chọn được mã, route tra cache trước khi kiểm tra rate-limit và trước khi gọi venue. Chỉ cache payload có `source="venue_fills"` và `status` `ok`/`partial`; cache giữ tối đa 240 giây, lưu **một payload duy nhất** trong process và xóa payload cũ khi ghi payload mới. Khóa cache gồm profile venue (exchange id + base URL), HMAC của cặp credential môi trường hiện tại và đúng thứ tự danh sách symbol đã chọn; nếu thiếu credential đầy đủ thì không cache. Khi cache hit, `generated_at`, `start_at`, `end_at` và `observed_at` giữ timestamp của lần đọc venue đã lưu; warning và phạm vi từ audit được cập nhật theo lần gọi hiện tại, không tạo lần đọc venue mới.
- Phạm vi chọn từ audit luôn chưa đầy đủ: không biết toàn bộ mã/giao dịch tài khoản, basis vị thế trước cửa sổ hoặc thời gian lưu trữ của sàn. Reader đọc thành công luôn gắn warning `normalized_history_unverified`, kể cả trang chuẩn hóa rỗng; endpoint cũng ép `status="partial"` với mọi kết quả venue `status="ok"`. Chỉ khi không có lần đọc chuẩn hóa thành công nào mới trả `unavailable`, không phải lịch sử đầy đủ với 0 giao dịch. `fills: []` khi partial/unavailable không chứng minh không có giao dịch khác.

Response 200 ví dụ giả định, mọi mã và số liệu đều là dữ liệu minh họa:

```json
{
  "generated_at": "2026-11-02T12:00:00.000Z",
  "status": "partial",
  "source": "venue_fills",
  "venue_profile": "demo",
  "start_at": "2026-10-26T12:00:00.000Z",
  "end_at": "2026-11-02T12:00:00.000Z",
  "observed_at": "2026-11-02T12:00:00.000Z",
  "coverage": "Chỉ các mã trong nhật ký được chọn; chưa xác minh đầy đủ tài khoản và lịch sử trước khoảng này. Các giao dịch là của tài khoản sàn đang chọn, có thể do người dùng hoặc hệ thống khác thực hiện. Không xác minh được giao dịch nào do bot thực hiện. Chưa xác định cơ sở vị thế ban đầu và thời gian lưu trữ của sàn. Mã đã chọn: ASSET/USDT:USDT.",
  "warnings": ["Lịch sử chuẩn hóa chưa được xác minh đầy đủ."],
  "fills": [{
    "trade_id": "101",
    "order_id": "201",
    "symbol": "ASSET/USDT:USDT",
    "side": "buy",
    "timestamp": "2026-11-01T12:00:00.000Z",
    "contracts": "0.12500000",
    "price": "100.00000001",
    "fee": "0.01000000",
    "fee_currency": "USDT",
    "realized_pnl": null,
    "realized_pnl_currency": null
  }]
}
```

- `status`: `partial` cho kết quả normalized từ venue; reader luôn cảnh báo `normalized_history_unverified`, kể cả khi không có fill. `unavailable` khi gate đóng, không có mã, reader/client/markets không khả dụng hoặc đang trong cooldown rate-limit; `unsupported` khi sàn/tài khoản không hỗ trợ; `error` khi profile/endpoint không khớp hoặc có lỗi xử lý. Các trạng thái `unavailable`, `unsupported`, `error` có `source="unavailable"` và `fills=[]`; giữ warning thay vì trình bày như số dư hay lịch sử bằng 0. Concurrent request khi history đang được đọc trả **HTTP 503 `history_busy`** theo error envelope §0.1, không response history 200. Khi rate limit, route đặt cooldown tối thiểu 900 giây; nếu venue cung cấp `Retry-After` dạng số giây/HTTP-date hoặc thời gian ban, cooldown dùng thời lượng dài hơn. Reader không có lần đọc chuẩn hóa thành công trả `unavailable`; lỗi theo từng mã khác khi còn ít nhất một lần đọc chuẩn hóa thành công có thể trả `partial`.
- `source="venue_fills"` xác định nguồn là reader lịch sử sàn, không tự chứng minh mọi mã đã đọc thành công. `venue_profile` là `"demo"`, `"production"` hoặc `null`; reader nhận diện profile từ cấu hình Binance và kiểm tra endpoint tương ứng trước khi lấy dữ liệu. `observed_at` là thời điểm reader hoàn tất phần đọc venue thành công; có thể `null` nếu chưa có lần đọc chuẩn hóa thành công. Khi cache hit, các timestamp giữ nguyên từ lần đọc đã lưu, còn warning audit được cập nhật. `coverage` và `warnings` mô tả giới hạn/lỗi và phải được hiển thị cùng dữ liệu.
- Mỗi fill có `trade_id`, `order_id` nullable, `symbol`, `side` (`buy`/`sell`), `timestamp`, `contracts`, `price`, `fee`, `fee_currency`, `realized_pnl`, `realized_pnl_currency`. `contracts` và `price` là chuỗi thập phân dương; `fee` và `realized_pnl` là chuỗi thập phân chính xác từ giá trị nguồn hoặc `null`, không ép về float để hiển thị. Ưu tiên chuỗi gốc của venue; nếu CCXT chỉ cung cấp số thì việc chuyển thành chuỗi không khôi phục độ chính xác đã mất. Currency có thể null; không tự suy ra đơn vị hoặc biến `null` thành 0 (`server/futures_trade_history.py:96-166`).
- `realized_pnl` chỉ là giá trị sàn báo cho từng fill khi có, không phải P&L tổng hay thống kê lịch sử hoàn chỉnh. Endpoint không trả funding, không tự tạo historical P&L/funding từ audit hoặc ước tính; không tính win rate hay expectancy từ phạm vi từng phần.

Audit log tồn tại nhưng không đọc được trả HTTP `503 audit_log_unreadable` với message đã làm sạch; các lỗi đọc lịch sử khác thường trả HTTP 200 với `status` và `warnings` ở trên. UI phân biệt loading, partial, unavailable, unsupported, error và stale; Overview poll lịch sử mỗi 5 phút (`web/components/overview/dashboard.tsx:46`), không xem dữ liệu cũ là quan sát mới.

---

## 13. Autoloop + halt — vòng lặp tự chạy & kill switch (rev 5)

Nguồn: `server/autoloop.py`. Scheduler là daemon thread re-read state file (`server/data/autoloop.json` — `paths.AUTOLOOP_PATH`, `server/paths.py:17`) mỗi wake, nên đổi interval/enabled có hiệu lực ở wake kế tiếp, không cần restart (`autoloop.py:152-172`). Một cycle = sàng universe (30 mã/asset → top 8, như daily auto scan) → pipeline LLM per candidate → **mọi plan resolve qua confirm policy của engine `_handle_plan`** (`tradingagents/execution/daily.py`) — plan không execute chỉ được audit; candidate stock chỉ advisory (không sinh plan). Cycle khi run/job active → skip + audit `autoloop_cycle_skipped` (`autoloop.py:198-218`).

- **Enable fail-closed**: `all_open` = `exec_auto_confirm is True`; `exec_live` và `exec_derivatives` chỉ hiển thị để quan sát chính sách thực thi, không phải điều kiện bật scheduler.
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
    "exec_live_config": false,
    "exec_derivatives_config": false,
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

## 14. Activity trên Overview (UI-only, read-only)

Card `Hoạt động hiện tại` không tạo endpoint hoặc thay đổi execution state. Nó ghép bốn snapshot hiện có:

- `GET /api/autoloop`: `running_cycle`, `last_cycle.telemetry.active_stage`, mã hiện tại, node/agent, elapsed, và kết quả gần nhất.
- `GET /api/daily` → `active_job_id` → `GET /api/daily/{job_id}`: trạng thái queued/running/completed/failed/cancelled/interrupted, mã hiện tại, số kết quả và screening summary.
- `GET /api/health` → `active_run_id` → `GET /api/runs/{id}`: ticker, trade date, run id và trạng thái compact; event feed đầy đủ vẫn thuộc Pipeline SSE.

Khi worker đang queued/running, hook poll mỗi 5 giây; khi idle poll mỗi 15 giây. Các request có sequence guard để response cũ không ghi đè snapshot mới. Hook dùng `Promise.allSettled`: lỗi một nguồn giữ snapshot trước đó và đánh dấu chính nguồn đó stale/error; các panel còn lại vẫn hiển thị dữ liệu của chúng. Card có trạng thái loading/error/idle/stale riêng và link tới Settings, Scanner hoặc Pipeline.

---

## 15. Đường futures trong autoloop (rev 6)

Ứng viên crypto của 1 chu kỳ autoloop đi qua `DerivativesExecutor` thay vì spot bridge khi config `exec_derivatives` là literal True — executor được dựng 1 lần/cycle (`autoloop.py:253-254`, seam test `_build_derivatives_executor` — `autoloop.py:338-344`); ứng viên stock vẫn chỉ advisory như §13.

**3 cổng — UI không giữ cổng nào:**

1. **Config** — `exec_derivatives is True`: chặn lúc cycle quyết định dùng executor (`autoloop.py:254`) và chặn lần nữa trong `plan_order`/`execute_order` của executor (`derivatives.py:125-131, 191`).
2. **Config-only execution permission:** `exec_derivatives is True` blocks/permits the Futures executor at cycle, plan and execute boundaries; if closed, the candidate is refused and the cycle completes with an audited reason.
3. **Per-order confirmation:** every Futures plan still requires `confirm=True` through the engine policy; live mode additionally requires the LIVE permission and the venue/risk checks.

**Đòn bẩy, margin mode và ngân sách rủi ro do agent quyết định.** Trader đề xuất `FuturesExecutionProposal` (margin mode, leverage, `risk_fraction`, `margin_fraction`, entry/SL/TP, rationale); Portfolio Manager chốt lại sau tranh luận risk và trả về `futures_execution_proposal` trong state cuối. Autoloop chỉ **kiểm tra** đề xuất: thiếu proposal → `agent_proposal_missing` (không vào lệnh), vượt trần → từ chối chứ không tự hạ xuống, vì một lệnh bị resize không còn là quyết định của agent. Trần hiện hành: `MAX_AUTO_LEVERAGE = 3.0`, `risk_derivatives_max_risk_fraction` (mặc định 0.02), `risk_derivatives_max_margin_fraction` (mặc định 0.5). `risk_max_derivatives_leverage` vẫn là gate có thẩm quyền của RiskGuard và có thể từ chối dưới trần hệ thống. Executor gọi `set_margin_mode` trước `set_leverage`, rồi mới gửi entry, nên margin mode không còn phụ thuộc preset của sàn.

**Nguồn vốn sizing là ví Futures, không phải spot.** Cycle đọc `read_futures_account()` và dựng `PortfolioContext` từ `equity` (fallback `available_balance`) của tài khoản derivatives; `ExchangeBridge.sync_portfolio()` là snapshot spot nên không dùng cho sizing futures. Chính dữ liệu đó (equity, available margin, vị thế mở, nến đóng 1h/4h, bracket ATR, các trần hard) được render thành `execution_context` gửi cho agent; digest của context nằm trong checkpoint signature nên resume không tái dùng quyết định của một snapshot khác.

**Position state + profit protection:** sau live derivatives entry, executor ưu tiên `filled`/`average` từ response; nếu thiếu một trường, đọc vị thế futures exact symbol/side rồi dùng `contracts`, entry và mark của venue. State lưu `initial_stop`, `current_target`, venue + `bracket_task_id` và thời điểm quan sát. Background monitor poll 30 giây chỉ khi policy và cả quyền Futures/LIVE trong config mở. Chính sách tính R từ entry thực tế tới initial stop; break-even/lock/trailing chỉ nâng stop theo hướng giảm rủi ro và target chỉ được đẩy xa hơn khi mark vượt target nhưng position vẫn mở.

Bracket replacement đặt conditional leg mới trước, persist order ID/trigger rồi mới cancel leg cũ; timeout được reconcile theo strict symbol/side/type/reduceOnly/quantity/trigger. Cancel chưa xác nhận giữ retired ID trong SQLite để retry. Monitor lease atomic theo position key ngăn nhiều process cùng mutate; nhiều entry state cùng một Binance net position trả `ambiguous_net_position` và không tự chia size. Khi venue báo flat, chỉ xóa state sau khi các leg hiện hành/retired được xác nhận canceled hoặc filled.

Policy không đảm bảo giá khớp/lợi nhuận trong gap, slippage, liquidation hay outage. Binance Futures mutation hiện là venue được hỗ trợ; venue khác fail-closed. Không tự đổi One-way/Hedge mode; `reduceOnly` rejection được ghi nhận và không đổi state policy.

**Venue Binance:** mặc định chọn Demo qua `TRADINGAGENTS_EXEC_BASE_URL=https://demo-api.binance.com`; ccxt tự route futures tới `demo-fapi.binance.com`. Chuyển production bằng cách thay URL thành `https://api.binance.com`, dùng `BINANCE_API_KEY`/`BINANCE_SECRET`, rồi restart backend. Bridge chỉ nhận đúng hai profile HTTPS allowlist này và chọn key pair tương ứng. Quyền LIVE/Futures trong Settings là quyền vận hành; Demo order vẫn là live execution trên tài khoản Demo, không phải dry-run.
