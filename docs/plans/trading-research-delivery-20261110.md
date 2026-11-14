# Kế hoạch triển khai: Trading Research Delivery — 2026-11-10

- **Người soạn:** agent kiến trúc sư hệ thống giao dịch (phiên khảo sát 2026-11-10)
- **Mốc code:** nhánh `feat/trading-research-delivery-20261110` (tách từ `main` @ `0943103`)
- **Căn cứ chính:** [docs/research/2026-11-10-carm1nee-ai-trading-stack-report.vi.md](../research/2026-11-10-carm1nee-ai-trading-stack-report.vi.md) (báo cáo đã được người dùng duyệt; giữ nguyên, không sửa trong kế hoạch này)
- **Phạm vi:** khảo sát + thiết kế + plan + runner kiểm chứng. **Chưa implement feature code trong ask này.** Không commit/push/PR/deploy, không đổi Auto/venue/risk limits trên runtime, không gửi lệnh sàn.

## 0. Mục tiêu và thứ tự đã duyệt

Người dùng duyệt triển khai tuần tự 4 hạng mục:

1. **Ledger kết quả ròng + backtest futures** dùng cùng risk/sizing/exits đang chạy.
2. **So sánh phiên bản** trên dữ liệu ngoài mẫu (out-of-sample).
3. **Bản tin vận hành trên dashboard** và redesign UX/UI dashboard.
4. **Kronos + bộ lọc đơn giản** chạy quan sát (shadow), không tác động quyết định.

Runner kiểm chứng `scripts/verify_research_delivery.py` ánh xạ 4 stage: `--stage ledger | experiments | dashboard | shadow` và `--full` (mục 10).

## 1. Hiện trạng đã khảo sát (bằng chứng trong phiên này)

Tất cả đường dẫn dưới đây đã được đọc trực tiếp trong phiên 2026-11-10, mốc `0943103`. Không dùng lại kết luận của phiên khác nếu chưa đối chiếu lại.

### 1.1. Nền tảng cycle → order đã có sẵn

| Thành phần | Vị trí | Nội dung dùng được |
| --- | --- | --- |
| Bảng cycle | `server/migrations/0002_control_plane.sql:120-135` | `futures_cycles(cycle_id PK, symbol, four_hour_open, one_hour_open, strategy_version, status, phase, payload JSONB)`, UNIQUE `(symbol, four_hour_open, one_hour_open, strategy_version)` — idempotency theo cặp nến có sẵn |
| Ghi terminal cycle | `server/autoloop.py:2243-2284` | payload terminal đã mang `order_id`, `execution_status` (vocabulary `executed/awaiting_approval/risk_rejected/not_executed`), `protection_state`, `risk_guard_decisions`, `phase_timestamps`; ghi qua `job_store.update_futures_cycle(..., terminal=True)` |
| Cycle identity | `server/futures_cycle_telemetry.py:102-109` | `cycle_identity()` → (symbol, 4h_open, 1h_open); `claim_cycle/persist_progress/persist_rejection` tại `:231/:250/:273` |
| Store cycle | `server/job_store.py:254-427` | claim `FOR UPDATE`, khóa terminal, `reconcile_futures_cycles()` khi boot (`server/main.py:138`) |
| Bảng order/fill chuẩn hoá | `server/migrations/0001_core_ledger.sql:187-231, 233-277` | `execution_orders` (client_order_key SHA-256, partial fill statuses, unique venue_order_id), `order_fills` (venue_trade_id UNIQUE theo account, `fee/fee_currency`), `execution_events` (idempotency_key UNIQUE theo account) — schema đã có, **chưa có writer runtime** |
| Journal entry + provenance | `tradingagents/execution/futures_entry_journal.py:15-115` | marker `managed_live_entry` (intent_id, client_order_id, identity hash), `entry_journal_leg` ch stop/target/scale_out_policy — dùng để phân biệt lệnh bot với lệnh khác |

### 1.2. Dữ liệu kết quả venue

- `server/futures_trade_history.py:33-57` — `FuturesTradeFill` đã có `trade_id, order_id, fee, fee_currency, realized_pnl, realized_pnl_currency`; `FuturesTradeHistory` có `status ok/partial/error/unsupported/unavailable`, `coverage`, `warnings`; đọc có giới hạn (`_MAX_SYMBOLS=20`, `_MAX_PAGES=20`, `:24-25`).
- `server/position_cost.py:21-28, 51-63` — funding chưa biết giữ `None` (không hiểu là 0); round-trip fee tính từ notional và "must not be doubled" với profit_protection.
- `server/performance.py:57-64` — `compute_performance` trả `summary: None` (không bịa thống kê).
- `server/performance_history.py:165-215` — gate `exec_live`, status `unavailable/partial/error`, warning vocabulary.

### 1.3. Logic thuần dùng lại cho mô phỏng

| Hàm | Vị trí | Đặc tính |
| --- | --- | --- |
| `RiskGuard.check` | `tradingagents/execution/risk.py:430-453, 617` | chặn lệnh theo FR-K limits, thuần đối với input |
| `sizing.compute` / `PlannedOrder` | `tradingagents/execution/sizing.py:39-101` | luôn trả plan, không raise nghiệp vụ |
| `evaluate_profit_protection` | `tradingagents/execution/profit_protection.py:18-134` | thuần; funding/fee vào R qua `cumulative_funding_r/round_trip_fee_r`; stop/target đơn điệu |
| `plan_scale_out` | `tradingagents/execution/scale_out.py:13-79` | thuần |
| Time review | `server/position_review.py` + `tradingagents/execution/futures_funding.py:63` | chính sách xem xét vị thế, funding reader riêng |
| Replay điều kiện | `server/futures_replay.py:27, 66-120, 182-198` | deterministic, chỉ nến đóng, fixture xuất từ PG, không mạng |

- Backtest hiện tại `tradingagents/backtest.py:35-143` là **decision-level grid** (hit rate/alpha), không mô phỏng danh mục — đúng như báo cáo mục 5B đã ghi.
- Settlement hiện tại `tradingagents/memory/settlement.py:60-120` settle return so benchmark, chưa đọc PnL lệnh.

### 1.4. Hạ tầng test PostgreSQL

- `tests/conftest.py:19-31` — blank overlay giữ lại đúng `TRADINGAGENTS_TEST_DATABASE_URL`; `:182-229` — fixture `postgres_schema`: mỗi test một schema `test_<hex>` ngẫu nhiên, migrate, drop CASCADE; `:200-208` — bắt buộc DSN test chỉ vào DB tên `test`, `test_*`, `*_test`, `*_tests`.
- `tests/test_postgres_migrations_integration.py:20-80` — compose project riêng `ta-migration-test-{pid}` từ `tests/postgres-compose.yml`, `up -d --wait` → `port` → finally `down --volumes`; chỉ chạy khi Docker sẵn sàng.
- `tests/postgres-compose.yml:1-14` — postgres:16-alpine, DB `migration_test` (đuôi `_test` thỏa rule conftest).
- `server/postgres.py` — migration runner: ledger `schema_migrations` + checksum + advisory lock; được gọi lúc boot tại `server/main.py:133-135` (`postgres.migrate()`).
- `pyproject.toml:72-82` — `addopts = "-ra --strict-markers -m \"not integration\""`; markers `unit/integration/smoke`.

### 1.5. Dashboard web

- `web/app/page.tsx:8-14` — trang Overview render `OverviewDashboard`; các component tách sẵn trong `web/components/overview/` (kpi-cards, portfolio-card, performance-*, order-table, activity-card).
- `web/components/overview/dashboard.tsx:41-58` — poll interval theo endpoint (audit 10s, health 15s, portfolio 30s, fills 5 phút).
- `web/next.config.ts:6-24` — đã đọc env (`API_INTERNAL_URL`) và rewrite `/api/:path*` → FastAPI: **cơ chế sẵn có cho offline fixture test** (trỏ API_INTERNAL_URL tới fixture server cục bộ).
- `web/package.json` scripts: `dev/build/start/lint` — **chưa có script typecheck**.
- Đã chạy `node node_modules/next/dist/bin/next build --help` trong phiên này: **Next 16 không còn cờ `--dist-dir`** — muốn build sang thư mục khác `.next` phải thêm `distDir` đọc env vào `web/next.config.ts` (mẫuenv đã có sẵn ở `API_INTERNAL_URL`).

### 1.6. Môi trường đã kiểm tra trong phiên này

| Kiểm tra | Lệnh | Kết quả |
| --- | --- | --- |
| Docker Engine | `docker info --format "{{.ServerVersion}}"` | **29.9.2** |
| Venv Python | `C:\Users\mrc\Documents\projects\TradingAgents\.venv\Scripts\python.exe --version` | **Python 3.13.14** |
| `websockets` khả dụng | `.venv\Scripts\python.exe -m pip show websockets` | 17.4, transitive (langgraph-sdk, yfinance, langsmith, google-genai) — dùng được cho CDP browser check **không thêm dependency** |
| Playwright | importlib find_spec | **không có** — browser check phải đi qua CDP, không qua Playwright |
| Next build flags | `node next/dist/bin/next build --help` | không có `--dist-dir` (thay bằng `distDir` config) |
| Branch | `git checkout -b feat/trading-research-delivery-20261110` | tách từ `main` @ `0943103`; `docs/research/` untracked được giữ |

### 1.7. Khoảng trống so với báo cáo research (xác nhận lại)

- Chưa có sổ kết quả ròng gắn cycle→order→fill→exit (báo cáo §5A).
- Chưa có mô phỏng portfolio/futures có chi phí (báo cáo §5B); `futures_replay` chỉ so điều kiện xác nhận.
- Chưa có walk-forward/holdout có purge (báo cáo §5C); `run_settings`/checkpoint mới có provenance một phần (báo cáo §5G).
- Chưa có bản tin vận hành (báo cáo §5F) — dashboard có KPI nhưng không có brief tổng hợp read-only.
- Chưa có Kronos hay bộ lọc baseline dạng quan sát (báo cáo §5D).
- `tradingagents/memory/log.py:32, 47` — dedup decision memory theo ticker+date (báo cáo đã ghi; thí nghiệm memory phải tính đến điều này khi đo memory-on/off).

## 2. Giai đoạn 1 — Outcome ledger (Hạng mục 1a, stage `ledger`)

### 2.1. Mô hình attribution cycle → order → fill → exit

Chuỗi attribution dùng **join key bền tồn tại thật** (các bảng `execution_orders` của 0001 chưa có writer runtime — không dùng `approval_id` làm nguồn):

```
futures_cycles.cycle_id (đã có, PG)
   └── cycle payload["order_id"] (autoloop.py:2269; sống sót qua redaction —
        futures_cycle_telemetry.py:15-20 không khớp order_id/client_order_id)
         └── entry journal legs (bracket_recovery_tasks: client_order_id,
              placed_ids, entry_provenance — futures_entry_journal.py:15-56, 86-115)
               └── FuturesTradeFill.order_id / trade_id (futures_trade_history.py:33-44,
                    venue_trade_id unique theo account)
                     └── outcome_exits (per leg: bracket/stop/target/scale-out/time-stop)
                           └── outcome_positions (tổng hợp ròng 1 position lifecycle)
```

Join pin: `cycle payload order_id ↔ journal leg placed_ids/client_order_id ↔ fill.order_id ↔ fill.trade_id`. `venue_order_id` được ghi khi venue trả về, nhưng không bắt buộc cho attribution (partial/retry có thể đổi venue id; client_order_id là key ổn định).

Nguyên tắc:

1. **Chỉ ghi nhận, không suy đoán.** Một fill chỉ thuộc bot khi có bằng chứng attribution: `entry_provenance` (marker `managed_live_entry`, `futures_entry_journal.py:40-56`) khớp với một leg trong entry journal VÀ leg đó nối được về `cycle_id` qua `order_id` trong cycle payload. Fill không tìm được attribution → chỉ nằm trong `coverage` với nhãn `unattributed`, **không bao giờ gán về bot** (không hồi tố gãn bừa).
2. **Không gán hồi tố:** migration mới chỉ tạo bảng rỗng; ingestion chỉ đọc từ thời điểm bật. Dữ liệu cũ trước khi bật ledger được ghi trong `outcome_coverage.snapshot` là "chưa phủ", không được gán cycle khớp theo thời gian/giá (matching heuristic = gán bừa).
3. **Idempotency:** unique `(account_id, venue_trade_id)` ở tầng fill (đã có pattern `0001_core_ledger.sql:274`); ingest dùng `INSERT ... ON CONFLICT DO NOTHING` và trả về số dòng mới/trùng; đọc lại cùng cửa sổ không làm thay đổi tổng (bất biến test bắt buộc).
4. **Partial fills:** mỗi fill là một dòng; position đóng khi tổng quantity các fill khớp/order terminal; entry dưới-plan (partial rồi timeout) vẫn tạo `outcome_positions` với `filled_quantity < planned_quantity` và ghi vào `coverage.partial_entry_count`.
5. **Recovery:** lệnh đi qua đường `futures_entry_recovery` (submission_unknown rồi tìm thấy) được merge về cùng intent qua `client_order_id` — không tạo position thứ hai; test bắt buộc: recovery tìm thấy fill không cộng đôi.
6. **Manual trades / lệnh ngoài bot:** fill có trên venue nhưng không có marker/provenance → đếm trong `coverage.unattributed_manual_detected`, không vào PnL bot, không vào funnel.

### 2.2. Phí, funding, tiền tệ

- Lưu **verbatim theo venue**: `fee/fee_currency`, `realized_pnl/realized_pnl_currency` từ `FuturesTradeFill` (`futures_trade_history.py:33-44`); funding lấy từ venue income (`incomeType=FUNDING_FEE`) với `funding_currency` riêng, kèm trường `venue_semantics` ghi rõ dấu (Binance: income dương = tiền nhận vào).
- **Không quy đổi FX trong ledger.** Tổng hợp theo bucket tiền tệ; tổng mixed-currency bị từ chối và trả `unavailable` kèm lý do (bất biến test).
- **Không đếm đôi:** công thức ròng từng exit:

  ```
  net_exit = realized_pnl − fee  (chỉ khi realized_pnl_currency == fee_currency,
                                   ngược lại giữ 2 thành phần riêng theo tiền tệ)
            + funding_received − funding_paid (theo từng funding event)
  ```

  Trượt giá đã nằm trong giá khớp (fill price) → dùng cho phân tích chất lượng thực thi, không trừ lần hai. Nếu `realized_pnl` nguồn của một venue tương lai đã gồm fee, phải ghi vào `venue_semantics` và công thức đổi theo — quyết định này nằm trong ingestion, có test pin.
- Funding `None`/thiếu → giữ `None`, hiển thị "chưa đủ dữ liệu" (tương thích contract `position_cost.py:21-28`); **không diễn giải funding thiếu = 0**.

### 2.3. Coverage & đối soát

Bảng `outcome_coverage` theo `(account_id, window_start, window_end)`:

- `fills_found`, `venue_realized_total` (từ income REALIZED_PNL), `ledger_realized_total`, lệch và tỷ lệ lệch;
- `status`: `ok / partial / error / unsupported / unavailable` — dùng nguyên vocabulary `FuturesTradeHistory` (`futures_trade_history.py:48`; `unsupported` = sàn không phải binance, `performance_history.py:202` đã trả status này) + `warnings[]`;
- sai số đối soát phải được công bố trong bản tin (không tổng khập khiễng).

### 2.4. Provenance (snapshot/model/config/git)

Mỗi dòng `outcome_positions` / `outcome_exits` mang `provenance JSONB`. Phân biệt rõ **đã có** và **là công việc mới**:

- **Đã có:** `run_settings()` (model/provider, analyst, rounds, vendors — `tradingagents/graph/trading_graph.py:316`, được `_log_state` ghi vào state tại `:577/:604`); `_run_signature` — digest sha256 của config + execution_context (`trading_graph.py:145-176`); `strategy_version` (cột có sẵn của `futures_cycles`); package version (`reporting.py:37` ghi `s('version')`, test pin tại `tests/test_reporting.py:143-149`).
- **Công việc mới thuộc seam này (đúng yêu cầu ask về "git provenance"):** thu `git_sha` + `dirty` **tại thời điểm cycle/ingest** và lưu vào provenance. Repo hiện chưa có chỗ nào chạy `rev-parse` (đã kiểm `findstr /s "rev-parse dirty git_sha"` trên tradingagents/+server/ — 0 kết quả). Quy tắc: chỉ gắn git_sha cho cycle/ingest **sau khi** bật worker; cycle cũ không được dán git hiện tại (không bịa provenance, đúng escalation 2026-11-10); cycle cũ thiếu git_sha → trường để trống với ghi chú `unavailable`.
- `prompt_version` nếu đã có tại thời điểm đó; nếu không có thì ghi thiếu — không suy đoán.
- giới hạn công bố: model hosted phía sau có thể đổi — ghi rõ trong bản tin (báo cáo §5G).

### 2.5. Migration mới tối thiểu — `0008_outcome_ledger.sql`

Một file migration additive (DDL thôi, đúng hợp đồng `server/migrations/README.md`):

```sql
CREATE TABLE outcome_positions (
    outcome_id      UUID PRIMARY KEY,
    account_id      UUID NOT NULL,          -- identity venue_key/profile, FK logic (không FK sang trading_accounts: bảng đó chưa có writer runtime)
    cycle_id        TEXT,                   -- NULL cho unattributed; FK logic sang futures_cycles
    client_order_id TEXT,
    venue_order_id  TEXT,
    symbol          TEXT NOT NULL,
    market_type     TEXT NOT NULL CHECK (market_type IN ('spot','perpetual','future')),
    side            TEXT NOT NULL CHECK (side IN ('long','short')),
    planned_quantity NUMERIC,
    filled_quantity  NUMERIC NOT NULL CHECK (filled_quantity >= 0),
    opened_at TIMESTAMPTZ, closed_at TIMESTAMPTZ,
    attribution TEXT NOT NULL CHECK (attribution IN ('managed','recovered','unattributed')),
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, client_order_id)
);
CREATE TABLE outcome_fills (... account_id, outcome_id, venue_trade_id, quantity, price,
    fee, fee_currency, realized_pnl, realized_pnl_currency, occurred_at,
    UNIQUE (account_id, venue_trade_id));
CREATE TABLE outcome_exits (... outcome_id, leg TEXT, exit_reason TEXT, quantity, price,
    net_per_currency JSONB, r_multiple NUMERIC, closed_at, provenance JSONB);
CREATE TABLE outcome_funding (... outcome_id, funding_at, amount NUMERIC, currency TEXT,
    venue_semantics TEXT NOT NULL);
CREATE TABLE outcome_coverage (... account_id, window_start, window_end, fills_found,
    ledger_realized_total JSONB, venue_realized_total JSONB, status TEXT CHECK (status IN
    ('ok','partial','error','unsupported','unavailable')), warnings JSONB NOT NULL DEFAULT '[]',
    unattributed_manual_detected BIGINT NOT NULL DEFAULT 0,
    UNIQUE (account_id, window_start, window_end));
```

(CHI TIẾT DDL cuối cùng được chốt ở stage implement; khung trên giữ các ràng buộc bắt buộc: unique idempotency, CHECK attribution, CHECK status, tiền tệ tách trường.) Không backfill, không viết lại hàng cũ. Ước lượng ~120-160 dòng SQL.

### 2.6. Module seam (đã duyệt) + wiring qua composition root

- `tradingagents/outcomes/` — logic tài chính thuần:
  - `attribution.py` (quyết định fill thuộc managed/recovered/unattributed),
  - `currency.py` (bucket tiền tệ, từ chối mixed sum),
  - `netting.py` (công thức ròng + guard không đếm đôi),
  - `coverage.py` (đối soát cửa sổ).
- `server/outcome_store.py` — store riêng theo concern: ghi/đọc PG (psycopg, pattern như `server/job_store.py`) với **pagination/cursor bền vững, dedup bền vững, bounded batch** — không chỉ đọc `list_futures_cycles(limit=20)` rồi bỏ mất dữ liệu.
- `server/outcome_ledger.py` — **worker riêng độc lập với autoloop**: đọc `futures_cycles` terminal đã lưu (snapshot/cycle đã bền vững trong PG qua `job_store` + `futures_cycle_telemetry.snapshot_payload`), ghép `FuturesTradeHistory` + income funding; bounded batch, **restart catch-up** (tiếp tục từ cursor đã lưu, không bỏ sót cycle trong khoảng nghỉ), không chặn scheduler, không thay lệnh/risk/protection.
- **Wiring duy nhất (composition root):** `outcome_ledger.start()/stop()` trong lifespan `server/main.py` (sau `position_monitor.start()`, dừng trong finally) — cùng pattern với `bracket_monitor`; **không thêm observer call/import nào vào `server/autoloop.py`, không nhét tính năng vào `job_store`**.
- Không đổi bất kỳ giá trị risk/limit/Auto nào; không thêm cờ runtime mới vào `_ENV_OVERRIDES` ngoài các khóa cấu hình worker cần thiết (interval, bật/tắt ingest — default off).

### 2.7. Tests chính xác cho stage `ledger`

| # | File | Kiểm chứng |
| --- | --- | --- |
| L1 | `tests/test_outcome_attribution.py` (unit) | managed/recovered/unattributed; marker khuyết → unattributed; không gán hồi tố; manual detected |
| L2 | `tests/test_outcome_currency.py` (unit) | bucket tiền tệ; mixed sum bị từ chối; `None` giữ `None`; fee/realized currency khác nhau giữ 2 thành phần |
| L3 | `tests/test_outcome_netting.py` (unit) | net = realized − fee + funding; không đếm đôi trượt giá; venue_semantics đổi nghĩa → công thức đổi theo (test pin) |
| L4 | `tests/test_outcome_ledger_postgres.py` (PG qua fixture `postgres_schema`) | ingest idempotent (đọc lại không đổi tổng); partial fills; recovery merge không cộng đôi; coverage đối soát; status `unsupported` (sàn không phải binance) được giữ nguyên trong coverage; schema migration 0008 áp được |
| L5 | `tests/test_outcome_ledger_worker.py` (PG) | worker độc lập autoloop: cursor/pagination bền vững (không bỏ cycle khi gap), dedup bền vững, bounded batch, restart catch-up, không chặn scheduler |
| L6 | `tests/test_futures_simulation.py` (unit, mục 3) | parity risk decision + protection transitions; OHLC ambiguity; chi phí không đếm đôi; offline |
| L7 | Cập nhật danh sách migration: `tests/test_postgres_migrations_integration.py:87-95, 140-148` thêm `0008_outcome_ledger.sql`; `tests/test_postgres_migration_contract.py` dry-run list |
| L8 | Regressions phải còn xanh: `tests/test_futures_replay.py`, `tests/test_performance_truthfulness.py`, `tests/test_execution_risk.py`, `tests/test_profit_protection.py`, `tests/test_scale_out.py`, `tests/test_futures_entry_recovery_scale_out.py` |

Lệnh stage (do runner chạy, mục 10):

```bash
.venv\Scripts\python.exe scripts\verify_research_delivery.py --stage ledger
```

## 3. Giai đoạn 1b — Futures simulation event-driven (Hạng mục 1b, nằm trong stage `ledger`)

### 3.1. Thiết kế

- Module seam `tradingagents/simulation/` (đã duyệt):
  - `engine.py` — vòng sự kiện theo thời gian: mỗi nến đóng → cập nhật mark → gọi `evaluate_profit_protection` (giữ nguyên chữ ký) → quyết định exit/scale-out (`plan_scale_out`) → sinh fill mô phỏng → update position/cash;
  - `fills.py` — khớp lệnh mô phỏng từ OHLC: market fill tại giá khả dụng của nến (open cho lệnh vào lúc nến mở sau quyết định; stop/target khớp tại mức stop/target với trượt giá mô hình), tick/lot rules từ market info;
  - `costs.py` — fee theo maker/taker rate, funding series, trượt giá mô hình **một nguồn duy nhất**: trượt chỉ đổi giá khớp, fee tính trên giá đã trượt, không trừ lần hai; funding áp theo cửa sổ nến giao giữa các mốc funding;
  - `data.py` — đọc nến **dated/point-in-time** qua dataflows hiện có (không look-ahead: một run dated trong quá khứ chỉ đọc dữ liệu tới ngày đó — hợp đồng v0.5 trong AGENTS.md).
- **Reuse bắt buộc (không fork logic):** gọi trực tiếp `RiskGuard.check`, `sizing.compute`, `evaluate_profit_protection`, `plan_scale_out`, và chính sách time review — parity runtime là tiêu chí nghiệm thu (báo cáo §5B).
- **OHLC ambiguity phải công bố:** khi stop và target cùng nằm trong khoảng high–low của một nến, policy mặc định là **worst-first** (giả định stop chạm trước) và đếm vào `ambiguity_count` xuất hiện trong report; tham số `--ambiguity-policy worst-first|skip` ở mức CLI, mọi báo cáo phải in policy đã dùng. Không dùng high/low nội suy để "giải" mơ hồ một cách ngầm.
- **Offline tuyệt đối:** simulation không import exchange client, không tạo `ExchangeBridge`, không đọc `exec_base_url` để gọi mạng; unit test chặn mạng sẵn có của conftest (`tests/conftest.py:66-83`) là lớp bảo vệ — thêm test asserting module import không kéo ccxt network path.
- **Không phải bằng chứng lợi nhuận:** mọi artifact sim gắn nhãn `simulation` và ghi disclaimer như user-facing output hiện có (AGENTS.md).

### 3.2. Parity contract (tiêu chí nghiệm thu)

1. Cùng proposal (PlannedOrder + portfolio + limits) → cùng `RiskDecision` giữa `RiskGuard.check` trong sim và runtime (fixture đối chiếu ghi lại both sides).
2. Cùng chuỗi mark/funding/fee → cùng chuỗi `ProfitProtectionDecision.phase` (break_even/profit_lock/trailing/time_stop/unchanged) giữa sim và monitor hiện tại.
3. Report phải có: đường vốn (equity curve), drawdown, kỳ vọng ròng sau chi phí, độ nhạy chi phí (±fee/slippage), `ambiguity_count`, phân bố exit_reason.

## 4. Giai đoạn 2 — Experiments: so sánh phiên bản ngoài mẫu (Hạng mục 2, stage `experiments`)

### 4.1. Thiết kế

- Module seam `tradingagents/experiments/` (đã duyệt):
  - `walkforward.py` — chia train/holdout theo thời gian; **purge + embargo** cho nhãn/nến nhiều ngày chồng lấn (purge = (horizon − 1) ngày giữa train và test; embargo thêm biên sau test trước khi tái dùng); tự kiểm không rò rỉ;
  - `baselines.py` — baseline bắt buộc: (1) no-trade, (2) quy tắc đơn giản (tái dùng `server.futures_replay.RULES` + sim), (3) bot hiện tại, (4) bot + một thay đổi (ablation một biến: memory on/off, thêm/bỏ Kronos, số vòng tranh luận, model nhanh/sâu, điều kiện soft-cap);
  - `runner.py` — seed/repeat: `--repeat N --seed S`, **lưu tất cả các lần chạy** (không chọn kết quả đẹp); LLM chi phí (token, calls) + latency ghi từng lần;
  - `artifacts.py` — thư mục `experiments/<run_id>/`: `manifest.json` (giả thuyết, dữ liệu+nguồn nến, seed, repeat, git sha, config hash, tiêu chí so sánh đã khóa), per-repeat results, aggregate summary; **không tự đặt trong repo** nếu kích thước lớn — mặc định dưới `TRADINGAGENTS_RESULTS_DIR` (mặc định `~/.tradingagents/`, không viết vào repo theo AGENTS.md);
  - `isolation.py` — **memory isolation:** mỗi experiment chạy với `TRADINGAGENTS_RESULTS_DIR/CACHE_DIR/MEMORY_LOG_PATH` chỉ vào thư mục experiment (pattern `tests/conftest.py:37-51`); experiment không được đọc bộ nhớ operator, không được ghi decision vào memory log thật; test tự động chặn (assert memory log path trong sandbox).
- **No auto-promotion:** runner chỉ xuất báo cáo + artifact; không có code path nào tự sửa config runtime/operator settings/risk limits. Tiêu chí so sánh **đăng ký trước** trong manifest trước khi xem kết quả (khóa `criteria` trước khi chạy). Promote là PR review riêng theo AGENTS.md.

### 4.2. Lệnh người dùng chạy được (user-visible)

```bash
# Thí nghiệm deterministic (không LLM): so hành vi QUYẾT ĐỊNH của rule trên cycle đã ghi.
# Đây là gate-agreement replay (rule có đồng ý với bản ghi không) — KHÔNG phải thí nghiệm tài chính:
# export chỉ mang 2 nến quyết định/cycle nên không dựng được equity/PnL, và report ghi rõ `unavailable`.
.venv\Scripts\python.exe -m tradingagents.experiments.replay_experiment --rule baseline --rule htf_dominance_bounded_pullback --window 2026-06-01:2026-08-31 --seed 7 --repeat 3

# Thí nghiệm TÀI CHÍNH (không LLM): import chuỗi nến đã đóng rồi chạy sim thật qua runner/engine.
# Cần `--candles` (JSON hoặc CSV: metadata symbol/timeframe/venue + OHLCV closed, UTC ms) và
# `--holdout-start`; split assert no-leakage. Financial part hữu hạn khi dữ liệu đủ (fills, equity,
# net sau phí/funding, phân bố exit_reason); thiếu dữ liệu → coverage/reason, không bao giờ 0 giả.
# `--derivatives-exposure-pct` là override NGHIÊN CỨU cho cap notional (runtime default 0 chặn mọi
# entry derivatives) — chỉ đổi simulation này, không ghi runtime config.
.venv\Scripts\python.exe -m tradingagents.experiments.financial_experiment --candles tests/fixtures/futures_candles_4h.json --fixture tests/fixtures/futures_cycles_replay.json --holdout-start 2026-10-26 --seed 7 --repeat 3 --derivatives-exposure-pct 50

# Thí nghiệm có LLM (chi phí thật, chỉ khi user kích hoạt): so bot hiện tại với 1 thay đổi
.venv\Scripts\python.exe -m tradingagents.experiments.run --baseline no_trade,rule,current --variant memory_on --window ... --holdout 2026-09-01:2026-09-30 --purge-days 6 --seed 7 --repeat 3

# Đọc lại kết quả (không chạy LLM)
.venv\Scripts\python.exe -m tradingagents.experiments.report experiments/<run_id>
```

**Hai con số KHÁC NHAU, không được lẫn (đối chiếu report review 2026-11-10):** trên cùng fixture 551 cycle, `replay_experiment` in `rate 0.3117 (24/77)` — đó là **confirmed-rate** (rule xác nhận 24 trong 77 cycle có context), còn `scripts/replay_futures_decisions.py` in `decision_agreements 77/77, decision_disagreements 0` — đó là **agreement** giữa rule và `confirmation_reason` đã ghi. Cả hai đọc đúng cùng 77 row (`rule_reasons` == `recorded_reasons` trên cả 77) nên chúng KHÔNG mâu thuẫn; một bên đo "rule nói gì", bên kia đo "rule có khớp bản ghi không".

(Các lệnh trên là **hợp đồng thiết kế** cho stage implement; tham số cuối cùng được pin trong stage đó và phải xuất hiện đúng trong README/artifact.)

### 4.3. Tests chính xác cho stage `experiments`

| # | File | Kiểm chứng |
| --- | --- | --- |
| E1 | `tests/test_experiments_walkforward.py` | purge/embargo loại chồng lấn nhãn; assert không giao thời gian giữa train/test; case nến 4d chồng lấn |
| E2 | `tests/test_experiments_runner.py` | seed/repeat lưu đủ N lần; manifest khóa criteria trước chạy; LLM cost/latency per-repeat; **không có code promotion** (assert không import operator_config/settings writer) |
| E3 | `tests/test_experiments_isolation.py` | RESULTS_DIR/CACHE_DIR/MEMORY_LOG_PATH sandboxed; experiment không ghi memory operator (tmp home khác) |
| E4 | `tests/test_experiments_baselines.py` | no-trade/rule/current/ablation chạy cùng sim; rule baseline tái dùng `futures_replay.RULES` không fork logic |
| E5 | Regressions: `tests/test_backtest.py`, `tests/test_memory_pointintime.py`, `tests/test_futures_simulation.py` (từ stage 1) |

## 5. Giai đoạn 3 — Bản tin vận hành + dashboard redesign (Hạng mục 3, stage `dashboard`)

### 5.1. Bản tin vận hành (operating brief)

- **Nơi hiển thị: dashboard tại `/` — KHÔNG Telegram** (không thêm kênh push trong phạm vi này).
- API mới `GET /api/brief` (hoặc `/api/operating-brief`): **read-only aggregate**, tổng hợp từ store hiện có: `futures_cycles` (funnel/theo giai đoạn), portfolio snapshot, fills coverage (từ `outcome_coverage` nếu đã bật, nếu chưa → mục "kết quả" hiển thị `unavailable` rõ ràng), health, audit gần nhất. Không có route POST/PUT/DELETE; không đổi state.
- Nội dung theo mẫu báo cáo §5F (tiếng Việt, template — không phải số tài khoản thật):

  > Trạng thái: Auto Spot/Futures, venue Demo/production.
  > Kết quả: PnL đã đối soát + phí + funding; phần chưa đủ dữ liệu ghi rõ.
  > Rủi ro: vị thế mở, exposure/heat đang dùng, vị thế thiếu/không chắc protection.
  > Hoạt động: số ứng viên, số phân tích, số lệnh; 3 lý do chính khiến không vào lệnh (từ `execution_status/plan_reason` vocabulary có sẵn `autoloop.py:2243-2263`).
  > Cần xử lý: lỗi credential, dữ liệu quá cũ, lệnh chờ đối soát — kèm link cycle.

- **Financial truthfulness (bất biến test):**
  - không bịa net PnL: tổng chưa đối soát → hiển thị `chưa đối soát`, không phải 0;
  - phân biệt rõ "không có tín hiệu" (cycle completed/rejected hợp lệ) với "pipeline lỗi" (cycle error/timeout) — hai nhóm trạng thái khác nhau trong vocabulary cycle;
  - sai số đối soát và cảnh báo coverage được công bố nguyên văn;
  - disclaimer research framework giữ nguyên ở user-facing output.
- Cảnh báo tạm thời chỉ gắn trong brief (mục "cần xử lý"); alert push một chiều là phạm vi sau, không thuộc ask này.

### 5.2. Redesign UX/UI (đã được user duyệt decomposition)

- Trang `/` tách thành section components dưới `web/components/overview/brief/` (đã duyệt decomposition): brief card đặt **trên cùng** trang overview.
- **Điều phối với UX plan:** [docs/plans/dashboard-operating-brief-ux-20261110.md](dashboard-operating-brief-ux-20261110.md) là bản thiết kế chi tiết cho hạng mục 3 và governs decomposition. Hai điểm UX plan đã tự cờ cần user chốt khi tới stage implement: (1) bỏ khối positions lặp khỏi `PerformanceCard` (UX plan §4/§13.2, dòng 368) — nằm ngoài câu "giữ nguyên các card hiện có" của mục này, chỉ thực hiện sau khi user xác nhận lời; (2) server đang bind `0.0.0.0` (UX plan §13.4, dòng 370) — rà authentication trước khi brief hiển thị venue production. Mục này chỉ tạo section brief + API client + i18n; các đổi card khác đi theo quyết định của UX plan tại stage đó.
- API client + i18n: thêm fetcher trong `web/lib/api.ts` (tầng lỗi chung `web/lib/api.ts:1-78`), message tiếng Việt trong `web/lib/i18n/messages`.
- Đổi nhỏ bắt buộc cho verify: `web/next.config.ts` thêm `distDir: process.env.NEXT_DIST_DIR || ".next"` (Next 16 đã bỏ `--dist-dir` — xác minh bằng `next build --help` trong phiên này); `web/package.json` thêm script `"typecheck": "tsc --noEmit"`.

### 5.3. Offline production fixture browser test

- Fixture backend cục bộ (`tests/browser/brief_fixture_server.py`): trả JSON canned cho các endpoint brief dùng (không mạng ngoài, không DB); chạy trên `127.0.0.1:<port>` qua FastAPI test server hoặc uvicorn cục bộ.
- Build production: `next build` với `NEXT_DIST_DIR=.next-verify` và `API_INTERNAL_URL=http://127.0.0.1:<fixture_port>`; serve bằng `next start` cùng env; **không đụng `.next`** của dev server.
- Browser check qua **CDP + websockets** (websockets 17.4 đã có transitive; không thêm Playwright): mở trang `/`, assert (1) brief card render đúng giá trị fixture, (2) các trường chưa đủ dữ liệu render trạng thái explicit (không hiện 0), (3) không request nào ra ngoài localhost (offline). File: `tests/browser/test_dashboard_brief_offline.py`, marker `integration` (cần Chrome).
- Shadow inference smoke (bổ sung cho `--full`, xem mục 6.4).

### 5.4. Tests chính xác cho stage `dashboard`

Gate của stage này là `verify --stage dashboard`, runner chạy **cả pytest + web trio + browser check** trong một lệnh (đã cập nhật runner 2026-11-10 theo review B3):

| # | File/Khối | Kiểm chứng |
| --- | --- | --- |
| D1 | `tests/test_operating_brief.py` (unit + API qua TestClient) | aggregate read-only; truthfulness None/unavailable; phân biệt no-signal vs error; dedup "cần xử lý"; không route thay đổi state |
| D2 | web typecheck: `node node_modules/typescript/bin/tsc --noEmit` (thiếu node_modules là FAIL prerequisite, không skip) |
| D3 | web lint: `eslint .` qua node |
| D4 | web build: `next build` với `NEXT_DIST_DIR=.next-verify` (runner tự dọn `.next-verify` — chỉ tài nguyên nó tạo; NEXT_DIST_DIR chưa wired → SKIPPED/not-implemented, không fake pass) |
| D5 | `tests/browser/test_dashboard_brief_offline.py` (integration) — offline production fixture; runner chạy với `-m integration` (addopts mặc định deselect integration — nếu không truyền cờ, pytest collect 0 test và exit 0 = fake pass, đã vá runner) |
| D6 | Regressions: `tests/test_performance_truthfulness.py`, các test API portfolio/audit hiện có |

Trạng thái tổng hợp của stage: exit 0 khi tất cả PASS; exit 2 khi có phần chưa implement (brief pytest, NEXT_DIST_DIR chưa wired, browser test chưa có); exit 1 khi phần nào chạy và FAIL.

## 6. Giai đoạn 4 — Kronos shadow + bộ lọc đơn giản (Hạng mục 4, stage `shadow`)

### 6.1. Nguyên tắc bất di bất dịch

1. **Shadow inference không tác động quyết định:** module shadow không được import bởi graph/decision path; chạy như **worker riêng** đọc dữ liệu cycle/snapshot đã lưu bền vững trong PG (không hook vào vòng cycle); lỗi shadow không bao giờ làm cycle thất bại (try/except biên, log warning). Test bất biến: assert không module nào trong `tradingagents/graph/` + `tradingagents/agents/` + `server/autoloop.py` import `tradingagents.shadow`.
2. **Chỉ nến đã đóng** (`Candle(is_closed=True)` — hợp đồng `tradingagents/dataflows/market_data.py` mà `futures_replay.py:60` đã dùng); đúng venue/loại hợp đồng/timeframe; lưu `data_open_time` và `model_revision`.
3. **Optional + isolated + pinned:** Kronos không vào `dependencies`; cài vào **venv riêng ngoài repo** (`.venv-shadow` tại `%LOCALAPPDATA%\tradingagents-research\`, hoặc `pip install --target` thư mục riêng), pin git revision + hash weights; provider local chạy inference tại chỗ. `artifact/source/model revision` ghi vào từng dòng observation. Thiếu package → status `unavailable` **rõ ràng** trong observation và trong bản tin; unit test chạy được khi không có package (không fake output).
4. **Không cập nhật calibrated probability:** chỉ mô tả phân phối mô hình ("72/100 kịch bản mô hình kết thúc cao hơn sau 24h, horizon X, ngày dữ liệu Y" — đúng như báo cáo §5D yêu cầu).

### 6.2. Bộ lọc đơn giản (baseline filters)

- `tradingagents/shadow/filters.py`: bộ lọc rẻ chạy cùng nến đóng (ví dụ: hướng 4h + volume tối thiểu; breakout Donchian; funding-signed) — cùng hợp đồng đầu vào/đầu ra với Kronos provider để thí nghiệm so sánh được (báo cáo §5D).
- Persist cùng bảng với Kronos, trường `provider='filter:<name>'` vs `provider='kronos:<revision>'`.

### 6.3. Migration + lưu trữ

- `0009_shadow_forecasts.sql` (additive, minimal): `shadow_forecasts` (forecast_id, cycle_id nullable, symbol, timeframe, horizon_hours, provider, model_revision, artifact_hash, source_revision, data_open_time, **prediction_created_at** — thời điểm dự báo thực sự được tạo, **generation_kind** `forward | retrospective` — replay muộn phải dán nhãn retrospective, không trình bày như dự báo forward, samples JSONB (dir-rate, percentile band, dispersion), status ok/unavailable/error, created_at) + `shadow_evaluations` (forecast_id, realized_direction_hit, band_coverage, stop_first_rate, net_r nếu đã có ledger, evaluated_at, **outcome_group** `executed | rejected | abstained` để đánh giá đúng theo nhóm). Ràng buộc: unique theo `(provider, symbol, data_open_time, horizon_hours, model_revision)` để idempotent.

### 6.4. Module seam (đã duyệt) + worker riêng qua composition root (theo escalation 2026-11-10)

- `tradingagents/shadow/`: `provider.py` (protocol), `kronos.py` (optional import, isolated path), `filters.py`, `recorder.py`, `evaluation.py`.
- `server/shadow_store.py` — store riêng theo concern với **pagination/cursor bền vững, dedup bền vững, bounded batch** — không dùng `list_futures_cycles(limit=20)` rồi bỏ mất dữ liệu.
- `server/shadow_worker.py` — **worker riêng độc lập, KHÔNG thêm observer call/import vào `server/autoloop.py`, không nhét tính năng vào `job_store`**:
  - đọc snapshots/cycles **đã lưu bền vững** trong PG (payload snapshot của `futures_cycles`), quét đủ snapshot đủ điều kiện theo scope, phân biệt `executed / rejected / abstained` — **không coi observation của các lệnh đã executed là mẫu toàn universe**;
  - nếu cần thêm OHLCV cho Kronos: worker lấy historical closed candles với cutoff/as_of của snapshot — **không lấy nến sau quyết định**;
  - `prediction_created_at` ghi đúng thời điểm tạo dự báo; replay muộn dán `generation_kind='retrospective'` và được đánh giá riêng, không trộn với forward;
  - không bịa git/prompt provenance cho cycle cũ bằng git hiện tại — provenance chỉ từ dữ liệu lưu tại thời điểm cycle (nếu thiếu → ghi thiếu);
  - bounded batch, restart catch-up từ cursor, isolation, không chặn scheduler, lỗi provider không ảnh hưởng cycle;
  - lifecycle start/stop qua composition root `server/main.py` lifespan (pattern bracket_monitor) — **không wiring vào autoloop, không wiring vào risk limits hay decision path**.

### 6.5. Tests chính xác cho stage `shadow`

| # | File | Kiểm chứng |
| --- | --- | --- |
| S1 | `tests/test_shadow_forecasts.py` (unit + PG) | chỉ nến đóng; persistence idempotent; evaluation fields + outcome_group executed/rejected/abstained; `unavailable` khi thiếu package; baseline filter cùng hợp đồng |
| S2 | `tests/test_shadow_worker.py` (PG) | worker độc lập autoloop: cursor/pagination bền vững (không bỏ cycle), dedup, bounded batch, restart catch-up; phân biệt executed/rejected/abstained; đọc snapshot đủ điều kiện theo scope; as_of cutoff — không nến sau quyết định; replay muộn dán retrospective; không bịa provenance cũ |
| S3 | `tests/test_shadow_isolation.py` | graph/agents không import shadow; worker lỗi không ảnh hưởng cycle/scheduler; lifecycle qua composition root |
| S4 | `tests/test_shadow_kronos_smoke.py` (smoke, conditional) | inference thật tại chỗ **chỉ khi** package cài được trong env isolated; nếu không có → skip có lý do explicit ("kronos package not installed in isolated env") |
| S5 | Migration: cập nhật expected lists trong `tests/test_postgres_migrations_integration.py` + dry-run contract cho `0009` |
| S6 | Regressions: `tests/test_futures_replay.py`, funnel tests autoloop hiện có |

## 7. Seams đã duyệt và seam mới cần phê duyệt

### 7.1. Đã duyệt (user) — ghi lại biên giới module cụ thể

| Seam | Biên giới |
| --- | --- |
| Outcome ledger | `tradingagents/outcomes/` (attribution/currency/netting/coverage) + `server/outcome_store.py` + `server/outcome_ledger.py` (worker riêng qua composition root) + migration 0008 |
| Futures simulation | `tradingagents/simulation/` (engine/fills/costs/data) |
| Experiments | `tradingagents/experiments/` (walkforward/baselines/runner/artifacts/isolation) |
| Operating brief + dashboard sections | `server/operating_brief.py` + `web/components/overview/brief/*` + `web/lib/i18n/messages` |
| Shadow forecasts | `tradingagents/shadow/` + `server/shadow_store.py` + `server/shadow_worker.py` (worker riêng qua composition root) + migration 0009 |

### 7.2. Wiring nhỏ (composition root, đúng phạm vi đã duyệt)

1. `server/main.py` lifespan: `outcome_ledger.start()/stop()` và `shadow_worker.start()/stop()` (pattern bracket_monitor) — workers đọc dữ liệu đã lưu bền vững, độc lập với autoloop.
2. `cli/main.py`: thêm subcommand experiments (Typer) — chỉ surface, không logic mới trong cli.
3. **Không chạm `server/autoloop.py`**: không observer call, không import shadow/outcome vào autoloop, không nhét tính năng vào `job_store` (quyết định escalation 2026-11-10).
4. Không đổi: `RiskGuard` limits, `RiskLimits`, auto authorization, venue checks, identity/protection checks.

### 7.3. Technical debt đã đăng ký — chưa quyết định

- **`server/autoloop.py` (117.131 bytes, ~74 symbol) hội tụ đủ split signal** (god file: scheduler + screening/refill + execution pipeline + telemetry wiring trong một file). Đề xuất seam khi nào đó được quyết định: `server/autoloop_candidates.py` (preflight/screening/refill), `server/autoloop_execution.py` (execution pipeline `_handle_plan`), giữ `autoloop.py` làm entry/scheduler. **Trạng thái: chưa được quyết định (không ghi là duyệt hay từ chối); delivery này không chạm file; không thêm ai-note vào file không sửa.** Nếu exploration sau cho thấy một yêu cầu thật sự không thể thực hiện từ dữ liệu/boundary hiện có → escalation đúng constraint cụ thể, không tự mở rộng refactor.

## 8. Runner kiểm chứng — `scripts/verify_research_delivery.py`

Yêu cầu từ ask (bắt buộc đúng):

1. PG test **disposable** dùng `tests/postgres-compose.yml` nhưng **project name riêng ngẫu nhiên** (`ta-verify-<8 hex>` — không đụng `ta-migration-test-{pid}` của migration integration tests).
2. `TRADINGAGENTS_TEST_DATABASE_URL` **chỉ inject vào child process của pytest**; runner **không đọc** `TRADINGAGENTS_DATABASE_URL` (runtime DSN) — chỉ pop nó khỏi child env để không rò.
3. `finally` cleanup chỉ tài nguyên test sở hữu: `docker compose -p <own-project> down --volumes`; không chạm project khác; nếu Docker unavailable → part bị đánh dấu skip có lý do, **không** fake pass (exit không phải 0).
4. `--stage ledger|experiments|dashboard|shadow` và `--full`.
5. `--full`: pytest có PG (fixture `postgres_schema`), migration integration tests tự spawn project riêng của nó (`ta-migration-test-{pid}`), smoke imports (`import tradingagents, cli.main`), web typecheck/lint/build (build với `NEXT_DIST_DIR=.next-verify` — không tranh `.next` với dev), offline browser fixture test dashboard, shadow inference smoke khi package cài được isolated.
6. Stage chưa implement (chưa có file test của stage) → exit 2 với thông báo explicit — không fake pass, không tự skip im lặng.

Cập nhật sau review độc lập 2026-11-10 (patch B1-B3):

- **B1 — phân loại kết quả:** `note()` của `--full` dùng đúng 3 bucket: exit 0 → PASS, exit 2 (chưa implement) → SKIPPED (loud), **mọi exit khác (đã chạy và thất bại) → FAIL** và làm `--full` exit 1. Một part conditional (browser/shadow) đỏ không thể biến thành skip. Thiếu `web/node_modules` là FAIL prerequisite (không phải skip) — full không được xanh khi web không kiểm được.
- **B2 — browser check chạy thật:** `_browser_offline_check` truyền `-m integration` khi gọi pytest, vì pyproject `addopts` mặc định deselect integration (`pyproject.toml:74`); không có cờ này pytest collect 0 test và exit 0 = fake pass.
- **B3 — gate stage dashboard đủ mạnh:** `verify --stage dashboard` chạy trong một lệnh: brief pytest (nếu đã có) + web typecheck + web lint + web build + browser check; tổng hợp exit 0 (tất cả PASS) / 1 (part nào FAIL) / 2 (part nào chưa implement). Thiếu brief pytest không chặn việc chạy web parts — chạy được gì chạy nấy, thiếu gì báo nấy.
- Shell khác đã phát hiện khi patch: vòng `web_results` cũ thêm `web-<name>` vào `skipped` hai lần cho exit 2 (note() một lần, dòng kế một lần) — đã bỏ khi viết lại `note()`.

Hành vi đã code trong runner (phiên này): lifecycle compose (up --wait → port → readiness loop → pytest child env → finally down), ánh xạ stage→test files, web trio, ruff, smoke imports, exit codes 0/1/2, hoặc `--help` để xem đầy đủ.

## 9. Thứ tự thực thi khi implement (sau ask này)

| Bước | Nội dung | Điều kiện đi tiếp |
| --- | --- | --- |
| 1 | Stage `ledger`: migration 0008 + `tradingagents/outcomes/` + `server/outcome_{store,ledger}.py` (worker riêng, cursor/catch-up) + wiring lifespan + tests L1-L8 | `verify --stage ledger` exit 0; regressions xanh |
| 2 | Stage `ledger` tiếp: `tradingagents/simulation/` + tests L6 (parity + ambiguity + chi phí) | parity contract mục 3.2 đạt |
| 3 | Stage `experiments`: modules + CLI + tests E1-E5 | `verify --stage experiments` exit 0; không có code promotion |
| 4 | Stage `dashboard`: brief API + web sections + typecheck script + distDir + offline browser test | `verify --stage dashboard` exit 0 — gate gồm cả brief pytest + web trio + browser check (runner mục 8) |
| 5 | Stage `shadow`: migration 0009 + `tradingagents/shadow/` + `server/{shadow_store,shadow_worker}.py` (worker qua composition root) + tests S1-S6 | `verify --stage shadow` exit 0; bất biến no-decision-impact (không import từ graph/agents/autoloop) |
| 6 | `--full` xanh toàn bộ; đọc lại kết quả, đối soát; chỉ sau đó cân nhắc mở thí nghiệm LLM | — |

Mỗi bước là một đợt code riêng theo AGENTS.md (ruff + pytest đúng scope + self-review diff + escalation covenant khi chạm file có split signal).

## 10. Rủi ro, giới hạn, non-goals

- **Chưa có bằng chứng tài chính nào từ kế hoạch này** — mọi số liệu là thiết kế; mô phỏng không phải chứng minh lợi nhuận (báo cáo §7).
- Kronos pretrain cutoff chưa xác minh được cho crypto (báo cáo §5D) — forward test sau khi khóa revision là điều kiện kết luận; backtest lịch sử chỉ mang tính tham khảo.
- Độ chi tiết dữ liệu lịch sử fills/funding hiện có quyết định chất lượng đối soát stage 1 — nếu venue history quá mỏng, `coverage` sẽ trả `partial/unavailable` và đó là kết quả hợp lệ, không phải lỗi.
- Non-goals (không làm trong delivery): Telegram/push channel, giao dịch qua chat, NautilusTrader migration, Qlib/RD-Agent phòng lab, đổi execution engine, memory tự huấn luyện.
- Docker Desktop trên Windows có thể cần khởi động lại khi `docker info` lỗi — recovery theo native memory (restart trước, WSL reset cần xác nhận).

## 11. Phụ lục — lệnh đã chạy trong phiên khảo sát này

| Lệnh | Kết quả |
| --- | --- |
| `git status --short` / `git log --oneline -5` | main @ 0943103; `?? docs/research/` untracked |
| `git checkout -b feat/trading-research-delivery-20261110` | branch mới từ HEAD, thay đổi giữ nguyên |
| `docker info --format "{{.ServerVersion}}"` | 29.9.2 |
| `.venv\Scripts\python.exe --version` | Python 3.13.14 |
| `.venv\Scripts\python.exe -m pip show websockets` | 17.4 (transitive) |
| `node node_modules/next/dist/bin/next build --help` (từ web/) | không có `--dist-dir` → cần `distDir` config |
| Đọc files | AGENTS.md, web/AGENTS.md, docs/research/...vi.md, conftest.py, 0001/0002 migrations, futures_trade_history.py, position_cost.py, performance.py, performance_history.py, futures_replay.py, profit_protection.py, sizing.py, risk.py, scale_out.py, futures_entry_journal.py, autoloop.py (trích), job_store.py (trích), main.py, next.config.ts, package.json, ci.yml, test_postgres_migrations_integration.py, test_postgres_fixture_isolation.py, test_postgres_migration_contract.py, migrations/README.md |

*Lưu ý kỷ luật: ruff check và pytest chưa chạy trong ask này vì không có thay đổi code Python runtime (chỉ plan + runner mới); việc xác minh runner được ghi ở mục 12 sau khi tạo file.*

## 12. Xác minh của ask này (kết quả thực chạy 2026-11-10)

| Lệnh | Kết quả |
| --- | --- |
| `.venv\Scripts\python.exe -m ruff check scripts\verify_research_delivery.py` (2 lần, lần đầu bắt SIM210 + UP037 → sửa) | **All checks passed!** |
| `.venv\Scripts\python.exe -m ruff check .` (toàn repo) | **All checks passed!** |
| `.venv\Scripts\python.exe scripts\verify_research_delivery.py --help` | hiển thị `--stage {dashboard,experiments,ledger,shadow}` và `--full` |
| `... --stage ledger` (chưa implement) | exit **2**, thông báo explicit "not implemented yet", 6 file test missing (sau khi thêm test worker) |
| `... --stage experiments` / `--stage dashboard` | exit **2**, thông báo explicit |
| `... --stage shadow` (chưa implement, mapping 3 file sau khi thêm test worker) | exit **2**, thông báo explicit (chạy lại sau khi cập nhật mapping theo escalation) |
| Lifecycle PG qua runner (inline: `_TestPostgres` + `_run_pytest(['tests/test_bracket_monitor.py::test_no_active_tasks_does_not_construct_executor'], env=_child_env(dsn))`) | **1 passed in 5.84s**; compose project `ta-verify-513e7608` up → healthy → port → pytest child → `down --volumes` gọn (xác nhận `docker ps -a --filter name=ta-verify` rỗng) |
| `.venv\Scripts\python.exe -c "import tradingagents, cli.main"` | sạch (clean-import smoke như CI) |
| `.venv\Scripts\python.exe -m pytest -q tests/test_postgres_migration_contract.py tests/test_futures_replay.py tests/test_postgres_fixture_isolation.py` | **18 passed in 1.88s** (focused sanity — full suite chưa chạy trong ask này, runner `--full` là công cụ chạy full suite cho các stage implement) |

### 12.1. Xác minh patch sau review độc lập (B1-B7) — chạy 2026-11-10

Review độc lập chỉ-read đã đối chiếu ~35 file và chỉ ra B1-B7; các claim của review được xác nhận lại trước khi vá bằng cách đọc trực tiếp: `tradingagents/reporting.py:37` (chỉ ghi `s('version')`), `tests/test_reporting.py:143-149` (pin `__version__`), `tradingagents/graph/trading_graph.py:145-176` (`_run_signature` sha256 settings + context digest), `:316` (`run_settings`), `:577/:604` (`_log_state`), `futures_trade_history.py:48` (Literal có `unsupported`), `performance_history.py:202` (trả `"unsupported"`), `docs/plans/dashboard-operating-brief-ux-20261110.md:368, :370` (self-flag performance-card + 0.0.0.0 bind).

| Vá | Xác minh thực chạy |
| --- | --- |
| B1 — `note()` 0/2/else | script kiểm tạm (`%TEMP%\check_note_semantics.py`): assert routing exit 1→failed (kể cả part conditional), exit 2→skipped, 0→passed; assert source còn `-m integration` ở cả browser + migration checks → **OK cả hai assert** |
| B2 — browser check `-m integration` | `.venv\Scripts\python.exe -m pytest --collect-only -q tests\test_postgres_migrations_integration.py` → **exit 5, "no tests collected (2 deselected)"** (chứng minh addopts deselect); cùng lệnh thêm `-m integration` → **"2 tests collected"** (chứng minh cờ override addopts) |
| B3 — dashboard gate đủ mạnh | `verify_research_delivery.py --stage dashboard` → chạy thật: `tsc --noEmit` **PASS**, `eslint .` **PASS**, build SKIPPED (NEXT_DIST_DIR chưa wired), browser SKIPPED (file chưa có), brief-pytest SKIPPED → tổng **exit 2** với bảng per-part; khi implement xong gate sẽ là exit 0/1 thật |
| B4/B5/B6/B7 — plan text | đã sửa §2.1 (join key bền: cycle payload order_id ↔ journal legs ↔ fill.order_id/trade_id; bỏ approval_id), §2.3/§2.5 (thêm `unsupported` vào vocabulary + CHECK), §2.4 (tách "đã có" = `_run_signature`/`run_settings`/`_log_state`/package version vs "công việc mới" = git_sha+dirty thu tại thời điểm, cycle cũ không dán git hiện tại), §5.4/§8/§9 (gate dashboard gồm web trio + browser check), §5.2 (điều phối UX plan §13.2/§13.4) |
| Regression runner | `--stage ledger` exit 2 explicit (6 file mapped); `ruff check .` toàn repo **All checks passed** sau mọi edit |

Bug phụ phát hiện khi vá B1: vòng `web_results` cũ thêm `web-<name>` vào `skipped` hai lần cho exit 2 — đã loại bỏ khi viết lại `note()`.

## 13. Bản ghi triển khai hạng 1 (stage `ledger`) — 2026-11-10

Hạng mục 1 đã implement đầy đủ theo đúng seam đã duyệt (§7.1), không chạm `server/autoloop.py`, không đổi risk/venue/Auto, không gửi lệnh sàn.

### 13.1. Code mới

| Thành phần | Vị trí | Nội dung |
| --- | --- | --- |
| Migration | `server/migrations/0008_outcome_ledger.sql` | `outcome_positions` (UNIQUE `account_id+client_order_id`, CHECK attribution, CHECK managed⇒client_order_id NOT NULL), `outcome_fills` (UNIQUE `account_id+venue_trade_id`, `role` entry/exit), `outcome_exits` (UNIQUE theo leg+price+qty+closed_at; closed_at luôn từ fill), `outcome_funding` (UNIQUE `account_id+outcome_id+funding_at+currency`, `venue_semantics` NOT NULL), `outcome_coverage` (PK theo account+window, CHECK status vocabulary, `snapshot` JSONB), `outcome_ledger_state` (cursor restart catch-up). Additive, không backfill. |
| Pure layer | `tradingagents/outcomes/` (`attribution.py`, `currency.py`, `netting.py`, `coverage.py`) | attribution chỉ theo bằng chứng (marker + order_id, không heuristic thời gian/giá); bucket tiền tệ per-currency, `None` giữ `None`, mixed sum bị từ chối; công thức `net = realized − fee + funding(dấu venue)` với `fee_included=True` từ chối trừ fee lần hai và **không có tham số slippage nào** (trượt giá đã nằm trong giá khớp); coverage tính diff + ratio, status vocabulary giữ nguyên `unsupported`. |
| Store | `server/outcome_store.py` | ghi idempotent `ON CONFLICT DO NOTHING` theo các key của 0008, đọc summary per-currency (sổ rỗng → `unavailable`, không phải 0; `funding_unknown_currencies` cho các tiền có PnL nhưng không có dòng funding), coverage upsert, cursor get/set, scan `futures_cycles` terminal theo batch bền vững `(updated_at, cycle_id)`, đọc entry journal read-only. |
| Worker | `server/outcome_ledger.py` | worker riêng, độc lập autoloop (không import, autoloop không import worker — test pin bằng AST); ingest mặc định tắt; lần chạy đầu set cursor = "now" (không backfill, không gán hồi tố); bounded batch; venue READ history only (fills + funding income qua reader của `futures_trade_history`/`futures_funding`); unattributed chỉ vào coverage. |
| Simulation | `tradingagents/simulation/` (`engine.py`, `fills.py`, `costs.py`, `data.py`) | vòng sự kiện theo nến đã đóng; sizing chạy đúng chuỗi runtime (`validate_bracket` + `risk_limited_quantity` + cap checks của `_size` — test pin engine plan == `DerivativesExecutor._size` cho cùng input); `RiskGuard.check`, `evaluate_profit_protection`, `plan_scale_out` gọi trực tiếp, parity log ghi input/output từng lần gọi; OHLC mơ hồ → policy `worst-first`/`skip` + `ambiguity_count`; slippage một nguồn duy nhất (đổi giá khớp, fee tính trên giá đã trượt); funding thiếu → `funding_unknown`, không phải 0; report gắn nhãn `simulation` + disclaimer. Replay bản ghi: `scripts/replay_futures_decisions.py` (offline, chạy rule thật `server.futures_replay.RULES` trên fixture 551 cycle đã ghi, phần tài chính unavailable-with-reason). |
| API consumer | `server/outcomes_api.py` (`GET /api/outcomes`, §16 contract) | read-only, rỗng → unavailable, không route ghi; endpointCount 28→29 trong `docs/ui-api-contract.md`. |
| CLI consumer | `cli/main.py` lệnh `outcomes` | in summary JSON (plain print để rich không soft-wrap), thiếu DSN → exit 1 có thông báo. |
| Wiring | `server/main.py` lifespan | `outcome_ledger.start()/stop()` sau `position_monitor.start()` (pattern bracket_monitor), no-op khi tắt; router `outcomes_api`. |
| Config | `tradingagents/default_config.py` | `_ENV_OVERRIDES` + defaults: `outcome_ledger_ingest_enabled=False`, `outcome_ledger_interval_seconds=60`. |

### 13.2. Điểm ràng buộc cũ phát hiện khi implement (không sửa ngoài seam)

- `tradingagents/execution/__init__.py` import eager `bridge`/`derivatives`/`daily` → mọi `import tradingagents.execution.<submodule>` kéo ccxt + server. Vì vậy test offline-purity của simulation kiểm **import trực tiếp bằng AST** trên các file của `tradingagents/simulation/` + chạy subprocess chặn socket (cách conftest `_no_network`), không thể dùng sys.modules khi import gián tiếp vẫn xảy ra. Điều kiện có sẵn của repo, không sửa trong ask này (sửa sẽ đổi import surface công khai — cần quyết định riêng).
- `DerivativesExecutor._size` là chuỗi sizing futures thật nhưng cần executor; engine tái dùng đúng chuỗi pure bên trong (`futures_safety.validate_bracket` + `risk_limited_quantity` + cap comparisons) và test pin kết quả bằng `_size` thật trong môi trường test.

### 13.3. Xác minh thực chạy (2026-11-10)

| Lệnh | Kết quả |
| --- | --- |
| `.venv\Scripts\python.exe scripts\verify_research_delivery.py --stage ledger` | **exit 0 — 87 passed in 23.47s** (7 file, disposable PG `ta-verify-feaaea11` up→healthy→pytest→down sạch; `docker ps -a --filter name=ta-verify` rỗng sau chạy) |
| `.venv\Scripts\python.exe -m pytest -q tests/test_futures_replay.py tests/test_performance_truthfulness.py tests/test_execution_risk.py tests/test_profit_protection.py tests/test_scale_out.py tests/test_futures_entry_recovery_scale_out.py tests/test_postgres_migration_contract.py tests/test_postgres_fixture_isolation.py` | **288 passed, 2 skipped** (skip = 2 test PG của recovery; đã chạy riêng dưới PG disposable `ta-verify-regress`: 32 passed gồm cả recovery + toàn bộ PG test của ledger; cleanup xong) |
| `.venv\Scripts\python.exe -m ruff check .` | **All checks passed!** |
| `.venv\Scripts\python.exe -c "import tradingagents, cli.main"` | sạch |
| Runner mapping | ledger stage giờ gồm 7 file (thêm `tests/test_outcomes_api.py`); `--stage experiments/dashboard/shadow` giữ nguyên hành vi exit 2 theo §8 |

Sửa lỗi test trong quá trình chạy (không phải lỗi production): timestamp test hardcode ngày 2026-11-10 trong khi đồng hồ UTC máy còn 2026-11-09T19:5x — đúng dạng lỗi timezone-dependent mà AGENTS.md cấm; mọi mốc thời gian test giờ suy từ `datetime.now(UTC)`. Các cycle test dùng chung cặp nến (symbol, 4h_open, 1h_open) va UNIQUE index thứ hai của `futures_cycles` — fixture cấp cặp nến riêng từng cycle. Vấn đề đọc kết quả: hai lần chạy "xanh" trước đó là đọc nhầm vì tail cắt mất phần summary pytest — luôn đọc dòng `N passed` / exit code đầy đủ, không kết luận từ phần đuôi cleanup.

### 13.4. Khoảng trống dữ liệu nguồn (không suy thiếu=0)

1. **Fixture replay đã ghi chỉ có 2 nến quyết định (1h+4h) mỗi cycle** — không có lịch sử nến sau quyết định, không có portfolio/cash/terms thực thi đã ghi → exits/funding/PnL từ dữ liệu đã ghi là `unavailable` kèm lý do trong report (`replay_recorded_decisions`); đây là kết quả hợp lệ, không phải lỗi.
2. **Funding history**: đọc qua `fetch_funding_income` — sàn không công bố retention; đọc thất bại → funding unknown, không ghi 0.
3. **Venue fill history**: cửa sổ đọc giới hạn 7 ngày/lần đọc và "full initial page may omit older fills" (`futures_trade_history.py`) — coverage có thể `partial` và đó là kết quả trung thực.
4. **Thời điểm bắt đầu phủ**: = thời điểm `outcome_ledger_ingest_enabled` bật lần đầu (cursor khởi tạo "now"); dữ liệu trước đó không được gán và nằm trong `outcome_coverage.snapshot` là "chưa phủ".
5. **`r_multiple` trong `outcome_exits` để NULL**: cần mức initial-risk amount tại thời điểm mở vị thế, payload cycle hiện không ghi đủ dữ kiện đó — chưa đủ dữ liệu, không bịa.

### 13.5. Escalation covenant — scan split signal

Các file mới đều một-trách-niệm (attribution/currency/netting/coverage/store/worker/engine/fills/costs/data/api). `server/outcome_store.py` ~360 dòng một concern (PG persistence của ledger); `server/outcome_ledger.py` ~420 dòng một concern (worker + venue read adapter). Không file nào trong diff này thêm chức năng thứ hai; không chạm file có split signal đã đăng ký (`server/autoloop.py` — §7.3 giữ nguyên trạng thái chưa quyết định).

### 13.6. Bản ghi sửa theo review độc lập (2026-11-10) — blocker 1-3 + finding phụ

Review độc lập read-only kết luận "KHÔNG ĐẠT — 3 blocker"; xác nhận lại từng finding trước khi vá:

| Vá | Nội dung | Bằng chứng sau vá |
| --- | --- | --- |
| Blocker 1 | `entry_journal_leg` giờ nhúng `managed_live_provenance` ngay trong leg (`futures_entry_journal.py`) — mọi journal row ghi qua builder này mang marker; không caller nào còn sinh được entry managed thiếu marker | `tests/test_execution_derivatives.py` 48 passed dưới PG (đường runtime thật); `test_entry_journal_leg_always_carries_the_marker` |
| Blocker 2 | Attribution exit fills qua bằng chứng bracket task: `outcome_store.find_bracket_task(symbol, entry_order_id)` đọc task `bracket:{symbol}:{entry_order_id}` (placed_ids/leg order ids là stop/target/scale-out — `brackets._save`, `scale_out_apply`), liên kết bắt buộc same symbol + same identity hash; `attribution.bracket_exit_order_ids` + `attribute_exit_fill`; worker ghép exit evidence; `closed_at` = fill EXIT cuối (không có exit → vị thế còn mở, không tự đóng) | `test_executed_cycle_with_managed_fills_is_recorded` (realized 9.9 vào net), `test_exit_fill_without_bracket_evidence_stays_unattributed` (fail-closed), PG suite xanh |
| Blocker 3 | `fills.match_exit` xử lý GAP: nến mở qua stop → fill tại open (trigger venue bắn tại đó, không bao giờ "sống sót" qua nến giao dịch hoàn toàn dưới stop); nến mở qua target → fill tại open (thuận); gap là decisive, không ambiguity; `match_scale_out` tương tự cho tranche resting | `test_gap_through_stop_fills_at_open_not_survived`, `test_gap_through_target_fills_at_open_favorable` (lưu ý: target-extension của hàm runtime là parity thật — test pin theo effective target) |
| Fix phát sinh khi vá 3 | Engine khớp exit theo level ĐANG ĐẶT (từ close nến trước), rồi mới evaluate protection ở close để cập nhật cho nến sau — khớp theo stop vừa được nến đó nâng là look-ahead trong nến; thêm `entry_bracket_invalidated_by_gap`: gap giữa quyết định và fill làm bracket vô hiệu → bỏ plan (validate_bracket semantics), không mở vị thế stop không bảo vệ | `test_gap_stop_worse_than_level_rejects_the_better_stop_price`, `test_entry_gap_below_stop_abandons_the_plan_instead_of_crashing` |
| Finding 1 | `FundingSeries.known_from/to_ms`: cửa sổ trong span không có settlement = zero đã biết; ngoài span = unknown; report thêm `funding_partial` | `test_funding_no_settlement_inside_known_span_is_a_known_zero`, `test_funding_window_outside_known_span_is_unknown_not_zero` |
| Finding 2 | Scale-out bật → sim KHÔNG exit full remaining tại TP (runtime thay TP bằng tranche legs + trailed runner) | `test_scale_out_enabled_never_closes_full_at_the_take_profit` |
| Finding 3 | `summary(window_start, window_end)` lọc coverage rows giao nhau; bound sai định dạng → ValueError (không nuýt thành no-filter) | `test_summary_window_filter_selects_intersecting_coverage_rows`, API test |
| Finding 4 | Cycle lỗi: retry có bound (`MAX_CYCLE_ATTEMPTS=3`, cursor không tiến qua cycle lỗi khi còn lượt); quá bound → coverage row `status='error'` (account `cycle-error:<cycle_id>`) — lỗi bền vững, không bỏ cycle, không block pipeline vĩnh viễn | `test_failing_cycle_is_retried_then_published_as_error_coverage` |
| Finding 5 | Cột `realized_pnl_semantics` trong `outcome_fills`; ingestion ghi `REALIZED_PNL_EXCLUDES_FEE` ("binance: realized_pnl excludes commission"); `summary()` chỉ trừ fee khi đúng semantics đó — lệch/miếu → fee bị omit khỏi net + `fee_unknown_currencies` | `test_net_subtracts_fee_only_with_recorded_exclusion_semantics` |
| Finding 6 | Coverage rows là per-cycle snapshots chồng lấn, KHÔNG cộng được — snapshot mang cycle_id, docstring store/API ghi rõ | `test_coverage_rows_are_per_cycle_and_never_summed` |
| Finding 7 | Parity pins theo wiring runtime thật: `fee_r = round_trip_rate × entry / risk_per_unit` (futures_protection `_time_stop_funding_inputs`), funding_r chia cho quantity MỞ (không phải remaining — đã sửa engine), limits construct được đúng như ExchangeBridge mapping; cumulative_funding_r = 0.0 khi thiếu funding là fallback fail-closed của chính runtime (kèm note) | 3 test mới trong `test_futures_simulation.py` |
| Finding 8 | Funding thiếu `venue_semantics` → SKIP (không default binance); funding thiếu currency → SKIP (không default USDT); `CycleOrder.marker()` public; `_RECORDED_REJECTION_REASONS` ghi chú phạm vi (reason của rule counterfactual không bao giờ được persist) | `test_funding_without_venue_semantics_is_skipped_not_guessed` |

Xác minh sau vá (đều chạy trong phiên này): `verify --stage ledger` = **112 passed in 27.80s** (exit 0, disposable PG); PG tests derivatives journal path = 48 passed; migration integration = 2 passed; `ruff check .` All checks passed. Không commit/push; không đổi risk/venue/Auto; không gửi lệnh sàn.

### 13.7. Xác minh lại độc lập khi review được chuyển lại (2026-11-10, ask "sửa findings đã đọc")

Review độc lập (3 blocker + 8 finding) được chuyển lại nguyên văn; từng điểm được đối chiếu lại với code hiện tại và chạy demo của chính review, kết quả trong ask này:

| Kiểm chứng (chạy trong ask này) | Kết quả |
| --- | --- |
| Script focused 13 assert ("%TEMP%\\verify_fixes_ledger\\check_fixes.py", `.venv -I`): marker trong journal leg, entry/STOP(9002)/TARGET(9003) post-fix attribution, exit evidence từ chối identity hash khác, gap-down fill stop tại open, gap-up fill target tại open, in-range không đổi, funding known-zero vs unknown-ngoài-span, malformed window bound raise, `CycleOrder.marker()` public | **13/13 PASS** — "ALL FOCUSED FIX CHECKS PASSED" |
| `pytest -q tests/test_futures_simulation.py tests/test_outcome_attribution.py tests/test_outcome_currency.py tests/test_outcome_netting.py` | **73 passed in 5.54s** |
| `verify --stage ledger` (disposable PG) | **112 passed in 28.00s**, exit 0; `docker ps -a --filter name=ta-verify` rỗng sau chạy |
| Named PG tests: `test_executed_cycle_with_managed_fills_is_recorded`, `test_exit_fill_without_bracket_evidence_stays_unattributed`, `test_failing_cycle_is_retried_then_published_as_error_coverage` | 3 passed |
| Named PG tests: `test_net_subtracts_fee_only_with_recorded_exclusion_semantics`, `test_summary_window_filter_selects_intersecting_coverage_rows`, `test_coverage_rows_are_per_cycle_and_never_summed` | 3 passed |
| `pytest -q tests/test_execution_derivatives.py tests/test_execution_derivatives_override.py tests/test_futures_entry_recovery_scale_out.py` dưới PG | **50 passed in 24.89s** (đường entry/derivatives production với marker nhúng) |
| `ruff check .` / `python -c "import tradingagents, cli.main"` | All checks passed / sạch |

Đối chiếu trực tiếp 2 claim chính của review: (a) "`entry_journal_leg()` không nhúng entry_provenance" → nay nhúng tại `futures_entry_journal.py:114`, và builder này được đường production gọi (`derivatives.py:777`, `:821`); (b) "`match_exit` nến gap [90,97] stop 98 → None" → nay fill `SimulatedFill(kind='stop', price=90.0)` và gap-up target `price=105.0`.

---

## 14. Bản ghi triển khai hạng 2 (stage `experiments`) — 2026-11-10

Hạng mục 2 implement đúng seam đã duyệt (§7.1), dùng ledger/backtest/futures simulation thực của stage 1, không chạm `server/autoloop.py`, không đổi risk/venue/Auto, không gửi lệnh sàn, **không có code promotion nào**.

### 14.1. Code mới

| Thành phần | Vị trí | Nội dung |
| --- | --- | --- |
| Walk-forward | `tradingagents/experiments/walkforward.py` | split theo thời gian với purge = (horizon−1) ngày và embargo sau holdout; row rơi vào vùng purge bị DROP và đếm (`purged_rows` công bố, không giữ im lặng); `assert_no_leakage` chặn train row đóng quá biên (kể cả horizon=1 — nến 4d mở trước biên đóng sau biên bị từ chối); input out-of-order sort theo timestamp, row hỏng raise. |
| Baselines | `tradingagents/experiments/baselines.py` | no-trade (tiền mặt), rule (import THẬT `server.futures_replay.RULES` — membership check chặn rule lạ; `source.rule_fn` pin identity để chứng minh không fork), recorded (replay bot như đã quyết — reason `direction_mismatch`/`doji_candle` là recorded rejection; đề xuất chỉ khi export mang `payload.agent_proposal` đủ stop/target — export thiếu terms thì `built=0` được báo trung thực, không bịa stop/target), ablation một knob (`memory_on` tái hiện dedup thật của runtime `tradingagents/memory/log.py` — MỘT proposal mỗi symbol/ngày, off thì repeat được đề xuất; `soft_cap_enabled`/`quick_model` chỉ được nhận tên, behavior chưa wire). Knob lạ bị từ chối. Mọi variant chạy CÙNG engine/cost/fill/funding qua `run_simulation` — khác nhau chỉ ở proposal source. |
| Paired stats | `tradingagents/experiments/stats.py` | paired difference theo repeat index; bootstrap percentile 95% seeded từ caller; verdict chỉ có `insufficient_evidence_coverage` / `insufficient_evidence_interval` / `consistent_difference` — **không có khái niệm winner**; `MIN_PAIRED_REPEATS=3`. |
| Runner | `tradingagents/experiments/runner.py` | `--seed S --repeat N`: mỗi repeat một RNG fresh (`seed + i`), MỌI repeat được lưu (không chọn đẹp); LLM cost/latency per-repeat khi source khai báo `llm_usage()` (deterministic → 0 cột vẫn có); manifest khóa trước khi kết quả đầu tiên được ghi (`write_manifest` từ chối ghi đè/re-lock); metric None được báo cáo, không thay bằng 0; `assert_no_promotion_imports` AST-check toàn package — không import `server.operator_config`/`settings`/`control_store`. |
| Artifacts | `tradingagents/experiments/artifacts.py` | `experiments/<run_id>/` dưới `TRADINGAGENTS_RESULTS_DIR` (không viết vào repo); `manifest.json` + `manifest.sha256` (digest = SHA-256 canonical JSON; `verify_manifest` recompute qua pre-lock manifest), `results.jsonl` (per-repeat), `summary.json`; run_id phải là tên thư mục thuần. |
| Isolation | `tradingagents/experiments/isolation.py` | RESULTS_DIR/CACHE_DIR/MEMORY_LOG_PATH/EXEC_LOG_PATH reroute vào sandbox của run (pattern `tests/conftest.py:37-51`); `assert_sandboxed` fail-loud khi path thoát sandbox; apply/restore env chính xác. **Wired thật trong runner** (không machinery-only): engine chạy với `audit_log_path` = sandbox exec log (không phụ thuộc tempdir mặc định), env sandboxed được reroute trong thời gian chạy và khôi phục ở `finally` — test E3 pin audit lines nằm trong `sandbox/execution/audit.jsonl`. |
| Replay experiment CLI | `tradingagents/experiments/replay_experiment.py` | §4.2 lệnh 1: `python -m tradingagents.experiments.replay_experiment --rule ... --window ... --seed 7 --repeat 3`; deterministic, không LLM; so sánh rule thật trên fixture cycle đã ghi; financial part `unavailable` kèm lý do; manifest mang `git_sha` thật (rev-parse) + `fixture_sha256` (content digest — hai runs trên fixture khác nội dung phân biệt được); summary tách nhãn `cycles_with_context` (cycle có đủ snapshot để replay) khỏi tổng số cycle, kèm `recorded_baseline_note` ghi rõ recorded baseline qua engine chỉ đề xuất khi export mang entry terms. |
| Report CLI | `tradingagents/experiments/report.py` | §4.2 lệnh 3: đọc lại run_dir, verify digest; manifest lệch → error, không in summary tin được. |
| LLM variant entry | `tradingagents/experiments/run.py` | §4.2 lệnh 2: exit 2 + hướng dẫn kích hoạt — không chạy chi phí LLM im lặng; machinery dùng chung đã sẵn sàng. |
| Migration | `server/migrations/0009_experiment_registry.sql` | `experiment_runs` (PK run_id; manifest+criteria JSONB; manifest_digest; CHECK kind ∈ walkforward/replay_experiment; CHECK repeat_count ≥ 1; variants phải là JSONB array), `experiment_results` (UNIQUE run+variant+repeat_index; `metric_value` NUMERIC nullable — None ≠ 0; llm_tokens/calls/latency ≥ 0; report JSONB đầy đủ), `experiment_comparisons` (UNIQUE run+challenger+control+metric; **CHECK verdict chỉ gồm 3 giá trị insufficient/consistent — 'winner' không thể tồn tại trong schema**). Additive, không backfill. |
| Store | `server/experiment_store.py` | register_run (idempotent theo run_id; digest khác → từ chối "criteria are locked, not revised"), record_results/record_comparison (`ON CONFLICT DO NOTHING`, trả new/duplicates), list_runs/run_results/summary; registry rỗng → `unavailable` kèm lý do. |
| API | `server/experiments_api.py` | `GET /api/experiments` + `GET /api/experiments/{run_id}` read-only; verdict pass-through; disclaimer kèm theo; endpointCount 29→31 trong `docs/ui-api-contract.md` (§17 mới). |
| CLI consumer | `cli/main.py` lệnh `experiments` | in registry summary JSON (plain print); thiếu DSN → exit 1. |
| Wiring | `server/main.py` | router `experiments_api` (registry chỉ được ghi bởi runner/CLI module path; không worker mới). |

### 14.2. Không promotion

- Không module nào trong `tradingagents/experiments/` import writer của config/operator settings/risk limits — test E2 AST-check pin; migration 0009 không có cột/khái niệm promotion.
- Verdict `consistent_difference` mang lý do chữ ký "NOT a promotion or a winner claim" trong cả stats, store, API.

### 14.3. Xác minh thực chạy (2026-11-10)

| Lệnh | Kết quả |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -q tests/test_experiments_walkforward.py tests/test_experiments_runner.py tests/test_experiments_isolation.py tests/test_experiments_baselines.py` | **27 passed** (E1-E4) |
| `python -m tradingagents.experiments.replay_experiment --rule baseline --rule htf_dominance_bounded_pullback --window 2026-10-01:2026-11-01 --seed 7 --repeat 3` | chạy thật trên fixture 551 cycle đã ghi: 77 cycles replayed, baseline rate 0.3117 (24/77 confirmed), htf rate 0.3506 (27/77), verdict `consistent_difference` (+0.039, interval suy hẹp vì deterministic); financial `unavailable` kèm lý do — artifact ghi ra `%TEMP%` |
| `python -m tradingagents.experiments.report <run_dir>` | `manifest_verified: true` |
| `verify --stage experiments` (runner gate) | chạy lần đầu exit 1 do lỗi test của chính agent (fixture split, factory, env-restore) — sửa xong, kết quả cuối xem bản ghi 14.5 |

### 14.4. Điểm ràng buộc phát hiện khi implement

- `cycle_id` format `futures:<symbol>:<4h ISO>:<1h ISO>:<strategy>` chứa ':' NGAY TRONG phần ISO → parse bằng regex ISO-timestamp, không `rsplit(':', n)`.
- `baseline_confirms` cần context ĐẦY ĐỦ (1h+4h direction) — rule baseline mang theo `_rebuild_context(snapshot)` nguyên bản, không projection 1h-only.
- `Split.assert_no_leakage` horizon=1: biên purge_ms=0 vẫn phải chặn train row đóng SAU first holdout open (nến kéo dài qua biên) — bản vá đầu dùng `purge_ms > 0 and ...` đã bỏ sót case này, test E1 nến 4d bắt được.

### 14.5. Xác minh cuối cùng (lần chạy cuối của phiên này, 2026-11-10)

| Lệnh | Kết quả |
| --- | --- |
| `.venv\Scripts\python.exe scripts\verify_research_delivery.py --stage experiments` (qua harness ghi exit code) | **EXIT_CODE=0 — 39 passed in 8.66s** (6 file test; disposable PG `ta-verify-aae5ab1c` / `ta-verify-<hex>` up→pytest→down sạch; `docker ps -a` không còn container ta-verify sau chạy) |
| `.venv\Scripts\python.exe -m pytest -q tests/test_experiments_walkforward.py tests/test_experiments_runner.py tests/test_experiments_isolation.py tests/test_experiments_baselines.py tests/test_backtest.py tests/test_memory_pointintime.py tests/test_futures_simulation.py` (E5 regressions) | **88 passed in 7.38s** |
| `.venv\Scripts\python.exe -m pytest -q tests/test_postgres_migration_contract.py tests/test_futures_replay.py tests/test_performance_truthfulness.py tests/test_execution_risk.py tests/test_profit_protection.py tests/test_scale_out.py` (L8-style sanity) | **286 passed in 3.08s** |
| `python -m tradingagents.experiments.replay_experiment --rule baseline --rule htf_dominance_bounded_pullback --window 2026-10-01:2026-11-01 --seed 7 --repeat 3` | chạy thật trên fixture 551 cycle đã ghi: 77 cycles replayed, baseline rate 0.3117 (24/77), htf rate 0.3506 (27/77), `consistent_difference` (+0.039); financial `unavailable` kèm lý do; artifact ghi dưới temp (không vào repo) |
| `python -m tradingagents.experiments.report <run_dir>` | `manifest_verified: true` |
| `python -m tradingagents.experiments.run` (LLM variant entry) | exit 2 + hướng dẫn kích hoạt — không có chi phí LLM nào phát sinh |
| `.venv\Scripts\python.exe -m ruff check .` | **All checks passed!** |
| `.venv\Scripts\python.exe -c "import tradingagents, cli.main"` | sạch |
| `verify --stage dashboard` / `--stage shadow` | exit 2 giữ nguyên (chưa implement), không fake pass |

Sửa lỗi test trong quá trình chạy (không phải lỗi production): fixture split của agent dùng row kết thúc TRƯỚC biên holdout (holdout rỗng → raise đúng); `VariantSpec.factory` phải là callable trả về proposal-source (pass trực tiếp function là sai cách dùng); env-restore assert phải so với giá trị đã lưu vì conftest suite đã đặt sẵn biến; `_FORBIDDEN_IMPORTS` text-match tự dính chính nó → chuyển sang AST-import check; `write_manifest` phải từ chối ghi đè manifest đã khóa (không chỉ từ chối object đã có `criteria_locked_at`); `Split.assert_no_leakage` bỏ guard `purge_ms > 0` để horizon=1 vẫn chặn nến kéo dài qua biên (case 4d của E1).

### 14.6. Bản ghi sửa theo review độc lập hạng 2 (2026-11-10) — 2 blocker + 5 finding

Review độc lập (chỉ-read, đối chiếu code + chạy probe trên fixture thật) kết luận "ready = false" với B1/B2 chặn; từng điểm được xác nhận lại bằng probe trong phiên vá trước khi sửa:

| Vá | Nội dung | Bằng chứng sau vá |
| --- | --- | --- |
| **B1 — ablation placebo** | Nhánh `memory_on` cũ thân là `pass` — không có logic dedup nào để tắt, output bất biến theo knob. Vá: knob tái hiện dedup THẬT của runtime — `TradingMemoryLog.store_decision` (`tradingagents/memory/log.py:46-55`) giữ MỘT entry mỗi (ticker, date); `recorded_source` với `memory_on=True` (mặc định recorded) chỉ giữ proposal đầu mỗi symbol/ngày, đếm phần bị chặn vào `skipped["memory_dedup"]`; `memory_on=False` bỏ dedup. Test E4 mới pin: cùng 2 cycle cùng ngày → on `built=1` (dedup=1), off `built=2` (dedup=0); ngày kế không chặn | `pytest tests/test_experiments_baselines.py` = **9 passed**; probe trên fixture thật trước vá: `ablation=None` và `{'memory_on': False}` cho output byte-identical (`built=0, skipped={missing_terms:24, recorded_rejection:53}`) — placebo được tái lập rồi mới sửa |
| **B2 — recorded baseline bất động trên dữ liệu thật** | Fixture thật 551 rows, 77 có snapshot, **0 rows có `payload.agent_proposal`** (probe JSON trực tiếp) → `recorded_source` qua engine xây 0 proposal trên dữ liệu thật; chỉ E4 synthetic làm nó hoạt động. Vá: docstring `recorded_source` disclose giới hạn dữ liệu; replay summary thêm `recorded_baseline_note`; plan §14.1 ghi rõ; hạn chế không được giấu sau `built=0` im lặng — `built`/`skipped` luôn được công bố | Probe: `total 551, snap 77, prop 0`; `recorded_source` trên fixture thật `built=0, skipped={missing_terms:24, recorded_rejection:53}`; các disclosure đã vào docstring + summary |
| **F1 — sandbox machinery-only** | `runner.py` chỉ `prepare()`; `config_overlay`/`assert_sandboxed`/`apply_env` không được gọi ở đường chạy. Vá: runner wire thật — `assert_sandboxed` fail-loud trước khi chạy, `apply_env`/`restore_env` (`finally`) quanh toàn bộ vòng repeat, engine nhận `audit_log_path=sandbox.exec_log_path` (không còn phụ thuộc tempdir mặc định `engine.py:658-662`). Test E3 mới: variant thật sự trade → audit lines nằm trong `sandbox/execution/audit.jsonl` | `pytest tests/test_experiments_isolation.py` = **6 passed** (gồm `test_runner_writes_the_risk_audit_into_the_sandbox`) |
| **F2 — `_ms_to_ts` thiếu tz** | `server/experiment_store.py:36` naive `fromtimestamp` lệch convention repo (`server/autoloop.py` dùng `UTC`). Vá 1 dòng: `datetime.fromtimestamp(ms/1000, UTC)` | `grep datetime.fromtimestamp server/experiment_store.py` → có `UTC` |
| **F3 — provenance replay mỏng** | `git_sha: "recorded-fixture"` không phải commit thật; không content-hash fixture. Vá: manifest replay mang `git_sha` thật (rev-parse, fallback "unknown") + `fixture_sha256` (SHA-256 bytes fixture) | Manifest của run replay mới mang hai trường; `report` vẫn `manifest_verified: true` |
| **F4 — degenerate interval** | 3 repeats giống hệt → interval một điểm, không mang thông tin bất định mẫu. Vá: `stats.paired_difference` thêm note explicit vào reason khi `low == high` ("no sampling uncertainty"); docstring module disclose; test pin note chỉ xuất hiện khi degenerate | Test `test_degenerate_interval_is_disclosed_not_pretended` passed |
| **F5 — nhãn cycles gây hiểu nhầm** | `cycles_in_window: 551` vs `cycles_replayed: 77` dễ đọc thiếu context. Vá: đổi nhãn thành `cycles_with_context` (cycle có đủ snapshot để rule quyết) — trung thực hơn vì 551-474 cycle KHÔNG có snapshot, không nằm trong "window có thể replay" | Summary replay mới dùng `cycles_with_context` |

Xác minh sau vá (chạy trong phiên vá này): `verify --stage experiments` exit 0 (disposable PG, xem §14.7); E5 combo xanh; `ruff check .` sạch; replay CLI end-to-end chạy lại với manifest provenance mới.

**Giới hạn còn lại (disclose, không giấu):** `soft_cap_enabled`/`quick_model` chỉ được nhận tên knob (từ chối knob lạ) — behavior chưa wire vì recorded export không mang đủ dữ kiện để tái hiện soft-cap hay model quick; dùng chúng trong manifest hiện vẫn là no-op hành vi (được ghi trong docstring §14.1). Ablation LLM-gated (số vòng tranh luận, model) thuộc đường `run.py` exit 2 chờ kích hoạt.

---

## 15. Bản ghi triển khai hạng 4 (stage `shadow`) — 2026-11-10

Hạng mục 4 implement đúng seam đã duyệt (§7.1), không chạm `server/autoloop.py`, không đổi risk/venue/Auto, không gửi lệnh sàn. **Model Kronos chạy thật** trong môi trường cách ly ngoài repo, không thêm dependency nào vào `pyproject.toml`.

### 15.1. Code mới

| Thành phần | Vị trí | Nội dung |
| --- | --- | --- |
| Contract | `tradingagents/shadow/provider.py` | `ForecastRequest` (candles + timeframe + horizon + as_of + seed), `ForecastAttempt` (status `ok/unavailable/error/timeout`, `reason`, `paths`), `PathSummary` (end/min/max/direction + closes/highs/lows theo từng nến để đối chiếu stop-first sau này), `ForecastProvider` protocol; hằng số honesty `CALIBRATION_SAMPLE_RATIO`/`CALIBRATION_RULE` + `CALIBRATION_NOTES` nói rõ "sample ratio, không phải probability"; `STATUSES` chặn status lạ. |
| Recorder | `tradingagents/shadow/recorder.py` | `validate_candles` (chỉ nến đóng, cùng timeframe, không gap/overlap/lặp, **không nến đóng sau as_of**), `merge_candles` (union theo `(timeframe, open_time)`, bản trùng khít thì gộp, lệch giá thì từ chối), `moment_from_last_close` (thời điểm sớm nhất một quyết định trên bộ nến đó có thể tồn tại), `outcome_group_for` (executed/rejected/abstained từ status đã ghi), `build_observation` (quyết định `generation_kind` **theo thời điểm nến dự báo đầu tiên đóng**; rule không có band/dispersion/ratio). |
| Filters | `tradingagents/shadow/filters.py` | 3 bộ lọc rẻ cùng contract: `htf_direction_volume`, `donchian_breakout` (kênh loại nến mới nhất — không look-ahead), `momentum_zscore`; `FilterProvider` mang `provider_id = filter:<name>`, `calibration_label = deterministic_rule_no_distribution`; từ chối series không đọc được thay vì đoán. |
| Kronos adapter | `tradingagents/shadow/kronos.py` | `KronosProvider` chạy **out-of-process** qua interpreter cách ly; **pin nguồn + weights bằng SHA-256** (`PINNED_SOURCE_HASHES`, `PINNED_WEIGHT_HASHES`, `verify_artifacts` memoized theo (size, mtime) để không hash 115 MB mỗi request); mỗi path là **một draw `sample_count=1`** giữ riêng; `summarise_paths` cho `direction_counts`/`direction_ratio`, band p10–p90, dispersion; mọi lỗi (thiếu interpreter/weights, hash lệch, crash, non-JSON, quá deadline) → status tương ứng kèm lý do, **không raise ra caller**. |
| Child runner | `tradingagents/shadow/kronos_runner.py` | Entry point chạy bởi interpreter cách ly (`-I`), đọc JSON từ stdin, ghi JSON ra stdout; **không import `tradingagents`** — chỉ code upstream đã pin; seed từng path, trả `duration_ms`, `torch`, `device`, hash weights thực tải. |
| Evaluation | `tradingagents/shadow/evaluation.py` | `realized_candles` (chỉ nến đóng sau cutoff, đủ horizon, không gap — thiếu thì `unavailable`), `evaluate_forecast` (direction hit, band coverage **null với rule**, stop-first theo path của mô hình qua `fills.match_exit` worst-first + `ambiguity_count`, `net_r` pass-through chỉ khi ledger có). |
| Migration | `server/migrations/0010_shadow_forecasts.sql` | `shadow_forecasts` (UNIQUE `(provider, symbol, data_open_time, horizon_hours, model_revision)`, CHECK `status`, CHECK `generation_kind`, CHECK `outcome_group`, `calibration_label NOT NULL`, `per_path_sample_count = 1` CHECK, `samples`/`paths` JSONB), `shadow_evaluations` (UNIQUE `forecast_id`, CHECK status), `shadow_state` (cursor). Additive, không backfill. |
| Store | `server/shadow_store.py` | `record_forecast`/`record_evaluation` idempotent `ON CONFLICT DO NOTHING`; cursor get/set; `terminal_cycles_after` (pagination bền vững `(updated_at, cycle_id)`); `forecasts_awaiting_evaluation`; `forecast_rows` (join evaluation); `summary` → `totals` + `providers[]` **đếm** (attempts/verdicts/evaluations), rỗng → `unavailable`. |
| Worker | `server/shadow_worker.py` | Worker riêng, mặc định **tắt** (`shadow_observations_enabled=False`): đọc cycle terminal đã bền vững, lấy mốc quyết định từ `phase_timestamps` (fallback created_at/updated_at, không dùng "now"), dựng series đóng **cutoff = mốc quyết định**, gọi provider (lỗi → `error`, không raise), evaluate khi horizon đã đóng, cursor restart catch-up, `MAX_CYCLE_ATTEMPTS=2` rồi bỏ qua cycle độc; không ghi gì ngoài `shadow_*`. |
| API | `server/shadow_api.py` | `GET /api/shadow` read-only (§19 contract): `totals`, `providers`, `evaluations`, `recent`, `disclaimer`; rỗng → `unavailable`. |
| Brief | `server/operating_brief.py` | `_shadow_section` đọc thật từ store: tắt → `unavailable` + lý do + `providers`/`kronos`; bật → `totals`/`providers_detail`/`observations`; `_shadow_observation_lines` chỉ đưa identity+direction+status (không giá, không PnL). |
| Web | `web/components/overview/brief/brief-card.tsx` (`ShadowDetail`), `web/components/overview/brief/contract.ts` (`BriefShadow`), `web/lib/i18n/messages/brief.ts` | Khối shadow **trong card brief hiện có**: đếm theo nguồn, ratio chỉ in khi `distribution_kind=model_samples` và luôn kèm mẫu số, rule hiển thị note "không có phân phối", dòng retrospective được gắn nhãn, note calibration + trạng thái Kronos. |
| Replay | `scripts/replay_shadow_observations.py` | Offline thật trên export đã ghi: mốc quyết định từ timestamp hoặc cycle identity (nến mới nhất đã đóng), **chỉ dùng đuôi nến liền mạch** (`contiguous_tail` — gap là biên cứng), nhãn `retrospective` cho mọi dòng, `verdict_counts` là **đếm**, provider thiếu → `providers_unavailable` kèm lý do, limitations + disclaimer trong report. |
| Config | `tradingagents/default_config.py` | `_ENV_OVERRIDES` + defaults: `shadow_observations_enabled=False`, `shadow_observations_interval_seconds=300`, `shadow_providers="filter:donchian_breakout"`. |
| Wiring | `server/main.py` | `shadow_worker.start()/stop()` trong lifespan (pattern bracket_monitor) + router `shadow_api`; **autoloop không import gì của shadow**. |

### 15.2. Môi trường cách ly (không thêm dependency)

| Hạng mục | Giá trị thực tế đã dùng |
| --- | --- |
| Interpreter | `%LOCALAPPDATA%\tradingagents-research\kronos-venv` (Python **3.10.20**, uv-managed), ngoài repo |
| Torch | `torch==2.9.1+cpu` từ `https://download.pytorch.org/whl/cpu` (CPU-only; **không** vào `pyproject.toml`) |
| Deps khác | `pandas==2.2.2`, `einops==0.8.1`, `huggingface_hub==0.33.1`, `safetensors==0.6.2` (đúng pin của upstream) |
| Nguồn | `shiyu-coder/Kronos` @ `67b630e67f6a18c9e9be918d9b4337c960db1e9a`; `model/kronos.py` sha256 `0a5f9028…`, `model/module.py` sha256 `a07edbad…`, `model/__init__.py` sha256 `f8f856ca…` |
| Weights | `NeoQuasar/Kronos-Tokenizer-base` @ `0e0117387f39004a9016484a186a908917e22426` (`model.safetensors` sha256 `59d85f6a…`), `NeoQuasar/Kronos-small` @ `901c26c1332695a2a8f243eb2f37243a37bea320` (`model.safetensors` sha256 `b082dfcb…`) |
| Tải về | `snapshot_download` chỉ với `*.json`/`*.safetensors` — **chỉ bytes**, không chạy script tải về |
| Ghi đè | `TRADINGAGENTS_SHADOW_KRONOS_PYTHON`, `..._SOURCE`, `..._MODELS` nếu operator đặt chỗ khác |

### 15.3. Xác minh thực chạy (2026-11-10)

| Lệnh | Kết quả |
| --- | --- |
| `.venv\Scripts\python.exe scripts\verify_research_delivery.py --stage shadow` | **exit 0 — 88 passed** (suite 4 file sau bản vá review, disposable PG `ta-verify-<hex>` up→pytest→down sạch) **+ 8 passed** (model smoke thật, `-m "smoke and integration"`); stage `OK`. Lần chạy đầu của phiên này là 73 passed (trước bản vá); số hiện hành là 88 ở §15.7. `docker ps -a --filter name=ta-verify` rỗng sau chạy |
| `.venv\Scripts\python.exe -m pytest -q -m "smoke and integration" tests\test_shadow_kronos_smoke.py` | **8 passed in 13.61s** — inference thật trên fixture nến đóng deterministic: 3 path, mỗi path 4 nến, direction `down` (1 up / 2 down), band `42104.30–42194.87` từ reference `42157.61`, `torch 2.9.1+cpu` CPU |
| Probe adapter end-to-end (`.venv -I`) | status `ok`, 3 paths, `duration_ms ≈ 451`, `direction_ratio {up: 1/3, down: 2/3}`, `band`, `spread_ratio 0.00269`, system hash khớp pin |
| Probe smoke-gate (trỏ interpreter vào `C:\definitely-not-here`) | `pytest.fail` mang nguyên văn lý do ("the isolated Kronos interpreter is absent… this test does NOT pass without a real model run") — **không skip** |
| `scripts/replay_shadow_observations.py --fixture tests\fixtures\futures_cycles_replay.json --provider filter:donchian_breakout --provider filter:momentum_zscore` | chạy thật: 551 cycle, 77 có snapshot, 154 observation, **tất cả `error` kèm lý do "needs at least N closed candles, got 1"** (export chỉ có 2 nến quyết định, không liền mạch) — trung thực, không bịa verdict |
| `.venv\Scripts\python.exe -m pytest -q tests\test_shadow_forecasts.py tests\test_shadow_worker.py tests\test_shadow_isolation.py tests\test_shadow_replay.py` dưới PG | **73 passed in 20.57s** (63 shadow core + 10 replay) |
| `cd web && node node_modules\typescript\bin\tsc --noEmit` | sạch, trừ `LayoutProps` (global Next sinh sau build — đúng như ghi chú runner §5.2) |
| `cd web && node node_modules\eslint\bin\eslint.js .` | sạch |
| `.venv\Scripts\python.exe -m ruff check .` | **All checks passed!** |
| `.venv\Scripts\python.exe -c "import tradingagents, cli.main"` | sạch |
| `tests/test_operating_brief.py` dưới PG | **27 passed in 30.91s** (gồm test mới cho shadow slot off/on) |
| **Regression 4 stage** (chạy lại sau hạng 4) | `--stage ledger` **112 passed in 33.40s** · `--stage experiments` **43 passed in 10.14s** · `--stage dashboard` **PASS brief-pytest 27 / web-build / web-typecheck / web-lint / browser-offline 5** → `stage dashboard OK` · `--stage shadow` **73 + 8** → `OK` |
| `pytest -q -m integration tests\test_postgres_migrations_integration.py` | **2 passed in 8.83s** (migration 0010 áp được, danh sách kỳ vọng đã cập nhật) |
| Regression autoloop/brief dưới PG | `tests/test_server_autoloop.py tests/test_autoloop_telemetry.py tests/test_futures_cycle_telemetry.py tests/test_operating_brief.py` → **153 passed in 99.90s** |
| Regression ledger/dashboard khác dưới PG | `tests/test_futures_replay.py tests/test_performance_truthfulness.py tests/test_execution_risk.py tests/test_profit_protection.py tests/test_scale_out.py tests/test_postgres_migration_contract.py tests/test_outcomes_api.py tests/test_experiments_api.py` → **299 passed in 13.37s** |

### 15.4. Parity đã pin (emitted orders không đổi)

`tests/test_shadow_isolation.py` dựng **`DerivativesExecutor` thật** (offline `FakeDerivExchange`) và so `plan.model_dump()` + toàn bộ `exchange.calls` + danh sách `action` trong audit giữa các trạng thái shadow: `disabled`, `enabled + provider ok`, `error` (provider raise), `timeout`, `unavailable` → **byte-identical**. Thêm: đường live (`exec_live` + gates + identity verified) cũng identical có/không shadow; worker exception không thoát ra khỏi `poll_once`; `graph/agents/autoloop` không import shadow (AST); router shadow chỉ có GET; `torch`/`einops` không vào `sys.modules` của repo venv; ghi observation không đổi risk limits/Auto.

### 15.5. Giới hạn còn lại (disclose, không giấu)

1. **Chưa có bằng chứng lợi nhuận và chưa có dữ liệu live mới.** Mọi con số trong bản ghi này là hạ tầng + một lần inference thật trên fixture tổng hợp; chưa có run forward nào trên thị trường. Kronos pretrain cutoff vẫn chưa xác minh được cho crypto (research §5D) — kết luận cần forward test sau khi khóa revision.
2. **Export đã ghi không đủ nến để quan sát.** Đo lại chính xác: mỗi snapshot chỉ mang **1** nến mỗi timeframe (cặp 1h+4h của đúng cây nến quyết định), nên sau khi lọc theo timeframe chỉ còn **1 bar** — lý do thật là **thiếu SỐ nến**, không phải "không liền mạch" như bản ghi trước viết. Cách ghi cũ còn che mất blocker F1 (worker không lọc timeframe nên mọi cycle thật bị từ chối) vì replay tự lọc timeframe trước khi validate còn worker thì không. Muốn có verdict thật cần export nến lịch sử (`--candles`) hoặc bật worker với venue READ.
3. **Worker cần venue READ để có history**: `_venue_candle_source` dùng `DerivativesExecutor._bridge` (đường đọc market data có sẵn). Chưa chạy thật vì ask cấm chạm runtime; test dùng nguồn nến inject.
4. **`stop_first_rate` là hypothetical trên path của mô hình** (dùng stop/target đã ghi của cycle), không phải kết quả giao dịch; mẫu số là **các path thực sự chạm một mức** (`stop_first_measured_paths` công bố), path không chạm mức nào bị loại thay vì bị tính là "target first"; `net_r` chỉ pass-through khi ledger join, worker mặc định không join nên ghi lý do.
5. **Evaluation 24h cần đủ 24 nến 1h liền mạch sau cutoff** — thiếu thì `unavailable`, không chấm một phần. `band_coverage` hỏi **close cuối kỳ** có nằm trong band p10–p90 của end-close (đúng thứ band mô tả); câu hỏi "cả đường đi có nằm trong band không" được trả lời riêng ở `realized.band_held_throughout`, vì band end-point không phải hành lang cho từng nến.
6. **`--full` giờ phụ thuộc môi trường cách ly**: thiếu interpreter/weights là FAIL (không skip) theo yêu cầu ask. Trên máy khác chưa cài, `--full` sẽ đỏ ở `shadow-smoke` — đúng thiết kế, không phải hồi quy.
7. **`web/next.config.ts` và script `typecheck` không bị chạm** trong stage này (đã có từ stage 3); `tsc --noEmit` chạy trực tiếp qua node như runner làm.
8. **Horizon cố định 24h cho mọi cycle** (`OBSERVATION_HORIZON_HOURS`), trong khi holding period của lệnh thật thay đổi theo time-stop/bracket. Direction/band vì thế được chấm trên một cửa sổ chung chứ không khớp kỳ giữ của từng lệnh; chỉ `stop_first` được disclose là hypothetical. Cần quyết định: horizon theo cấu hình time-stop hay giữ 24h làm chuẩn so sánh.

### 15.7. Bản ghi sửa theo review độc lập (2026-11-10) — 1 blocker + 3 finding metric/seed + 5 minor

Review kết luận "KHÔNG ĐẠT"; từng finding được tái lập bằng probe trước khi sửa.

| Vá | Nội dung | Bằng chứng sau vá |
| --- | --- | --- |
| **F1 (blocker) — worker không tạo được observation từ cycle thật** | `_snapshot_candles` trả cả 1h và 4h; `validate_candles` chỉ nhận một timeframe → mọi snapshot production bị từ chối `mixed timeframes`. Vá tại seam `_series_for`: lọc `_candles_at_timeframe` **trước** `merge_candles`; recorder vẫn strict (test riêng giữ điều đó). | Probe trước vá: `{one_hour, four_hour}` → `REFUSED mixed timeframes: expected 1h, got 4h`; export thật **77/77 snapshot mang cả hai**. Probe e2e trên PG disposable (cycle đúng shape autoloop + `poll_once` thật) sau vá: `{'provider': 'filter:donchian_breakout', 'status': 'ok', 'stored': true}`, 1 row, `generation_kind=forward`. Test mới: `test_series_narrows_a_two_timeframe_snapshot_to_the_observed_one`, `test_recorder_still_refuses_a_mixed_series_it_is_handed_directly`, và `test_observation_is_recorded_for_a_terminal_cycle` dùng snapshot 1h+4h. |
| **F2 — band_coverage so từng nến với band end-point** | Band là p10–p90 của **close cuối kỳ**; so từng nến làm coverage gần như luôn `False` (band Kronos thật ±0.1% quanh reference không thể chứa 24 close). Vá: `band_coverage` = close cuối kỳ nằm trong band; câu hỏi đường đi tách sang `realized.band_held_throughout` + `band_semantics` nói rõ. | Probe trước vá: band [100,101], bar1 120, close cuối 100.5 → `False`; sau vá → `True`, `band_held_throughout=False`. Test: `test_band_coverage_asks_about_the_endpoint_not_every_bar`. |
| **F3 — stop_first tính cả path không chạm mức nào** | Path không chạm stop lẫn target bị tính vào mẫu số như "target-first". Vá: chỉ path chạm một mức vào mẫu số; `stop_first_measured_paths` công bố; không path nào chạm → `None`. | Probe trước vá: 1 path không chạm + 1 path chạm stop → `0.5`; sau vá → `1.0` với `measured_paths=1`. Probe F3b: cả hai không chạm → `None` (trước vá `0.0`). Test: `test_stop_first_denominator_excludes_paths_that_touch_neither_level`, `test_stop_first_is_unknown_when_no_path_resolved_the_question`. |
| **F4 — seed replay đổi giữa process** | `abs(hash((cycle_id, provider_id)))` bị salt theo process. Vá: `stable_seed()` (SHA-256) đặt trong `tradingagents.shadow.provider`, worker và replay dùng chung. | Probe hai process: trước vá `158156885` vs `991776861`; sau vá cùng một giá trị. Test: `test_replay_seed_is_stable_across_processes` (chạy 2 subprocess thật), `test_replay_uses_the_worker_seed_derivation`. |
| **F5 — `?limit=0|-1` → 500** | Vá: `_checked_limit` ở API trả 400 `validation_error` (kể cả biên trên `MAX_LIMIT=200`), store cũng từ chối `limit <= 0`. | Probe trước vá qua handler: `ValueError` → 500 `internal_error`. Sau vá: `ApiError(400, validation_error)`; test `test_shadow_api_refuses_a_bad_page_size_as_a_client_error` + `test_store_refuses_a_non_positive_limit`. |
| **F6 — `CALIBRATION_NOTES[label]` KeyError** | Provider là plugin, label lạ không được làm nổ row building. Vá: `calibration_note()` trả note giải thích thay vì raise; row vẫn giữ label gốc. | Test: `test_unknown_calibration_label_gets_a_note_instead_of_a_key_error`. |
| **F7 — "provider lỗi vô hại" chỉ đúng khi có PG** | `shadow_store.record_forecast` nằm trong cùng chuỗi không bọc. Vá: `_observe_cycle(record=...)` + `poll_once(record=...)` injectable; ghi thất bại vẫn raise (mất observation là lỗi thật), provider lỗi thì không. | Test không cần PG: `test_provider_failure_is_data_even_when_persistence_is_injected`, `test_a_raising_recorder_surfaces_but_a_provider_does_not`; test isolation cũ siết từ "một trong ba status" thành đúng `polled`. |
| **F8 — `min_close`/`max_close` thực chất là min(lows)/max(highs)** | Vá: đổi tên thành `path_low`/`path_high` + docstring nói rõ khác biệt với min/max close. | Test: `test_path_summary_names_its_extremes_honestly`; `as_json()` không còn `min_close`/`max_close`. |
| **F9 — `stop()` join `interval + 5` = 300s** | Vá: `SHUTDOWN_GRACE_SECONDS = 10`, quá hạn thì log warning và để thread daemon chết theo process thay vì treo shutdown. | Test: `test_shutdown_waits_a_short_bounded_grace_not_the_poll_interval` (pin `join(10.0)`, không phải 300). |
| **Bound chưa test** | `MAX_PATHS`, `MAX_LOOKBACK_BARS`, `MIN/DEFAULT_LOOKBACK_BARS`, `MAX_HORIZON_BARS` được pin và kiểm biên thật. | Test: `test_model_bounds_are_pinned` (hằng số + horizon vượt biên bị từ chối). |

**Sửa lỗi test do chính bản vá gây ra (không phải lỗi production):** fixture `_candles()` kết thúc đúng đầu giờ hiện tại, nên mốc quyết định `now - 1 phút` nằm *sau* nến cuối mỗi khi phút hiện tại < 1 — đúng dạng lỗi phụ thuộc đồng hồ mà AGENTS.md cấm. Vá: nến kết thúc sớm hơn một bar và mốc forward/retrospective suy từ chính nến (`_moment_after_last_close`), không từ đồng hồ.

Xác minh sau vá (chạy trong phiên này): `verify --stage shadow` **88 passed in 38.12s** (suite) + **8 passed in 14.86s** (model smoke thật) → `stage shadow OK`; `--stage dashboard` **PASS** (brief 27 / build / typecheck / lint / browser 5); `--stage ledger` **112 passed**; `--stage experiments` **43 passed**; `pytest -m integration tests\test_postgres_migrations_integration.py` **2 passed**; `ruff check .` All checks passed; `import tradingagents, cli.main` sạch; `docker ps -a --filter name=ta-verify` rỗng sau chạy.

### 15.6. Escalation covenant — scan split signal

Các file mới đều một-trách-nhiệm: `provider.py` (contract), `filters.py` (baseline), `kronos.py` (adapter+pin), `kronos_runner.py` (child entry), `recorder.py` (series+row), `evaluation.py` (scoring), `shadow_store.py` (PG), `shadow_worker.py` (worker), `shadow_api.py` (route), `replay_shadow_observations.py` (CLI replay). `kronos.py` ~370 dòng một concern (optional model adapter); `shadow_worker.py` ~430 dòng một concern (worker + venue read adapter). Không chạm `server/autoloop.py` (§7.3 giữ nguyên "chưa quyết định"). `web/components/overview/brief/brief-card.tsx` đã dài trước đó; `ShadowDetail` được thêm như một hàm trong cùng file vì nó thuộc đúng khối `activity` của card — nêu tại đây để coordinator quyết định có tách `brief/shadow-block.tsx` ở pass sau hay không.
