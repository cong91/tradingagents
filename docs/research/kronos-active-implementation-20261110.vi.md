---
purpose: Implementation report — Kronos active context in the futures decision path (context_only)
branch: feat/trading-research-delivery-20261110
base_commit: 0943103
date: 2026-11-10
updated: 2026-11-11 (sửa lỗi theo kronos-active-review-20261111.vi.md — xem kronos-active-repairs-20261111.vi.md; UI/nghiệm thu hoàn tất — xem §0c và kronos-active-ux-20261111.vi.md)
status: implemented locally; NOT deployed, Auto untouched, no order sent
research: docs/research/kronos-trading-integration-20261110.vi.md
repairs: docs/research/kronos-active-repairs-20261111.vi.md
review: docs/research/kronos-active-review-20261111.vi.md
---

# Kronos đã vào đường quyết định thật — báo cáo triển khai

Tài liệu này mô tả trạng thái code SAU khi triển khai VÀ SAU vòng sửa lỗi ngày
2026-11-11: dự báo Kronos được đưa vào ngữ cảnh futures **trước khi** Trader và
Portfolio Manager quyết định, qua một forecast core trung lập mà cả shadow lẫn
đường quyết định cùng dùng. Mọi phát hiện kèm `file:line` hoặc lệnh đã chạy thật
trong phiên này. Không deploy, không bật Auto, không đổi venue/risk, không đặt
lệnh, không commit/push. Các sửa đổi 11-11 ghi trong
`kronos-active-repairs-20261111.vi.md` với bằng chứng test cụ thể.

---

## 0. TL;DR

1. **Đã có `tradingagents/forecast/`** — core trung lập (contract, artifact, sampler,
   service). Shadow re-export lại contract (`shadow/provider.py`) và là consumer thật;
   test pin isolation (`tests/test_shadow_isolation.py`) vẫn xanh — graph/agents/autoloop
   **không** import `shadow` (autoloop import `server.kronos_context`, module này import
   `tradingagents.forecast`).
2. **Đường active**: `server/autoloop.py` gọi `server/kronos_context.py` ngay sau khi
   render `futures_decision_context`, **trước** `build_graph` — block dự báo nén được
   nối vào cùng chuỗi `execution_context` mà Trader (`trader.py:51`) và PM
   (`portfolio_manager.py:54`) nhúng vào prompt thật.
3. **Chỉ `mode=context_only`.** Gate hướng 0.70 bị loại bằng đo (2/10 ổn định ở T=1.0,
   3/10 ở T=0.5 — dưới bar merge 8/10); chọn `filter` trong config bị từ chối kèm con
   số đo (`forecast/service.py:FILTER_MODE_REFUSAL`). Không ứng viên nào bị cắt.
4. **Bốn bug thật bị smoke thật bắt** (lý do smoke không được giả lập): (a) payload
   gửi dataclass `Candle` thẳng vào JSON; (b) persistent child không flush stdout,
   đọc bằng `json.load` (chặn tới EOF) và bỏ quên request đầu sau khi load model
   (deadlock request đầu tiên); (c) parent đọc `process.returncode != 0` — với child
   còn sống `None != 0` là True, mọi câu trả lời thành công bị đọc thành crash; (d)
   model nguồn ghim in banner "Loading weights from local directory" ra **stdout** —
   parent đọc dòng đầu sẽ parse banner thay vì kết quả (code cũ spawn-per-call đọc
   dòng CUỐI nên không thấy). Cả bốn đã sửa; handshake được chứng minh bằng stub
   interpreter rồi bằng model thật.
5. **BL-9 sửa xong, đã đo lại**: `--stage experiments` từ `1 failed, 58 passed`
   (KeyError `metric_name`) → **59 passed**; thêm 1 regression test đọc artifact CLI
   thật (`tests/test_financial_experiment_cli.py::test_registered_result_rows_carry_...`).
   Sửa kèm: `record_comparison` cũng KeyError `challenger` trên cùng đường — đã gắn
   cặp challenger/control cho cả hai nhánh trong `_summarize`.
6. Mặc định **off**. `kronos_active_enabled` là managed operator switch (confirm +
   reason + audit qua `/api/operator-config`); 9 key tuning là engine settings qua
   `PUT /api/settings` + `_ENV_OVERRIDES`, giờ có kiểm domain tại biên API
   (từ chối per-field trước khi áp dụng/persist).

## 0b. Cập nhật sau vòng sửa 2026-11-11 (thay các nhận định cũ không còn đúng)

1. **Lifecycle ownership là scoped per call, không phải warm reuse.**
   `forecast_for_cycle` dựng service cho một ứng viên và đóng trong `finally`
   (success/error/timeout); KHÔNG có child model sống qua ứng viên và KHÔNG có
   warm reuse tuyên bố. Chi phí: mỗi ứng viên trả cold start (torch import +
   weight load ~2.4–2.7 s trước inference); số đo trên model thật nằm ở
   `kronos-active-repairs-20261111.vi.md`. `KronosSampler.close` đã sửa bug
   "xoá `_process` trước khi kill" (child sống sót sau close) — test pin bằng
   fake Popen và Popen thật.
2. **Preflight artifact thật trước spawn.** `from_config` gọi
   `verify_artifacts` (memoize, cùng hàm với shadow); thiếu file/sai hash →
   `unavailable` TRƯỚC khi child import/load. `artifact_hash` trên block là
   digest của hash ĐÃ KIỂM (`artifact_digest_from(verified)`); khi chưa kiểm,
   giá trị là None — hash kỳ vọng không bị gắn nhãn là đã kiểm. Shadow
   KronosProvider cũng ghi hash đã kiểm thay vì hash kỳ vọng.
3. **Đầu vào được nhận diện, không được giả định.** `series_problem` từ chối:
   `as_of` naive, nến sai symbol, series stale (bar đóng mới nhất tại biên
   quyết định không có trong series), giá OHLC không finite/<=0, OHLC sai thứ
   bậc, volume âm/NaN, gap, nến đang chạy, thiếu lịch sử.
4. **Cache identity đầy đủ**: nội dung nến (`content_digest`), venue
   (`exec_exchange_id`), cutoff (`as_of`), mọi setting ảnh hưởng phân loại
   (band-spread bound), model revision, artifact hash, seed. Quyết định
   historical (bar horizon đầu đã đóng khi service chạy) BYPASS cache hoàn toàn
   và block tạo ra bị gắn `retrospective` — created_at trước bar outcome đóng
   KHÔNG phải bằng chứng dự báo tồn tại tại mốc quyết định. `retrospective`
   không bao giờ được đưa vào quyết định active.
5. **Kết quả được kiểm trước khi gắn `status=ok`**: đúng số path yêu cầu (yêu
   cầu 16 path, nhận 1 path là lỗi, không phải sample nhỏ), đủ chiều horizon
   từng path, giá finite dương, high>=low, symbol khớp, seed per-path phân
   biệt (sampler); service kiểm lại tại seam. 16 path là sampling budget,
   KHÔNG phải bằng chứng kỹ năng dự báo hay hiệu chỉnh xác suất.
6. **`failure_policy` có tác dụng thực**: `defer` dừng ứng viên TRƯỚC
   graph/LLM, persist lý do (status rejected, phase `forecast` trên cycle đã
   claim); `pass_through` tiếp tục với lỗi ghi rõ trong context (không phải
   pass âm thầm). Giá trị `context_only` của failure_policy đã bị xoá vì không
   có hành vi phân biệt.
7. **Deadline/cancellation có răng**: trước/sau span `kronos_active_context`
   autoloop kiểm `_raise_if_execution_closed` (deadline + cancelled event);
   bên trong, phần thời gian cycle còn lại giới hạn chunk deadline của sampler
   (hết budget → `unavailable`, không khởi động model); không nắm execution/
   protection lock; persist sau forecast bị chặn khi cycle đã abandoned.
8. **Tuning validate tại biên API**: `PUT /api/settings` từ chối 422 kèm lỗi
   theo field (mode/timeframe/horizon/paths/temperature/lookback/batch/
   failure_policy/band spread, kể cả NaN/Infinity) TRƯỚC khi `set_config`
   hay persist — không mutation một phần. Env-override sai vẫn bị từ chối ở
   lớp settings runtime (surfaced `unavailable`).

---

## 0c. Cập nhật pass UI/nghiệm thu 2026-11-11 (đóng mục "UI riêng" của §4)

Pass sau vòng review `kronos-active-review-20261111.vi.md` đã hoàn tất phần UI
mà cả báo cáo này và báo cáo dashboard để ngỏ (báo cáo cũ giữ nguyên làm lịch
sử; bằng chứng hiện hành):

1. **Settings**: nhóm **"Kronos — dự báo ngữ cảnh"** trong khu Mô hình AI,
   tách khỏi Auto Spot/Futures (badge "công tắc ngữ cảnh" riêng, không phải
   "chế độ"); panel trạng thái từ `GET /api/operator-config` → block `kronos`
   mới (enabled/mode/timeframe/horizon/paths/temperature/lookback/batch/
   failure_policy/band + `settings_problem` + readiness từ `verify_artifacts`
   memoized + model ghim) — server không tự chạy model. Confirm + reason +
   audit giữ nguyên backend; draft/undo/uncertain-save/server-refresh tái dùng
   controller hiện có; 9 key tuning vào nhóm nâng cao trong khu AI, i18n vi/en.
   Luồng confirm/reason/save/undo trên trình duyệt thật vẫn xanh
   (`test_operator_settings_ui_browser.py` 3 passed; settings UI/API tổng
   **117 passed** với PG). Không secret nào hiển thị; không control nào arm Auto.
2. **Dashboard/cycle**: block **"Kronos forecast (latest recorded cycle)"**
   tách khỏi shadow rows — `server/operating_brief.py::_active_forecast_section`
   đọc cycle đã persist gần nhất có `kronos_forecast`/`kronos_policy`, phân
   biệt off/empty/error/stale/unavailable/ok, kèm model/cutoff/generated/
   recorded/horizon/path count/tỉ lệ mẫu kèm mẫu số/band kèm đơn vị USDT/wall
   latency; block lỗi không số; **không** hiện tiết kiệm LLM call cho
   context_only, **không** gán PnL cho Kronos.
3. **Nghiệm thu trình duyệt**: fixture build production thật (`next build
   --webpack` + `next start`) thay cho `next dev` trước đây; mọi CDP command/
   websocket/boot có timeout riêng, socket/timer đóng trong finally, driver +
   runner tự reap đúng process-tree con của mình kể cả khi assertion fail.
   Suite **11 passed in 349.21s** (gồm `forecast_off`/`forecast_stale`).
4. **Screenshot + W1–W6 đo thật** — `kronos-active-ux-20261111.vi.md` +
   `.zcode/artifacts/trading-delivery-20261111/` (ảnh là FIXTURE, không phải
   runtime; W1: 0 target <44px; W2: 4.73:1 light khớp đo độc lập cũ, 7.63:1
   dark; W3: h1=1/h2=6/không skip; W5: không overflow ngang; W6: null ≠ 0).
5. **Test render vị thế chuyển về chủ sở hữu mới** (PortfolioCard) — giữ toàn
   bộ kiểm chứng precision/quantity âm/observed_at/incomplete/unknown currency/
   stale; **29 passed**; thêm test pin dedup performance card không nhân bản
   bảng vị thế.
6. **Runner** dashboard stage gộp test render + in summary số test thật; Gate
   dashboard đủ exit codes, full output, không nuýt failure trong warning tail.

Bằng chứng đầy đủ từng lệnh: `kronos-active-ux-20261111.vi.md` §8. Hạng mục
"UI riêng" ở §4 dưới đây **đã đóng**; hai dòng còn lại của §4 (screen reader
thật/Safari/Firefox và render với PG thật) vẫn chưa có bằng chứng — ghi trung
thực trong `kronos-active-ux-20261111.vi.md` §7.

---

## 1. File mới / đã đổi

### 1.1 Core trung lập — `tradingagents/forecast/`

| File | Nội dung | Nguồn |
|---|---|---|
| `contract.py` | `ForecastRequest`, `PathSummary`, `ForecastAttempt`, `ForecastProvider`, `direction_of`, `stable_seed`, các hằng `STATUS_*`/`CALIBRATION_*`/`DISTRIBUTION_*`/`GENERATION_*`/`OUTCOME_*` | move từ `shadow/provider.py` (không chép); `shadow/provider.py` giờ re-export |
| `artifact.py` | Pin hash 5 file, `verify_artifacts` memoize, `isolated_python/source/model_dir`, bounds (`MIN_PATHS=16`, `MAX_PATHS=32`, `MAX_HORIZON_BARS=72`, `MIN_LOOKBACK_BARS=32`), `artifact_digest_from(verified)` — digest của hash đã kiểm | move từ `shadow/kronos.py`; `MIN_PATHS` là mới (§3 nghiên cứu) |
| `sampler.py` | `KronosSampler`: persistent child JSON-lines, batch theo row budget (`plan_batches`, mặc định 80 rows, trần `MAX_BATCH_ROWS=960` — cấu hình đo 6.07 GiB), deadline wall mỗi chunk, kill/respawn, **drain stderr**, khớp câu trả lời theo `request_id` (banner/stale bị bỏ qua có bến; EOF/limit → kill fail-closed), `validate_settings` chặn `paths<16` và `max_batch_rows>960` lúc dựng, đóng child đúng quy trình (bắt pid trước, EOF stdin → wait → kill → reap → đóng pipe) | mới, theo §5.3 nghiên cứu; sửa theo review 11-11 |
| `kronos_runner.py` | Child: load model MỘT lần, loop `readline` → trả lời mỗi request một dòng, `flush=True` bắt buộc, **echo `request_id`** cả nhánh lỗi; seed riêng **từng path** (`seed + index`, chống batch-dependence BL-10); `duration_ms` là wall gồm load, load tính vào request đầu tiên | move + sửa từ `shadow/kronos_runner.py` |
| `service.py` | `KronosForecastService.forecast(series, seed) -> ForecastBlock`; validate series (symbol, nến đóng, một khung, không gap, cutoff, stale, OHLC finite dương + thứ bậc, volume); preflight artifact trước spawn; `cache_key` gồm symbol/venue/timeframe/`data_open_time`/`cutoff_ms`/`content_digest`/params(temperature,paths,horizon,lookback,band bound,...)/model_revision/artifact_hash/seed; quyết định historical bypass cache và gắn `retrospective`; kiểm kết quả trước `status=ok` (đúng số path, đủ horizon, finite dương, symbol khớp); `render_forecast_block` in nhãn "NOT a probability" + paths cạnh ratio + cutoff/band + `generated_at`; `settings_from_config` + `tuning_problems` (biên API); `settings_problem` bounds (timeframe/horizon/lookback/batch/band/failure policy) | mới, theo §4.3/§5.5/§5.7; sửa theo review 11-11 |
| `__init__.py` | Re-export bề mặt công khai | mới |

### 1.2 Đường quyết định

- `server/kronos_context.py` (mới): `forecast_for_cycle` (đọc lịch sử đóng qua cùng
  `fetch_closed_candles`, cutoff tại biên nến 4h của cycle; **scoped ownership** —
  service dựng cho call được đóng trong `finally` trên success/error/timeout;
  **budget** — phần thời gian cycle còn lại giới hạn chunk deadline của sampler,
  hết budget → `unavailable` không khởi động model; `cancelled` event được kiểm
  trước khi làm việc), `append_to_context` (None = không đổi chuỗi, kể cả khi
  rỗng), `policy_for` (`use`/`defer`/`pass` — không cắt ứng viên theo hướng), và
  chặn block `retrospective` ra khỏi quyết định active. Module tách khỏi autoloop
  vì AGENTS (one concern); autoloop chỉ gọi.
- `server/autoloop.py`: trong `_process_candidate`, sau `render_futures_decision_context`:
  kiểm mode + `_raise_if_execution_closed` (deadline + cancelled) **trước**, chạy
  forecast trong span `kronos_active_context`, kiểm lại **sau**, ghi
  `kronos_forecast` (JSON) + `kronos_policy` vào `futures_cycle_payload`, persist
  phase `kronos_finished_at` (gated `_cycle_writes_allowed`); rồi nếu policy là
  `defer` — persist terminal `status=rejected, phase=forecast` kèm lý do và
  **return trước khi build graph/LLM**. `pass_through` tiếp tục với lỗi ghi rõ
  trong context. Helper `_kronos_block_for` trả `None` khi off (không đọc gì);
  exception forecast → block `error`, candidate không chết. Không `execution_lock`
  qua đường này (signature được test pin).
- Prompt thật: `tradingagents/agents/trader/trader.py:50-53` và
  `tradingagents/agents/managers/portfolio_manager.py:53-56` nhúng
  `execution_context` nguyên văn → block dự báo nằm trong cả hai prompt, **trước**
  khi agent trả lời (test chấm text LLM nhận được).

### 1.3 Config / settings

- `tradingagents/default_config.py`: 10 key `kronos_active_*` (default off, mode
  context_only, 4h/24h/16 paths/T=0.5/lookback 96/batch 80/defer/band 0.15).
  `kronos_active_enabled` **không** nằm trong `_ENV_OVERRIDES` (managed switch);
  9 key tuning có env override.
- `server/operator_config.py`: `kronos_active_enabled` trong `_ALLOWED`, group
  `forecast`, `gate: True` — nghĩa là cần `confirm: true` + `reason` và được ghi
  audit; **không** nằm trong `auto_modes.MODE_KEYS` nên không thể tự bật lệnh.
  (Test bắt được lỗi này: lần đầu để `gate: False`, một PUT không confirm đã 200.)
- `server/settings.py`: không đổi code — 9 key tuning tự động vào `GET/PUT
  /api/settings` qua `_ENV_OVERRIDES` + coercion theo type default.

### 1.4 Shadow giữ nguyên hành vi

- `shadow/kronos.py` thành adapter mỏng: pin + bounds từ `forecast.artifact`,
  inference qua `KronosSampler` (spawn-per-call như cũ, deadline per call),
  aggregate `summarise_paths` giữ nguyên shape (`sample_count`, `band`,
  `dispersion`) — `shadow_store`/`shadow_worker` không đổi.
- `shadow/kronos_runner.py` đã chuyển đi (module cũ xoá, không wrapper).
- Suites: `tests/test_shadow_forecasts.py`, `test_shadow_worker.py`,
  `test_shadow_isolation.py`, `test_shadow_replay.py` — **88 passed** sau khi move.

---

## 2. Bằng chứng kiểm thử (đã chạy trong phiên này)

| Lệnh | Kết quả |
|---|---|
| `.venv\Scripts\python.exe scripts\verify_research_delivery.py --stage experiments` | **59 passed** (BL-9 trước đây 1 failed) |
| `.venv\Scripts\python.exe scripts\verify_research_delivery.py --stage kronos` | **stage kronos OK**: suite 75 passed với PG dùng một lần (`reject_skips`) + smoke model thật 4 passed trong 19.5 s |
| `pytest -q tests/test_forecast_service.py tests/test_kronos_decision_integration.py tests/test_kronos_decision_settings.py tests/test_shadow_*.py` (có DSN) | **127 passed** (shadow 88 + Kronos 39 cùng lúc) |
| `pytest -q tests/test_experiments_registry.py tests/test_financial_experiment_cli.py tests/test_experiments_runner.py` | **31 passed** |
| `pytest -q tests/test_financial_experiment_cli.py` (có DSN) | **17 passed** (gồm regression test mới đọc artifact CLI thật + `run_results` từ PG) |
| Smoke model thật trực tiếp `pytest -q tests/test_kronos_active_smoke.py -m "smoke and integration"` | **4 passed in 15.6 s** — block `status=ok`, 16 paths, `generation_kind=forward`, band p10≤p50≤p90, T=0.5, render có "NOT a probability" |
| `.venv\Scripts\python.exe -m ruff check .` | **All checks passed** |
| `git diff --check` | clean (sau khi bỏ 1 dòng trống EOF thừa trong `docs/ui-api-contract.md` — file đã sửa sẵn trong nhánh) |

Lưu ý trung thực: các số liệu trong bảng trên là của lần triển khai 11-10. Sau
vòng sửa 11-11, bảng bằng chứng hiện hành nằm ở
`kronos-active-repairs-20261111.vi.md` §5 — tóm lại: `--stage kronos` lần 2 chạy
**stage kronos OK** (kronos-suite `137 passed in 73.24s` với PG dùng một lần +
`reject_skips`, kronos-model-smoke `4 passed in 20.80s` trên model thật).
Suite test của core đã tách theo concern (`test_forecast_inputs/cache/sampler/
context`) thay vì một file gom; file cũ `test_forecast_service.py` đã xoá.

---

## 3. Quyết định thiết kế và lý do

1. **Tách module mà không đụng block ai-note.** Autoloop giữ nguyên block
   discovery/refill; phần forecast mới nằm ở `server/kronos_context.py` +
   `tradingagents/forecast/`. Không extract `candidate_policy` (seam đã duyệt để
   dành cho task riêng), không broad refactor. Prefilter/cắt ứng viên **không**
   được thêm vào `_select_cycle_candidates` — vì mode là context_only nên không có
   điểm chèn ở đó.
2. **`gate: True` cho `kronos_active_enabled`** dù nó không arm lệnh: cơ chế
   confirm/reason/audit của operator_config gắn với `_GATE_KEYS`; sync mode chỉ chạy
   với `auto_modes.MODE_KEYS` (kronos không có ở đó). Test pin cả hai vế.
3. **Cache in-memory, không persist**: entry chỉ có giá trị với đúng nến đóng đã tạo
   nó; key gồm `data_open_time` + `cutoff_ms` + `content_digest` (nội dung OHLCV) +
   venue + mọi setting ảnh hưởng phân loại + revision/hash/seed, nên entry tự chết
   khi bar mới đóng, khi nội dung nến bị sửa, hoặc khi setting đổi; giá trị persist
   nhỏ hơn rủi ro phục vụ stale. `retrospective` không được lưu và không được phục vụ;
   quyết định historical (bar horizon đầu đã đóng) bypass cache hoàn toàn —
   created_at trước bar outcome đóng không phải bằng chứng dự báo tồn tại tại mốc
   quyết định (chống look-ahead replay).
4. **Seed theo (cycle_id, "kronos_active")** qua `stable_seed` — cùng identity → cùng
   paths giữa hai process; seed per-path trong child chống batch-dependence (BL-10).
5. **`duration_ms` wall-clock** (BL-4): child đo từ dòng đầu `main`, load tính vào
   request đầu; `forecast_for_cycle` đè bằng wall đo tại call-site (cache hit → ~0).
6. **Không đổi**: RiskGuard, confirmation, bracket, authorization, order lifecycle,
   Auto modes, venue, risk limits. PM vẫn là người quyết cuối; block tự tuyên bố
   "not an instruction" trong chính text gửi prompt.

---

## 4. Giới hạn còn lại

- **Chưa có bằng chứng lợi nhuận** — context_only là đầu vào dữ liệu; chưa từng có
  PnL nào gắn cho Kronos (shadow cũng `net_r=None`). Sample ratio không phải xác
  suất; 16 paths là sampling budget, không phải bằng chứng kỹ năng dự báo.
- **Model không công bố training cutoff** (BL-1) → `generation_kind=forward` là
  timestamp claim (draw trước khi bar horizon đầu đóng), không phải bằng chứng
  tồn tại tại mốc quyết định hay OOS/alpha; output retrospective là research,
  bị chặn khỏi quyết định active.
- **Chi phí là cộng thêm và đã đo lại với ownership scoped (11-11)**: mỗi ứng
  viên trả cold start riêng — đo thật **5.6–7.1 s wall/candidate** (96 bar × 16
  path, CPU; benchmark trong `kronos-active-repairs-20261111.vi.md` §4); pool
  60 ứng viên sẽ cộng thêm ~5–7 phút nếu bật. Đo cũ 11-10 (~22.5 s/10 symbols
  với child warm, chunked) không còn mô tả runtime hiện tại.
- **UI riêng** *(đã đóng 2026-11-11 — xem §0c)*: các key đã đọc được qua API
  (`/api/settings`, `/api/operator-config`); trang Settings/Dashboard nay hiển
  thị riêng Kronos trong khu AI/forecasting với block dự báo active tách khỏi
  shadow; bằng chứng W1–W6 + screenshot tại `kronos-active-ux-20261111.vi.md`.
- `temperature=0.5` là giả thuyết từ nghiên cứu (ổn định hơn T=1.0 nhưng vẫn 3/10 ở
  quy mô merge); vì mode là context_only nên không có gate phụ thuộc nó.
- Full suite **không** chạy trong pass triển khai 11-10; sau vòng sửa 11-11,
  gate `--stage kronos` (suite PG + smoke model thật) đã chạy xanh một lượt —
  bằng chứng đầy đủ ở `kronos-active-repairs-20261111.vi.md` §5. Full acceptance
  vẫn là việc của script runner.

## 5. Cách bật (khi được phép, chưa thực hiện)

```
PUT /api/operator-config
{"settings": {"kronos_active_enabled": true}, "confirm": true,
 "reason": "<lý do vận hành>", "actor": "<người ra quyết định>"}
```
Tắt: `DELETE /api/operator-config/kronos_active_enabled` (không cần confirm).
Tuning: `PUT /api/settings {"settings": {"kronos_active_paths": 24, ...}}`.
Môi trường cần: isolated interpreter + weights đã pin (đã có trên máy này,
`verify_artifacts` = None) — không cài thêm gì.
