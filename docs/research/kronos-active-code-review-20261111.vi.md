---
purpose: Independent code review of the Kronos active repair pass (2026-11-11)
reviewer: independent verification agent (read-only, offline evidence)
scope: tradingagents/forecast/*, server/kronos_context.py, server/autoloop.py (kronos wiring), server/settings.py (tuning boundary), server/operator_config.py, tradingagents/default_config.py (kronos keys), shadow/kronos.py (hash), 6 test file mới + smoke
inputs:
  - docs/research/kronos-active-review-20261111.vi.md (báo cáo lỗi gốc)
  - docs/research/kronos-active-repairs-20261111.vi.md (báo cáo sửa)
verdict: READY cho phạm vi blockers của ask này — mọi lỗi tái hiện trong review gốc đều được xác nhận đã sửa trên code hiện tại bằng probe độc lập; 4 khiếm khuyết nhỏ còn lại không thuộc nhóm blocker (§6)
date: 2026-11-11
---

# Code review độc lập bản sửa Kronos active — 2026-11-11

Review đọc lại toàn bộ source đã sửa và **tự chạy lại bằng chứng**, không tái sử
dụng kết quả của agent sửa lỗi. Không chạy full suite / full acceptance (do script
runner phụ trách), không deploy, không commit, không đặt lệnh.

## 1. Lệnh đã chạy trong phiên này và kết quả nguyên văn

| # | Lệnh | Kết quả |
|---|---|---|
| E1 | `.venv\Scripts\python.exe -I C:\Users\mrc\AppData\Local\Temp\kronos_indep_probe_20261111.py` | JSON đầy đủ ở §2 (probe viết riêng, độc lập với probe của agent sửa) |
| E2 | `.venv\Scripts\python.exe -m pytest -q tests\test_forecast_inputs.py tests\test_forecast_cache.py tests\test_forecast_sampler.py tests\test_forecast_context.py tests\test_kronos_decision_integration.py` | `76 passed in 8.03s` (0 skip) |
| E3 | `.venv\Scripts\python.exe -m pytest -q tests\test_kronos_decision_settings.py tests\test_shadow_forecasts.py tests\test_shadow_isolation.py tests\test_operating_brief.py` | `40 passed, 64 skipped in 2.24s` — skip toàn bộ do `TRADINGAGENTS_TEST_DATABASE_URL` chưa set (fixture PG), đúng như repair doc ghi; **không có test đỏ bị skip** |
| E4 | `.venv\Scripts\python.exe -m pytest -q tests\test_kronos_active_smoke.py -m "integration"` | `4 passed in 19.78s` — **model thật (pinned weights local), gồm cả test deadline 0.05 s → timeout, không hang** |
| E5 | `.venv\Scripts\python.exe -m ruff check .` | `All checks passed!` |
| E6 | `.venv\Scripts\python.exe -I C:\Users\mrc\AppData\Local\Temp\kronos_tuning_probe_20261111.py` | JSON ở §5 — biên tuning `tuning_problems` không cần DSN |
| E7 | `git diff -- server/autoloop.py` | +107/−1: chỉ import, block forecast stage, nhánh defer, `_kronos_block_for` — đúng "small composition wiring", không đụng discovery/refill/mode/risk |

Chưa chạy lại (ngoài phạm vi ask, ghi rõ chứ không giả định):
`scripts\verify_research_delivery.py --stage kronos` (137 passed của repair doc —
script runner phụ trách full acceptance), benchmark §4 của repair doc (5.6–7.1 s)
— chỉ đối chiếu gián tiếp qua E4 (4 test model thật ~20 s tổng).

## 2. Probe độc lập — tái hiện lại 5 lỗi của review gốc trên code đã sửa

Probe: `%TEMP%\kronos_indep_probe_20261111.py` (fake sampler, không model/mạng/DB,
chạy với `-I`, tự chèn repo path). Kết quả nguyên văn:

```json
{
  "stale_status": "unavailable",
  "stale_reason_has_stale": true,
  "stale_sampler_calls": 0,
  "mixed_symbol_status": "unavailable",
  "mixed_reason_has_symbol": true,
  "mixed_sampler_calls": 0,
  "requested_paths": 16,
  "accepted_paths": null,
  "short_result_status": "error",
  "short_reason": "the sample carries 1 path(s) for a 16-path request; a request is satisfied only ",
  "short_has_numbers": false,
  "edited_series_problem": null,
  "changed_candle_cache_reused": false,
  "sampler_calls_after_edit": 2,
  "venue_change_sampler_calls": 3,
  "timed_out_child_killed": true,
  "pipes_closed": true,
  "real_deadline_status": "timeout",
  "real_child_dead_after_timeout": true,
  "preflight_problem": true,
  "preflight_hash_is_none": true,
  "preflight_no_sampler": true,
  "cancelled_reason": "the cycle was cancelled before inference",
  "spent_budget_reason": "the cycle deadline was already reached; no inference was sta",
  "policy_defer": "defer",
  "policy_pass": "pass",
  "policy_off": "use",
  "history_all_closed": true,
  "history_len": 40
}
```

Đối chiếu với JSON của review gốc (`stale_status ok`, `mixed_symbol_status ok`,
`requested_paths 16 / accepted_paths 1`, `changed_candle_cache_reused true`,
`timed_out_child_killed false`): **cả 5 giá trị đều đảo ngược đúng chiều**.

Ghi chú trung thực về probe: lần chạy đầu, case cache của chính probe bị lỗi —
tôi sửa close cùng mốc nhưng giữ high/low cũ, series bị từ chối vì OHLC sai thứ
bậc nên chưa kiểm tới cache; đã sửa probe (nến sửa vẫn hợp lệ) rồi chạy lại. Kết
luận cache chỉ dựa trên lần chạy thứ hai: request giống hệt → hit (`is first`);
sửa giá cùng mốc → 2 lần gọi sampler (miss); đổi venue → lần gọi thứ 3 (miss).

## 3. Đối chiếu từng blocker với code (file:line)

1. **Stale series bị từ chối** — `series_problem` kiểm `newest_closed =
   cutoff − cutoff % step` và từ chối khi `candles[-1].close_time <
   newest_closed` (`tradingagents/forecast/service.py:329-337`), sau kiểm nến
   đóng/múi giờ (`:306-328`). Refusal đi kèm `stale=True` → prefix `stale:`
   (`service.py:653-657`, `:771-777`). Sampler không được gọi khi series lỗi
   (probe: `stale_sampler_calls: 0`).
2. **Mixed-symbol bị từ chối** — tập symbol của nến phải == `{series.symbol}`
   (`service.py:314-316`). Probe: `mixed_reason_has_symbol: true`, 0 lần gọi.
3. **Path count/độ dài horizon/giá finite trước `status=ok`** — hai lớp: sampler
   `_entry_problem` (`sampler.py:511-559`: symbol echo, đúng số path, đủ horizon,
   giá finite dương, high ≥ low, seed per-path phân biệt) và service
   `_paths_problem` (`service.py:498-530`) chạy lại tại seam quyết định — chặn
   cả sampler do caller cung cấp. `_block_from` chỉ gắn `status=ok` sau check
   (`service.py:705-723`). Probe: 16 yêu cầu/1 nhận → `error`, `band == {}`.
4. **Cache identity đầy đủ** — `cache_key` gồm symbol+venue+timeframe+
   `data_open_time`+`cutoff_ms`+`data_digest` (OHLCV nội dung qua
   `content_digest`, `service.py:384-397`)+`as_params()` (kèm
   `max_band_spread_ratio`)+mode+model_revision+`artifact_hash`+seed
   (`service.py:400-444, 693-701`). `_key` truyền đủ venue/cutoff/digest.
5. **Cleanup child không bị bỏ quên** — `close()`/`kill()` capture process
   trước khi xoá tham chiếu (`sampler.py:228-238`), `_stop` EOF→wait→kill→
   reap→đóng mọi pipe (`sampler.py:240-259`). Timeout chunk → `self.kill()`
   (`sampler.py:308-317`); EOF/limit → kill fail-closed (`sampler.py:324-332`).
   Probe dùng cả fake Popen lẫn child thật ("hang"): `timed_out_child_killed:
   true`, `real_child_dead_after_timeout: true`.
6. **Preflight artifact trước spawn** — `from_config` gọi `verify_artifacts`
   (memoized, chung với shadow) và chỉ dựng sampler khi `problem is None`;
   `artifact_hash = artifact_digest_from(verified)` — digest của hash ĐÃ kiểm,
   sai → None (`service.py:568-612`; `artifact.py:162-176`). Shadow
   KronosProvider cũng chỉ ghi hash khi verify thành công
   (`shadow/kronos.py:133-141`). Probe với thư mục rỗng thật (không monkeypatch
   verify): `preflight_problem: true`, `preflight_no_sampler: true`.
7. **Historical decision không ăn cache** — `decision_is_historical` khi
   `created > first_realized_close` → bypass cache, draw gắn `retrospective`
   (`service.py:659-672`); `ForecastCache.put` không lưu retrospective
   (`service.py:469-471`); seam biến retrospective thành failure trước khi vào
   quyết định (`server/kronos_context.py:186-202`). Pin: E2 (test_forecast_cache).
8. **Policy có răng** — `defer` persist terminal `rejected/phase=forecast` rồi
   `return result` TRƯỚC `build_graph` (`server/autoloop.py:1319-1344`), có gate
   `_cycle_writes_allowed`; `pass_through` tiếp tục với lỗi trong context;
   `context_only` đã xoá khỏi `FAILURE_POLICIES` (`service.py:94-101`), refusal
   ở settings (`:196-200`) và biên API. Pin: E2 (`test_defer_stops...`,
   `test_pass_through_continues...` — dùng thật `_process_candidate`).
9. **Deadline/cancellation** — trước/sau inference:
   `_raise_if_mode_disabled` + `_raise_if_execution_closed` cả hai phía
   (`autoloop.py:1296-1305`); seam đổi deadline thành chunk deadline =
   min(bound sampler, thời gian còn lại), budget <= 0 → không dựng service
   (`kronos_context.py:143-152`); `cancelled` kiểm trước work
   (`kronos_context.py:154-158`); không nhận `execution_lock` (signature
   `kronos_context.py:112-122`, pin bởi test). Không giữ execution/protection
   lock — chạy trên thread của candidate.
10. **Ownership scoped per call** — `forecast_for_cycle(service=None)` dựng
    service trong call và đóng trong `finally` (success/error/timeout)
    (`kronos_context.py:139-183`); autoloop luôn truyền `service=None`
    (`autoloop.py:1674-1677`). Không còn "dựng service không đóng" như review.
11. **Tuning validate tại biên API** — `PUT /api/settings` gọi
    `tuning_problems(validated, get_config())` TRƯỚC `set_config`/persist
    (`server/settings.py:190-214`); refuse 422 kèm `details.fields`.
    `_ENV_OVERRIDES` chỉ chứa 9 tuning key, `kronos_active_enabled` không env-armable
    (`default_config.py:111-119`, `:417-441`); operator switch có
    `gate=True, group=forecast` (`operator_config.py:240-263`), không nằm trong
    `auto_modes.MODE_KEYS` (grep "kronos" trong `server/auto_modes.py`: 0 match).

## 4. Test có thật hay fake?

Đã đọc đủ 6 file: `test_forecast_inputs.py`, `test_forecast_cache.py`,
`test_forecast_sampler.py`, `test_forecast_context.py`,
`test_kronos_decision_integration.py`, `test_kronos_decision_settings.py`.

- Không thấy test giả: assert cứng trên status/reason/số lần gọi sampler; sampler
  test chạy child thật qua stub runner (`test_a_chunk_deadline_kills_the_real_child_and_reports_timeout`
  monkeypatch Popen bằng subclass **thật** của Popen, assert child chết);
  integration test drive hàm node Trader/PM thật với LLM ghi âm và assert block
  nằm trong prompt thật; smoke model thật **fail** (không skip) khi thiếu môi
  trường model (`test_kronos_active_smoke.py:77-92,101-105`).
- Pin khớp 1-1 với 10 mục repair doc §2; E2/E3/E4 đều xanh.
- Skip duy nhất trong E3 là fixture PG thiếu DSN (contract hiện có), không phải
  skip để né lỗi; stage runner dùng PG dùng một lần + `reject_skips` nên suite
  stage không có skip ẩn (đối chiếu repair doc §5, không tự chạy lại).

## 5. Biên tuning offline (E6) — vì API test cần PG

`tuning_problems` từ chối đúng: paths 3/64/16.5, mode `hard_filter`, timeframe
`8h`, horizon 25 (không whole-bar 4h), horizon 300 (>72 bar), lookback 8,
max_batch_rows 961, failure_policy `context_only`/`sometimes`, temperature
0/2.5/NaN, band spread 0/−0.1; bộ giá trị hợp lệ được chấp nhận; cross-field
horizon×timeframe đang ghi đúng. (Ghi chú: case `horizon=25` trên config
timeframe `1h` được chấp nhận — đúng, vì 25h là 25 bar 1h.)

## 6. Khiếm khuyết nhỏ còn lại (không thuộc nhóm blocker của ask này)

1. **Lookback > 200 bị cắt âm thầm trên đường active** — `history_for` lấy
   `limit=min(lookback, DEFAULT_HISTORY_BARS=200)` (`server/kronos_context.py:104-108`)
   trong khi `lookback_bars` cho phép tới 512 (`artifact.py:44-46`; biên API chấp
   nhận ≤512). Series 200 bar vẫn qua `series_problem` (chỉ cần ≥32) và forecast
   chạy trên ít bar hơn cấu hình, block không ghi số bar thực dùng. Mặc định 96
   không bị ảnh hưởng. Đề xuất: hoặc honour cấu hình (limit=lookback), hoặc từ
   chối `lookback_bars > DEFAULT_HISTORY_BARS` ở settings, hoặc ghi
   `bars_used` vào block.
2. **Ghi stdin nằm ngoài cửa sổ deadline** — `KronosSampler._communicate` viết
   payload vào stdin (`sampler.py:417-418`) trước khi bắt đầu đo deadline
   (`reader.join` tại `:451`). Nếu child ngừng đọc stdin (đang chạy request
   trước) và payload > buffer pipe (~64 KB), write có thể chặn ngoài deadline.
   Đường active 1 symbol (~vài chục KB) an toàn thực tế; chunk shadow đa symbol
   (~300 KB JSON) có thể chặn đến khi request trước xong. Rủi ro thấp, có biên
   ở tầng cycle timeout; nêu ra để không tuyên bố deadline "bao trùm toàn bộ".
3. **Docstring chi phí cold start cũ** — `kronos_context.py:31-33` ghi
   "~2.4-2.7 s" (chỉ phần load) trong khi số vận hành đúng cho ownership scoped
   là ~5.6-7.1 s/candidate (repair doc §4; nhất quán với E4). Không sai, nhưng
   con số mà operator gặp trong code là số nhỏ hơn.
4. **UI/vận hành còn mở (đã được repair doc ghi đúng là thuộc pass UX riêng)**
   — browser fixture vẫn chạy `next dev --webpack` thay vì `next start`
   (`tests/browser/test_dashboard_brief_offline.py:639`); Settings chưa có nhóm
   forecast riêng trong UI; dashboard chưa có disclosure `kronos_forecast`/
   `kronos_policy`; các claim screenshot/W1–W6 chưa đóng. Đây là hạng mục (3)
   của plan tuần tự, không phải blocker của đợt sửa cơ chế này.

## 7. Đối chiếu tuyên bố của repair doc với bằng chứng tôi chạy

| Tuyên bố repair doc | Trạng thái ở phiên review |
|---|---|
| Probe 5 giá trị đảo ngược (§1) | **Xác nhận** — probe độc lập E2 cho cùng chiều |
| 10 pin regression (§2) | **Xác nhận** — E2 76 passed; phần API (E3) skip do DSN, hàm biên kiểm offline bằng E6 |
| Ownership scoped + finally (§3.1) | **Xác nhận** — code `kronos_context.py:139-183` + test E2 |
| `MAX_BATCH_ROWS = 960` + biên API (§3.6) | **Xác nhận** — `sampler.py:56` + E6 |
| ruff sạch (§5) | **Xác nhận** — E5 |
| stage kronos 137 passed + smoke 4 passed (§5) | Smoke **xác nhận** (E4: 4 passed 19.78s); stage 137 **không chạy lại** (script phụ trách) |
| Benchmark 5.6–7.1 s/candidate (§4) | **Không chạy lại**; nhất quán cấp độ lớn với E4 |
| "Không có claim lợi nhuận/PnL" | **Xác nhận** — grep tài liệu: mọi con số là đo cơ chế; nhãn `model_sample_ratio_not_probability` giữ trong prompt (`service.py:945-983`) |
| Giới hạn UI còn mở (§6) | **Xác nhận đúng** — §6.4 ở trên |

## 8. Kết luận

- **READY** cho phạm vi blockers của ask này: stale/mixed-symbol/path-count/
  cache/cleanup/artifact preflight đều đã sửa thật trên code hiện tại, pin bằng
  test thật, và được tái xác nhận bằng probe độc lập chạy trong phiên này.
- Wiring active trước Trader/PM đúng như thiết kế duyệt: block dữ liệu đi vào
  `execution_context` trước graph (không lọc hướng, không veto), policy defer
  có tác dụng thật trước LLM, deadline/cancel hai phía, không giữ lock, off là
  off (byte-identical context), mặc định `kronos_active_enabled=False`.
- 4 khiếm khuyết nhỏ §6 không chặn; hai mục đầu nên gậy vào lần chỉnh kế tiếp
  trên cùng seam, hai mục sau thuộc pass UX/full acceptance.
