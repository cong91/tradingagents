---
purpose: Repair report — Kronos active path, fixing the failures reproduced in kronos-active-review-20261111.vi.md
branch: feat/trading-research-delivery-20261110
base_commit: 0943103
date: 2026-11-11
status: repaired locally; stage kronos OK; NOT deployed, Auto untouched, no order sent, no commit/push
review: docs/research/kronos-active-review-20261111.vi.md
implementation: docs/research/kronos-active-implementation-20261110.vi.md
---

# Kronos active — báo cáo sửa lỗi 2026-11-11

Tài liệu này ghi các lỗi ĐÃ TÁI HIỆN trong `kronos-active-review-20261111.vi.md`
được sửa, với bằng chứng test chạy thật trong phiên này (lệnh + kết quả nguyên
văn), ownership đã chọn, giới hạn còn lại và benchmark đường runtime đã sửa
bằng model pinned hiện có. Không tuyên bố lợi nhuận: mọi con số dưới đây là
số đo cơ chế (thời gian, số path), không phải PnL hay kỹ năng dự báo.

---

## 0. TL;DR

1. Cả năm lỗi tái hiện trong review đã được sửa và bị chặn bởi regression test
   mới; probe đối chiếu cùng 5 kiểm tra cho kết quả đảo ngược hoàn toàn
   (§1, §2).
2. Ownership lifecycle chọn **scoped per call** (service dựng cho một ứng
   viên, đóng trong `finally` trên success/error/timeout); cold start đo thật
   **5.6–7.1 s mỗi ứng viên** trên model thật — ghi đúng chi phí, không tuyên
   bố warm reuse (§4).
3. `failure_policy` giờ có tác dụng thực: `defer` dừng ứng viên TRƯỚC
   graph/LLM và persist lý do; `pass_through` tiếp tục với lỗi ghi rõ; giá trị
   `context_only` (không phân biệt hành vi) đã bị xoá (§3 mục 5).
4. Gate `--stage kronos` chạy lại trên code cuối: **137 passed** (suite PG,
   75.66 s) + **4 passed** smoke model thật 20.43 s → `stage kronos OK` (§5).
5. Không đụng Auto/venue/risk limits, không đặt lệnh, không commit/push. UI
   (Settings group, dashboard disclosure, browser fixture) thuộc pass UX riêng
   — chưa làm trong đợt này (§6).

---

## 1. Bằng chứng đối chiếu: probe review trước/sau

Review tái hiện (offline, fake sampler, không gọi model/mạng):

```json
{"stale_status":"ok","mixed_symbol_status":"ok","requested_paths":16,"accepted_paths":1,"changed_candle_cache_reused":true,"timed_out_child_killed":false}
```

Probe tương đương chạy lại trên code đã sửa (cùng kỹ thuật fake sampler, không
model/mạng) — kết quả:

```json
{"stale_status": "unavailable", "stale_reason_has_stale": true, "mixed_symbol_status": "unavailable", "mixed_reason_has_symbol": true, "requested_paths": 16, "accepted_paths": null, "short_result_status": "error", "changed_candle_cache_reused": false, "sampler_calls_after_edit": 2, "timed_out_child_killed": true, "pipes_closed": true}
```

Lệnh: `.venv\Scripts\python.exe -I C:\Users\mrc\AppData\Local\Temp\kronos_review_probe_recheck.py`
(probe tạm nằm ngoài repo; logic tương ứng được pin vĩnh viễn trong các test
mới ở §2 — không phụ thuộc file tạm).

Đọc từng giá trị:

| Giá trị review (trước) | Giá trị probe (sau) | Nghĩa |
|---|---|---|
| `stale_status: ok` | `unavailable`, reason chứa `stale` | series cũ hơn biên nến đóng mới nhất bị từ chối, không vào prompt |
| `mixed_symbol_status: ok` | `unavailable`, reason `not all for symbol` | nến sai symbol bị từ chối |
| `requested_paths 16 / accepted_paths 1` | `short_result_status: error`, `accepted_paths: null` | yêu cầu 16 path nhận 1 path là LỖI, không gắn số |
| `changed_candle_cache_reused: true` | `false`, `sampler_calls_after_edit: 2` | sửa giá đóng cùng mốc → cache miss, sample lại |
| `timed_out_child_killed: false` | `true` (kèm `pipes_closed: true`) | close/kill không còn bỏ quên child |

---

## 2. Sửa lỗi ↔ regression test (mỗi lỗi một pin)

Mọi test dưới đây đã chạy trong phiên này; lệnh tổng ở §5.

1. **Series stale/sai identity bị từ chối** — `series_problem`
   (`tradingagents/forecast/service.py`) kiểm: `as_of` timezone-aware, mọi
   candle cùng symbol với series, bar đóng mới nhất của lưới tại biên quyết
   định phải có trong series (không thì `stale`), OHLC finite dương + thứ bậc
   (high ≥ max(open,close) ≥ min ≥ low), volume finite ≥ 0, giữ nguyên các
   kiểm cũ (nến đóng, một timeframe, không gap, đủ lịch sử).
   Pin: `tests/test_forecast_inputs.py::test_a_stale_series_is_refused_even_without_a_late_bar`,
   `::test_a_mixed_symbol_series_is_refused`, `::test_a_naive_cutoff_is_refused`,
   `::test_a_non_finite_or_non_positive_price_is_refused`,
   `::test_inconsistent_bar_order_is_refused`, `::test_a_negative_or_nan_volume_is_refused`.
2. **Cache identity đầy đủ** — `cache_key` + `content_digest`
   (`forecast/service.py`): nội dung nến (sửa giá cùng mốc → miss), venue
   (`exec_exchange_id`), `cutoff_ms` (biên quyết định), setting ảnh hưởng phân
   loại (`max_band_spread_ratio` vào `as_params`), timeframe/horizon/lookback/
   paths/temperature/top_p, model revision, artifact hash, seed.
   Pin: `tests/test_forecast_cache.py::test_the_cache_key_changes_with_every_input_that_changes_the_meaning`,
   `::test_an_edited_close_at_the_same_timestamp_misses_the_cache`,
   `::test_a_venue_change_is_not_served_from_cache`,
   `::test_the_content_digest_changes_with_edited_candle_content`.
3. **Kết quả phải đúng như yêu cầu trước khi `status=ok`** — sampler
   (`_entry_problem`) và service (`_paths_problem`) kiểm: symbol khớp, ĐÚNG số
   path yêu cầu (16 yêu cầu / 1 nhận = lỗi), mỗi path đủ chiều horizon,
   giá finite dương, high ≥ low, seed per-path phân biệt (sampler).
   Pin: `tests/test_forecast_inputs.py::test_a_one_path_answer_to_a_sixteen_path_request_is_an_error`,
   `::test_a_short_horizon_or_bad_price_is_refused_before_status_ok`,
   `::test_a_result_for_a_different_symbol_is_refused`,
   `tests/test_forecast_sampler.py::test_malformed_answers_are_refused_with_the_concrete_reason` (5 tham số),
   `::test_a_correct_answer_carries_the_drawn_paths`.
4. **`KronosSampler.close` không còn bỏ quên child** — bắt process trước khi
   xoá tham chiếu; EOF stdin → wait → kill → reap → đóng mọi pipe.
   Pin: `tests/test_forecast_sampler.py::test_close_uses_the_captured_process_so_the_kill_still_runs`,
   `::test_close_signals_a_cooperative_child_through_stdin_eof`,
   `::test_a_chunk_deadline_kills_the_real_child_and_reports_timeout` (Popen thật, dead sau timeout),
   `::test_close_ends_a_warm_child` (child thật exit 0).
5. **Malformed response không làm lệch child warm** — parent khớp câu trả lời
   theo `request_id` (runner echo cả nhánh lỗi); banner/stale bị bỏ qua có bến;
   nội dung malformed → error cho chunk nhưng child vẫn in-sync (request sau
   vẫn được trả lời); EOF/limit → kill fail-closed (không tin stream).
   Pin: `tests/test_forecast_sampler.py::test_a_malformed_answer_is_an_error_that_keeps_the_child_in_sync`,
   `::test_banner_lines_are_skipped_and_the_answer_matched_by_id`,
   `::test_a_stale_answer_line_is_skipped_not_trusted`,
   `::test_an_absent_answer_is_an_error_and_the_child_is_stopped`.
6. **Preflight artifact trước spawn** — `from_config` gọi `verify_artifacts`
   (memoized, cùng hàm shadow); thiếu file/sai hash → `unavailable` TRƯỚC khi
   dựng sampler (không còn gì để spawn); `artifact_hash` = digest của hash đã
   kiểm (`artifact_digest_from`), sai → None. Shadow KronosProvider cũng ghi
   hash đã kiểm, không phải hash kỳ vọng.
   Pin: `tests/test_forecast_sampler.py::test_from_config_refuses_before_spawning_when_verification_fails`,
   `::test_from_config_records_the_verified_digest_when_artifacts_match`.
7. **Quyết định historical không được phục vụ cache** — service tự bypass
   cache khi bar horizon đầu của quyết định đã đóng; block tạo ra gắn
   `retrospective`; `forecast_for_cycle` biến block `retrospective` thành
   failure (nghiên cứu không vào quyết định active).
   Pin: `tests/test_forecast_cache.py::test_a_historical_decision_never_serves_the_cache`,
   `::test_a_retrospective_entry_is_never_served_forward`,
   `tests/test_forecast_context.py::test_a_retrospective_draw_is_never_fed_to_an_active_decision`.
8. **`defer` dừng ứng viên trước graph/LLM** — `_process_candidate` persist
   forecast/policy, rồi với policy `defer`: trả kết quả với lý do, persist
   terminal `status=rejected, phase=forecast` trên cycle đã claim (gated bởi
   `_cycle_writes_allowed`), KHÔNG build graph, KHÔNG gọi LLM.
   Pin: `tests/test_kronos_decision_integration.py::test_defer_stops_the_candidate_before_the_graph_and_the_llm`,
   `::test_pass_through_continues_with_the_failure_in_the_context` (pass_through
   tiếp tục với `status=timeout` + "not a passed check" trong context thật).
9. **Deadline/cancellation có răng** — trước/sau span autoloop kiểm
   `_raise_if_execution_closed` (deadline + cancelled); trong seam, phần thời
   gian còn lại giới hạn chunk deadline (hết budget → `unavailable`, không
   dựng service); không nhận `execution_lock` (signature pin giữ nguyên).
   Pin: `tests/test_forecast_context.py::test_no_inference_starts_after_the_cycle_deadline`,
   `::test_the_remaining_cycle_budget_bounds_the_service_deadline`,
   `::test_a_cancelled_cycle_reports_the_stop_before_the_work`,
   `tests/test_kronos_decision_integration.py::test_the_forecast_stage_takes_no_execution_lock`.
10. **Tuning validate tại biên API** — `PUT /api/settings` từ chối 422 với
    `details.fields` per-field TRƯỚC `set_config`/persist (không mutation một
    phần); bounds: mode/timeframe/horizon (kể cả cross-field với timeframe
    đang ghi)/paths/temperature/lookback/batch/band/failure_policy, NaN qua
    literal JSON cũng bị từ chối.
    Pin: `tests/test_kronos_decision_settings.py::test_the_settings_api_refuses_an_unrunnable_tuning_value` (11 tham số),
    `::test_the_settings_api_refuses_a_non_finite_number`,
    `::test_the_settings_api_refuses_a_cross_field_horizon_timeframe_pair`,
    `::test_a_rejected_write_does_not_leak_a_partial_mutation`,
    `::test_the_service_still_refuses_a_bad_env_value_at_read_time`.

---

## 3. Quyết định thiết kế

1. **Ownership: scoped per call.** `forecast_for_cycle` dựng service qua
   `from_config` khi `service=None` và đóng trong `finally` — success, error,
   timeout đều đóng; shutdown không còn gì phải dọn (không child sống qua
   ứng viên, không global pool, không owner ẩn). Đổi lấy: mỗi ứng viên trả
   cold start (đo ở §4) và cache service chỉ sống trong một call. `cache`
   trong một service instance vẫn hoạt động (test pin) nhưng reuse liên
   ứng viên không tồn tại — đúng như review yêu cầu ("không tuyên bố warm
   reuse khi runtime không làm").
2. **`request_id` echo trong runner.** Là thay đổi nhỏ nhất làm cho line
   framing có thể kiểm chứng: banner trông như JSON hoặc câu trả lời cũ
   (stale) không còn bị nhận nhầm; malformed content vẫn giữ child warm vì
   một dòng vào = một dòng ra.
3. **Kill fail-closed thay vì resync "thông minh".** EOF hoặc vượt limit dòng
   → kill; trust stream sau trạng thái không biết là rủi ro desync chính.
4. **Xoá `context_only` khỏi failure_policy.** Nó trôi về `defer` trong mọi
   nhánh cũ — knob không có phân biệt hành vi là lựa chọn giả. Vốn từ giờ là
   `("defer", "pass_through")`; biên API từ chối giá trị cũ.
5. **`forward` là timestamp claim.** `generation_kind=forward` chỉ nói "các
   draw xảy ra trước khi bar horizon đầu đóng"; nó không phải bằng chứng dự
   báo tồn tại tại mốc quyết định và không phải bằng chứng OOS/alpha. Block
   giờ ghi `created_at` để độc lập đối chiếu; historical decision bypass
   cache hoàn toàn.
6. **`MAX_BATCH_ROWS = 960`.** Trần mới trong `validate_settings` + biên API —
   960 rows là cấu hình đo 6.07 GiB (gần OOM); row budget nhỏ hơn vẫn ổn.
7. **Autoloop giữ thay đổi tối thiểu.** Chỉ: truyền `deadline`/`cancelled`
   vào seam, đổi 2 kiểm `_cycle_timeout` thành `_raise_if_execution_closed`,
   thêm nhánh defer (persist + return) và gate persist phase forecast bằng
   `_cycle_writes_allowed`. Không đụng discovery/refill (ai-note seam giữ
   nguyên), không extract gì ngoài seam đã duyệt.

---

## 4. Benchmark đường runtime đã sửa (model pinned hiện có)

Lệnh: `.venv\Scripts\python.exe -I C:\Users\mrc\AppData\Local\Temp\kronos_runtime_benchmark.py`
(fixture nến đóng xác định 96 bar 4h, 16 paths, T=0.5, horizon 24h, CPU;
mỗi run = một service scoped đúng như `forecast_for_cycle` chạy cho một ứng
viên, đóng trong `finally`):

```json
{"run": 1, "build_preflight_ms": 0.2, "wall_ms_total": 7061.8, "status": "ok", "paths": 16, "generation_kind": "forward", "artifact_hash_set": true}
{"run": 2, "build_preflight_ms": 0.2, "wall_ms_total": 5555.4, "status": "ok", "paths": 16, "generation_kind": "forward", "artifact_hash_set": true}
{"run": 3, "build_preflight_ms": 0.2, "wall_ms_total": 5990.5, "status": "ok", "paths": 16, "generation_kind": "forward", "artifact_hash_set": true}
```

Đọc trung thực:

- **Chi phí scoped per call: ~5.6–7.1 s wall mỗi ứng viên** (interpreter
  spawn + torch import + weight load + inference 96 bar × 16 path). Đây là
  cái giá của ownership scoped — KHÔNG có warm reuse giữa các ứng viên trong
  ownership này.
- Preflight artifact memoized: ~0.2 ms sau lần hash đầu (kích thước/mtime
  không đổi); lần đầu trong process hash ~115 MB weights.
- Block trả về `status=ok`, 16 paths, `generation_kind=forward`,
  `artifact_hash` đã kiểm (non-null), cache cùng identity hit trong service.
- Smoke stage (model thật, active path): `4 passed in 20.80s` — gồm cả test
  deadline (chunk deadline 0.05 s → block `timeout`, không hang).
- **Không có claim lợi nhuận/kỹ năng**: các số trên là thời gian cơ chế.
  16 paths là sampling budget; ratio kèm nhãn "NOT a probability"; không có
  bất kỳ phép so sánh PnL nào gắn cho Kronos (shadow cũng chưa từng có
  `net_r` gắn cho model).

---

## 5. Lệnh và kết quả (chạy trong phiên này)

| Lệnh | Kết quả |
|---|---|
| `.venv\Scripts\python.exe -m ruff check .` | All checks passed |
| `.venv\Scripts\python.exe -m pytest -q tests\test_forecast_inputs.py tests\test_forecast_cache.py tests\test_forecast_sampler.py tests\test_forecast_context.py tests\test_kronos_decision_integration.py` | 74 passed (chạy trước khi thêm 2 test preflight) |
| `.venv\Scripts\python.exe -m pytest -q tests\test_forecast_sampler.py` | 21 passed (gồm preflight) |
| `.venv\Scripts\python.exe -m pytest -q tests\test_shadow_forecasts.py tests\test_shadow_worker.py tests\test_shadow_isolation.py tests\test_shadow_replay.py` | 64 passed, 24 skipped (thiếu DSN) |
| `.venv\Scripts\python.exe -m pytest -q tests\test_shadow_forecasts.py tests\test_shadow_worker.py tests\test_kronos_decision_integration.py tests\test_forecast_inputs.py tests\test_forecast_cache.py tests\test_forecast_sampler.py tests\test_forecast_context.py` | 118 passed, 19 skipped (thiếu DSN) |
| `.venv\Scripts\python.exe -m pytest -q tests\test_autoloop_futures_scope.py tests\test_autoloop_futures_candidates.py tests\test_autoloop_modes.py tests\test_server_autoloop.py tests\test_settings_api.py` | 53 passed, 116 skipped (thiếu DSN) |
| `.venv\Scripts\python.exe -m pytest -q tests\test_kronos_decision_integration.py tests\test_kronos_decision_settings.py tests\test_forecast_inputs.py tests\test_forecast_cache.py tests\test_forecast_sampler.py tests\test_forecast_context.py tests\test_shadow_isolation.py tests\test_operating_brief.py` | 84 passed, 59 skipped (thiếu DSN) |
| `.venv\Scripts\python.exe -m pytest -q tests\test_forecast_inputs.py tests\test_forecast_cache.py tests\test_forecast_sampler.py tests\test_forecast_context.py tests\test_kronos_decision_integration.py tests\test_shadow_forecasts.py` (chạy cuối) | 108 passed, 5 skipped (thiếu DSN) |
| `.venv\Scripts\python.exe scripts\verify_research_delivery.py --stage kronos` (lần 1) | kronos-suite **FAIL** `1 failed, 134 passed` — test NaN của chính đợt sửa này encode NaN bằng httpx client (client từ chối, không tới server); sửa test gửi literal `NaN` qua raw body. smoke: 4 passed 23.83 s |
| `.venv\Scripts\python.exe scripts\verify_research_delivery.py --stage kronos` (lần 2) | **stage kronos OK**: kronos-suite `137 passed in 73.24s` (9 file, PG dùng một lần, reject_skips) + kronos-model-smoke `4 passed in 20.80s` |
| `.venv\Scripts\python.exe scripts\verify_research_delivery.py --stage kronos` (lần 3, trên code cuối sau 1 fix nhỏ `from_config`) | **stage kronos OK**: kronos-suite `137 passed in 75.66s` + kronos-model-smoke `4 passed in 20.43s` — bằng chứng chốt |
| probe đối chiếu review (§1) | giá trị đảo ngược đúng như bảng |
| benchmark (§4) | 3 run scoped: 7061.8 / 5555.4 / 5990.5 ms |

Không có test nào được skip để đi qua lỗi; skip chỉ xảy ra ở fixture thiếu DSN
PG (contract fixture hiện có), và stage runner dùng PG dùng một lần với
`reject_skips` nên suite stage không có skip ẩn.

---

## 6. Giới hạn còn lại (trung thực)

- **Không có bằng chứng lợi nhuận** — đợt này sửa đúng/sai cơ chế; không có
  phép so sánh PnL, win rate hay "Kronos giúp gì" ở bất kỳ đâu. Sample ratio
  vẫn là sample ratio, nhãn "NOT a probability" giữ nguyên trong prompt.
- **UI chưa làm trong đợt này** (thuộc pass UX theo plan): Settings chưa có
  nhóm riêng cho active Kronos; dashboard/cycle chưa hiển thị disclosure
  `kronos_forecast`/`kronos_policy`; browser fixture vẫn `next dev --webpack`
  chưa `next start`; test performance render vẫn còn import module vị thế cũ.
  Các mục này nằm trong review `kronos-active-review-20261111.vi.md` §"UI và
  nghiệm thu còn thiếu" và KHÔNG thuộc phạm vi 7 mục sửa của ask này.
- **Chi phí cộng thêm đo thật**: ~5.6–7.1 s/candidate (scoped cold start) +
  inference; pool 60 ứng viên sẽ cộng thêm ~5–7 phút nếu bật — chưa bật, và
  quyết định bật thuộc operator switch có confirm/reason/audit.
- **Cache trong service scoped chỉ sống một call**: reuse liên ứng viên qua
  cache KHÔNG tồn tại với ownership hiện tại; nếu sau này cần reuse thật thì
  phải thiết kế owner rõ ràng (ví dụ cycle-level owner) kèm cleanup — không
  làm trong đợt này vì ask ưu tiên ownership đơn giản và cleanup đảm bảo.
- **Split proposal (escalation covenant)**: `tradingagents/forecast/service.py`
  giờ chứa nhiều vai trò (settings/tuning boundary, input validation, cache,
  block building, rendering). Không phải đống code lộn xộn mới — vai trò này
  đã nằm trong file từ khi tạo — nhưng đã đạt tín hiệu split (multi-role
  identity). Seam đề xuất: tách `settings/tuning_problems + ForecastSettings`
  thành `forecast/settings.py` và `render_forecast_block` thành
  `forecast/rendering.py`. Đề xuất để quyết định, KHÔNG tự tách trong đợt này
  (ngoài seam đã duyệt).
- Full suite chưa chạy trong đợt này (theo yêu cầu "Focused tests only");
  script runner là nơi chạy full acceptance sau khi source ổn.

## 7. Trạng thái bàn giao

- Code sửa local, sạch ruff, gate kronos xanh (suite + smoke model thật).
- Mặc định: `kronos_active_enabled=False`, `mode=context_only`,
  `failure_policy=defer`, 16 paths, T=0.5 — không gì tự bật.
- Auto/venue/risk limits/confirmation/bracket/authorization: không đổi.
- Chưa deploy, chưa commit/push, chưa đặt lệnh.
