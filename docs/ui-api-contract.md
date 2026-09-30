# Web UI API Contract (`docs/ui-api-contract.md`)

updated: 2026-09-30 (rev 2: thêm POST /api/daily + GET /api/daily/{job_id}, bắt buộc CSRF guard) · status: design (chưa có implementation)
audience: frontend + backend implementer của control-panel UI cho TradingAgents

Hợp đồng này bọc engine hiện có **mà không sửa mã lõi `tradingagents/`**. Mọi mapping
sang mã thật được trích `file:line` ở thời điểm viết.

---

## 0. Conventions chung

- Base URL: `http://127.0.0.1:8420/api` (local control panel; server bind localhost mặc định).
- Content type: `application/json; charset=utf-8` (repo cố định UTF-8 — `.env.example`, `audit.py:4-6`).
- Timestamps: ISO-8601 UTC (`bridge.py:583` ghi audit bằng `datetime.now(timezone.utc).isoformat()`).
- Ngày: `YYYY-MM-DD`; engine từ chối ngày tương lai (`trading_graph.py:29-40`).
- Auth/CSRF: v1 không token (localhost only), nhưng **mọi phương thức đổi trạng thái (POST/PUT/DELETE) bắt buộc hai lớp chống CSRF** — một trang web lạ mở trong browser cùng máy có thể `fetch(..., {mode:'no-cors'})` vào localhost, và no-cors chỉ gửi được CORS-safelisted content types (`text/plain`, `application/x-www-form-urlencoded`, `multipart/form-data`), nên:
  1. `Content-Type: application/json` bắt buộc (body rỗng hợp lệ nhưng phải gửi `{}` với đúng content type); sai/thiếu → `415 unsupported_media_type`. Quy tắc áp cho mọi POST/PUT/DELETE trong tài liệu này.
  2. Kiểm tra Origin: `Origin` header tồn tại và không thuộc `{http://127.0.0.1:8420, http://localhost:8420}` → `403 csrf_rejected`; tương tự `Sec-Fetch-Site: cross-site` → `403`. GET và SSE không đổi trạng thái nên miễn trừ.
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
| 404  | `not_found`             | run/approval/backtest run/file không tồn tại |
| 409  | `conflict`              | run đang chạy, approval đã xử lý, duplicate watchlist |
| 412  | `gate_closed`           | hành động cần gate FR5 mở nhưng gate đóng |
| 415  | `unsupported_media_type`| POST/PUT/DELETE thiếu `Content-Type: application/json` (§0) |
| 422  | `coercion_error`        | giá trị không coerce được theo kiểu default (`default_config.py:60-80`) |
| 500  | `internal_error`        | lỗi không lường trước |
| 503  | `audit_log_unreadable`  | `exec_log_path` tồn tại nhưng không đọc được |

### 0.2 Trạng thái frontend bắt buộc xử lý

Mỗi endpoint dưới đây ghi rõ 4 trạng thái. Định nghĩa chung:

- **loading** — request đang bay: skeleton/spinner.
- **empty** — 200 với danh sách rỗng: empty-state riêng, không coi là lỗi.
- **error** — 4xx/5xx/503 hoặc network fail: hiển thị `error.message`, có nút retry.
- **stale** — response cũ hơn `stale_after` giây hoặc `generated_at` khác lần refresh gần nhất: badge "cũ", không tự ghi đè UI khi user đang tương tác.

---

## 1. Settings — `GET /api/settings`, `PUT /api/settings`

Nguồn: `_ENV_OVERRIDES` (`default_config.py:10-53`), defaults (`default_config.py:96-242`), `.env.example`.

Quy tắc bất di bất dịch:

1. **Không bao giờ trả nguyên văn API key/secret.** Mọi key có tên khớp `*_API_KEY` / `*_SECRET` chỉ trả `masked` (4 ký tự cuối) + `configured`. Audit của bridge cũng redact credential (`bridge.py:619-624`).
2. **Các env-gate FR5/FR-S2/FR-D là read-only**: `TRADINGAGENTS_EXEC_LIVE`, `TRADINGAGENTS_EXEC_AUTO_CONFIRM`, `TRADINGAGENTS_EXEC_DERIVATIVES` và config `exec_live`/`exec_auto_confirm`/`exec_derivatives` KHÔNG ghi được qua API. Chúng cố tình vắng khỏi `_ENV_OVERRIDES` (`default_config.py:33-36, 235-241`) và chỉ được đọc fail-closed lúc thực thi (`bridge.py:527-538`, `daily.py:67-76`). PUT nhận key này → `403 forbidden_key`.
3. `exec_watchlist` và `exec_symbol_overrides` không thuộc settings (không coerce được list/dict — `default_config.py:38-41`); watchlist có endpoint riêng (mục 2).

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
      "key": "exec_sandbox",
      "env_var": "TRADINGAGENTS_EXEC_SANDBOX",
      "value": false,
      "type": "bool",
      "editable": true,
      "source": "env"
    }
  ],
  "credentials": [
    { "env_var": "OPENAI_API_KEY",   "configured": true,  "masked": "••••abcd" },
    { "env_var": "BINANCE_API_KEY",  "configured": false, "masked": null },
    { "env_var": "BINANCE_SECRET",   "configured": false, "masked": null }
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

- `settings[]` phủ đúng 24 dòng `_ENV_OVERRIDES` (`default_config.py:10-53`): llm_provider, deep_think_llm, quick_think_llm, backend_url, llm_wire_protocol, output_language, max_debate_rounds, max_risk_discuss_rounds, checkpoint_enabled, benchmark_ticker, temperature, llm_max_retries, max_tokens, google_thinking_level, openai_reasoning_effort, anthropic_effort, exec_quote_currency, risk_max_daily_loss_pct, risk_max_position_pct_per_asset, risk_max_total_exposure_pct, risk_max_consecutive_loss_count, risk_max_derivatives_leverage, risk_max_derivatives_exposure_pct, exec_exchange_id, exec_sandbox.
- `type` suy từ kiểu của default trong `DEFAULT_CONFIG` (bool/int/float/str/null — `default_config.py:60-80`).
- `source`: `default` | `env` (env var có giá trị non-empty) | `.env`.
- `effective_exec_mode` = `bridge.effective_mode()` (`bridge.py:480-486`): `live` chỉ khi `exec_live is True` **và** env flag true — nếu UI thấy `live`, cả hai nửa gate đang mở.
- Paths read-only (`results_dir`, `data_cache_dir`, `memory_log_path`, `exec_log_path`) trả trong `settings[]` với `editable: false`.

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

1. Validate từng key: phải thuộc 24 keys `_ENV_OVERRIDES`; coerce theo kiểu default như `_coerce` (`default_config.py:60-80`) — sai kiểu → `422 coercion_error` kèm `details: {"env_var": "...", "message": "expected a boolean (true/1/yes/on/false/0/no/off), got 'treu'"}`.
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

Ràng buộc triển khai (quan trọng):

- **Runs chỉ phân tích, không đụng execution.** `POST /api/runs` chỉ stream graph như CLI; `cli/run.py` không import `ExchangeBridge`, và điểm gọi production duy nhất của `plan_order` là daily runner (`daily.py:167`). Run phân tích không bao giờ sinh plan → không bao giờ đẩy hàng đợi duyệt; nguồn item của queue là `POST /api/daily` (§5).
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

### GET /api/runs/{id}

Response 200:

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

- Mỗi event có `seq` tăng dần; client reconnect gửi header `Last-Event-ID: <seq>` → server replay từ seq+1 (buffer trong bộ nhớ của run).
- Terminal event (`run_completed`/`run_failed`) rồi server đóng stream.
- Run ở trạng thái completed khi client mở stream → server phát lại toàn bộ buffer rồi đóng (UI refresh trang vẫn xem lại được).
- `replay: true` trên mọi event của run mock.

Trạng thái: loading (trước event đầu) · empty (không xảy ra — luôn có `run_started`) · error (network đứt → tự reconnect bằng `Last-Event-ID`; run failed → event `run_failed`) · stale (không nhận event > 60s trong khi status vẫn `running` → badge "đang treo", gợi ý cancel).

---

## 4. Approvals — hàng đợi duyệt lệnh

Nguồn: hiện engine **không có** hàng đợi duyệt — daily runner chỉ audit `awaiting_approval` rồi trả về (`daily.py:186-201, 219-222`). Queue là state mới của API layer (bộ nhớ + persist `~/.tradingagents/webui/approvals.json`), nhưng **đặt lệnh thật vẫn chỉ đi qua `ExchangeBridge.execute_order`** (`bridge.py:265-328`) — RiskGuard re-check fail-closed chạy bên trong (`bridge.py:311-325`), không có đường nào gửi lệnh ngoài nó.

Invariants (bắt buộc, verify khi code review):

1. **Không tồn tại endpoint đặt lệnh.** Approve là đường duy nhất đưa plan vào `execute_order(confirm=True)`; reject không đụng exchange.
2. **Approve không tự arm live.** Mode thật do double gate quyết định lúc execute (`bridge.py:472-478`): `mode:"live", executed:true` chỉ khi `exec_live` config literal True **và** `TRADINGAGENTS_EXEC_LIVE` env true. Gate đóng → dry-fill mô phỏng (`bridge.py:358-369`), không lệnh thật. Nếu caller muốn chặn cứng việc dry-fill khi gate đóng thì trả `412 gate_closed` — server cấu hình theo policy `dry_fill_on_gate_closed` (default: cho phép dry-fill).
3. **Mọi hành động ghi audit.** Approve/reject viết qua `bridge.audit_plan` (`bridge.py:488-512`) với action `approval_granted` / `approval_denied`; execute path đã tự audit mọi nhánh kể cả lỗi (`bridge.py:20-21, 449-451`).
4. RiskGuard halted hôm nay → mọi approve trả `403 risk_halted` kèm reason (`risk.py:472-477, 587-589`).
5. **Queue chỉ được nuôi bởi `POST /api/daily` (§5).** Điểm gọi production duy nhất của `plan_order` là daily runner (`daily.py:167`; `DerivativesExecutor` ở `derivatives.py:71` nằm sau ba lớp gate FR-D và không được kích hoạt từ bất kỳ endpoint v1 nào). `POST /api/runs` chỉ phân tích (§3) — không có đường tạo plan nào khác, để không ai tự bịa ra một endpoint đặt lệnh ngầm, đúng thứ invariant 1 cấm.

### GET /api/approvals

Response 200:

```json
{
  "generated_at": "2026-09-30T08:00:00Z",
  "items": [
    {
      "id": "ap_0182",
      "created_at": "2026-09-30T08:15:40Z",
      "run_id": "r_20260930_081512_btcusd",
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
      "status": "pending",
      "resolved_at": null,
      "resolution": null
    }
  ],
  "pending_count": 1
}
```

- `status`: `pending | approved | rejected | executed | execute_failed | expired`.
- Nguồn item: **chỉ** từ job `POST /api/daily` (§5). Khi `run_daily` xử lý một coin có signal tradeable, `_handle_plan` audit `awaiting_approval` ngay tại thời điểm coin đó xong (`daily.py:186-201, 219-222`); khi job chuyển `completed`, API layer đọc `results[]` và đẩy vào queue mỗi `DailyResult.plan` có `side != null` và `reason` dạng `"awaiting approval"` (`daily.py:189, 201, 213`) — kế hoạch đã tự execute (auto-confirm armed) không enqueue.
- Query: `?status=pending&limit=50&offset=0`.

Trạng thái: loading · **empty** (`items: []` — khi chưa có plan nào) · error 500 · stale 15s (đây là màn approval gate — phải auto-poll; item biến mất khi đã resolve bởi nơi khác).

### POST /api/approvals/{id}/approve

Request bắt buộc `Content-Type: application/json` (quy tắc CSRF §0 — đây là endpoint nguy hiểm nhất của hệ thống, không có ngoại lệ); body tối thiểu `{}`:

```json
{ "portfolio": null }
```

`portfolio: null` → server tự `bridge.sync_portfolio()` (`bridge.py:330-356`, cần exchange đọc được; nếu không đọc được → vẫn approve nhưng percent-limits fail-open như `risk.py:512-520`, response ghi rõ).

Xử lý server:

1. Item phải `pending`, chưa quá hạn (mặc định hết hạn sau 24h, cấu hình được) → 409 `conflict` / 410 cho hết hạn.
2. Ghi audit `approval_granted` (audit_plan).
3. Chuyển `PlannedOrder` (lưu cùng item) → `execute_order(order, confirm=True, portfolio)` (`bridge.py:265-328`). Kết quả ánh xạ:
   - thành công dry → `status: executed`, `executed: false`, `mode: "dry"`, `order_id: null` (dry-fill mô phỏng — `bridge.py:358-369`);
   - thành công live → `executed: true`, `mode: "live"`, `order_id` từ exchange;
   - `PermissionError` từ RiskGuard → `status: execute_failed`, audit `rejected_by_risk` (`bridge.py:317-325`), HTTP 403 `risk_halted`;
   - `PreTradeRejection` → `execute_failed`, reason từ audit (`bridge.py:411-441`);
   - lỗi "order WAS placed but audit write failed" (`bridge.py:464-469`) → `status: executed` + `warning` red-flag — **không được retry**, order id có thật.

Response 200:

```json
{
  "id": "ap_0182",
  "status": "executed",
  "executed": false,
  "mode": "dry",
  "order_id": null,
  "risk": { "allowed": true, "halted": false, "reason": null },
  "audit_action": "buy"
}
```

### POST /api/approvals/{id}/approve — lỗi

| HTTP | code | ý nghĩa |
|------|------|---------|
| 403 | `risk_halted` | guard halt (kèm `details.reason`) |
| 409 | `conflict` | item không còn `pending` (đã approve/reject bởi tab khác) |
| 410 | `expired` | quá hạn duyệt |
| 412 | `gate_closed` | policy cấm dry-fill khi gate đóng |
| 503 | `audit_log_unreadable` | execute-time re-check fail-closed (`bridge.py:311-315`, `risk.py:122-147`) |

### POST /api/approvals/{id}/reject

Request bắt buộc `Content-Type: application/json` (quy tắc CSRF §0), body tối thiểu `{}`:

```json
{ "reason": "không duyệt mức giá này" }
```

Xử lý: item phải `pending`; ghi audit `approval_denied` kèm reason (đi qua `_redact` — `bridge.py:619-624`); **không** gọi exchange; `status: rejected`.

Response 200: item sau cập nhật. Errors: 409/410/503 như trên.

Trạng thái POSTs: loading (execute live có thể mất vài giây — disable nút, đợi) · error · stale (n/a — action; sau action poll lại GET).

---

## 5. Daily pipeline — `POST /api/daily`, `GET /api/daily/{job_id}`

Nguồn: `run_daily(watchlist, config)` (`daily.py:114-174`) — bề mặt lập lịch duy nhất của engine cho chuỗi *phân tích → sync portfolio → plan qua RiskGuard → confirm policy*. Đây là **nút bấm "Chạy pipeline hằng ngày ngay"** trên UI và là nguồn duy nhất của hàng đợi duyệt (§4, invariant 5).

### POST /api/daily

Request bắt buộc `Content-Type: application/json` (quy tắc CSRF §0); body tối thiểu `{}`:

```json
{ "tickers": ["BTC-USD", "SOL-USD"] }
```

- `tickers` bỏ qua/null → dùng `exec_watchlist` hiện hành (`daily.py:127-128`); normalize tại biên giới runner (strip, dedupe giữ thứ tự — `daily.py:95-111`).
- API layer gọi `run_daily` với config của process, **không arm auto-confirm qua API** (gates là read-only — §1). Nếu operator đã arm cả hai nửa `exec_auto_confirm` ở môi trường, `run_daily` tự thực thi theo gate của nó (dry-fill hoặc live — `daily.py:203-216`) và `results[]` trả `executed: true`; API không chặn và không nhân đôi quyết định này — lúc đó không có item nào vào queue.
- Chạy trong cùng worker thread duy nhất với `POST /api/runs` (lock `set_config` — §3); worker bận → `409 conflict`.
- Mỗi coin chạy full LLM pipeline (`daily.py:147-164`) — job có thể chạy nhiều phút với watchlist dài; không có SSE trong v1, frontend poll (dưới).

Response 202:

```json
{
  "job_id": "d_20260930_090001",
  "status": "queued",
  "tickers": ["BTC-USD", "SOL-USD"],
  "created_at": "2026-09-30T09:00:01Z",
  "detail_url": "/api/daily/d_20260930_090001"
}
```

Errors: 400 `validation_error` (ticker rỗng sau strip), 409 `conflict` (worker bận), 415/403 CSRF (§0).

### GET /api/daily/{job_id}

Response 200:

```json
{
  "job_id": "d_20260930_090001",
  "status": "completed",
  "created_at": "2026-09-30T09:00:01Z",
  "finished_at": "2026-09-30T09:11:24Z",
  "results": [
    {
      "ticker": "BTC-USD",
      "signal": "Buy",
      "executed": false,
      "order_id": null,
      "reason": "awaiting approval",
      "plan_summary": { "side": "buy", "quantity": 0.0012, "price_est": 83078.0, "cost": 99.69 },
      "approval_id": "ap_0183"
    },
    {
      "ticker": "SOL-USD",
      "signal": "Hold",
      "executed": false,
      "order_id": null,
      "reason": "Hold, REVIEW, sizing refusal or risk rejection",
      "plan_summary": null,
      "approval_id": null
    }
  ]
}
```

- `results[]` là shape `DailyResult` (`daily.py:79-88`: `ticker, signal, plan, executed, order_id, reason`) với `plan` tóm gọn thành `plan_summary` (field đầy đủ của `PlannedOrder` có thể thêm khi UI cần). Coin fail giữa pipeline được ghi `reason` dạng `"<ExceptionType>: <message>"` và vòng lặp chạy tiếp (`daily.py:170-173`).
- `approval_id` tham chiếu item trong `GET /api/approvals` — chỉ có với plan đã enqueue; null khi plan không tradeable hoặc đã tự execute.
- `status`: `queued | running | completed | failed`. Poll mỗi 2–5s khi `queued/running`; ngừng poll khi terminal.

Errors: 404. Trạng thái: loading · empty (n/a — job luôn có ≥1 coin) · error 404/500 · stale (poll là cơ chế chính; job không chuyển trạng thái > 15 phút → badge "đang treo").

## 6. Audit — `GET /api/audit`

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
  "last_halt": null,
  "last_halt_reset": null
}
```

- Đọc toàn file rồi lọc trong bộ nhớ (file append-only, cân nhắc index khi > vài MB — risk §10.7).
- `halted_today` tính như `_halted_today` (`risk.py:163-173`): UI approval gate hiển thị banner đỏ khi true; chỉ server mới được gọi `RiskGuard.reset_halt` (`risk.py:591-596`) — v1 **không** có endpoint reset (tránh nút kill-switch-reset trên UI); ghi vào risk §10.8.
- Dòng JSON hỏng bị bỏ qua và đếm vào `details.skipped_lines` (`risk.py:148-157` cũng làm vậy).

Errors: **503 `audit_log_unreadable`** khi file tồn tại nhưng không đọc được (không fail-open hiển thị — execute-time đã fail-closed ở `risk.py:122-147`, UI phải nhất quán).

Trạng thái: loading · **empty** (file chưa tồn tại = log mới — `risk.py:140-141`) · error 503 · stale 10s (audit là dữ liệu phán xét — hiển thị `generated_at` rõ).

---

## 7. History & Backtest

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
- `decision_excerpt` giới hạn ~500 ký tự; UI mở chi tiết qua `run_artifacts.state_log` (endpoint đọc file markdown/json chi tiết là phần mở rộng tương lai — ngoài scope 17 endpoint này, xem §10).

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

## 8. Health — `GET /api/health`

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

## 9. Bảng tổng hợp endpoint

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
| 9 | POST  | `/api/daily`                     | gọi `run_daily` — nguồn duy nhất của plan/queue |
| 10| GET   | `/api/daily/{job_id}`            | poll `DailyResult[]` (`daily.py:79-88`) |
| 11| GET   | `/api/approvals`                 | queue mới của API layer |
| 12| POST  | `/api/approvals/{id}/approve`    | `execute_order(confirm=True)` — duyệt đường chính thống |
| 13| POST  | `/api/approvals/{id}/reject`     | audit-only, không đụng exchange |
| 14| GET   | `/api/audit`                     | đọc `exec_log_path` phân trang |
| 15| GET   | `/api/history`                   | decision log + run tree |
| 16| GET   | `/api/backtest`                  | `results_dir/backtest/*` + summarize |
| 17| GET   | `/api/health`                    | mode/halt/key status |

**endpointCount = 17.**

---

## 10. Rủi ro thiết kế còn mở

1. **Approval queue chưa tồn tại trong engine** — `daily.py:186-201` chỉ audit `awaiting_approval` rồi thoát; queue là state mới (bộ nhớ + `~/.tradingagents/webui/approvals.json`). Khi server restart giữa chừng, pending plan phải khôi phục từ file; plan đã audit nhưng chưa vào queue trước khi sập có thể bị lạc — đối soát được qua audit line nhưng không tự hồi.
2. **`exec_watchlist` không env-overridable** (`default_config.py:38-41`) — persistence phải là file riêng của API layer, không phải `.env`; mọi nơi đọc watchlist phải đi qua layer này hoặc sync vào `DEFAULT_CONFIG` lúc khởi động, nếu không CLI và UI sẽ trôi về default `["BTC-USD","ETH-USD"]`.
3. **Env-gates phải read-only trong API** — nếu PUT settings cho phép ghi `TRADINGAGENTS_EXEC_LIVE`/`_AUTO_CONFIRM`/`_DERIVATIVES` thì double-gate FR5 (`bridge.py:33-36, 527-538`) sụp thành một công tắc. Contract cấm; cần test chặn.
4. **`.env` ghi từ API** có xung đột với format/comment hiện có và với việc user sửa tay cùng lúc; coercion phải chạy trước khi ghi để không sinh `.env` làm process chết lúc startup (`_apply_env_overrides` raise — `default_config.py:83-93`). Cân nhắc lock/ghi-đổi-tên.
5. **`set_config` process-wide** (`dataflows/config.py`, gọi tại `trading_graph.py:65`) — v1 serialize 1 run; muốn song song phải tách process (subprocess per run) — quyết định để sau v1.
6. **Mock replay phụ thuộc artifact cũ**: `full_states_log_*.json` chỉ tồn tại sau run hoàn tất (`trading_graph.py:346`); repo không có `results/` (dữ liệu thật ở `~/.tradingagents/logs`) — synthetic fallback là bắt buộc, không phải tùy chọn.
7. **`/api/audit` scan toàn file**: file append-only không giới hạn; khi lớn cần index/mmap — chưa thiết kế. Dòng JSON hỏng bị bỏ qua im lặng (như `risk.py:156-157`) — UI cần thấy `skipped_lines`.
8. **Không có endpoint reset halt trong v1** — `RiskGuard.reset_halt` (`risk.py:591-596`) là hành động operator nhạy cảm; để ngoài UI tránh nút one-click mở lại trading trong ngày bị halt.
9. **Web framework là dependency mới**: pyproject hiện không có fastapi/uvicorn — thêm dependency cần người dùng duyệt (AGENTS.md: không thêm deps khi không cần; ở đây task yêu cầu web API nên cần, nhưng phải ghi rõ).
10. **Approve → execute đồng bộ**: live order có thể chậm (network); UI phải disable + timeout riêng; idempotency dựa trên trạng thái approval (409 khi đã resolve) — không được retry `execute_order` mù quáng vì live order trùng là mất tiền thật (`bridge.py:20-21` ghi rõ rủi ro duplicate).
11. **`POST /api/daily` chiếm worker lock lâu** — mỗi coin là một full LLM pipeline (`daily.py:147-164`), watchlist 5 coin có thể khoá worker 10–30 phút, chặn luôn `POST /api/runs`; v1 không có cancel endpoint, job hỏng giữa chừng chỉ kết thúc tự nhiên khi vòng lặp coin xong (`daily.py:135-174` không có cơ chế huỷ).
12. **CSRF guard ≠ auth**: Content-Type + Origin/`Sec-Fetch-Site` chặn trang web lạ trong browser, nhưng không chống được process local hoặc malware trên cùng máy — mọi endpoint vẫn tin tưởng localhost; token auth chỉ bắt buộc khi mở bind ngoài `127.0.0.1` (§0).
13. **Enqueue approval xảy ra khi job `completed`**: `run_daily` trả toàn bộ `results[]` một lần cuối (`daily.py:135-174`), nên item xuất hiện trong queue sau khi toàn bộ watchlist xong, trễ hơn audit line `awaiting_approval` của từng coin (`daily.py:219-222`) — UI đối soát queue-trong-khi-chạy qua `/api/audit`; muốn enqueue tiến trình từng coin phải đổi `run_daily` thành callback/streaming — sửa mã lõi, ngoài scope.
