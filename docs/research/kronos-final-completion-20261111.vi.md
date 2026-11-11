---
purpose: Báo cáo hoàn thiện bốn gap Kronos và tách trách nhiệm forecast/autoloop
branch: feat/trading-research-delivery-20261110
date: 2026-11-11
status: hoàn tất code local; full runner exit 0; nhóm test sau split và browser conflict đạt riêng
plan: docs/plans/kronos-final-completion-20261111.md
---

# Hoàn thiện Kronos và tách module theo trách nhiệm

Bốn khoảng trống đã được sửa và kiểm chứng: fixture mở thủ công được cách ly khỏi API runtime, browser test đi qua đúng công tắc Kronos, lookback trên 200 không còn bị cắt âm thầm, và một deadline bao trùm pipe write/flush/read. Forecast và autoloop đã tách theo trách nhiệm với các caller/test chuyển sang module sở hữu thật. Full runner đã **exit 0**; các test được di chuyển và scenario conflict bổ sung sau đó cũng đã đạt riêng.

Không deploy, không apply migrations vào runtime, không bật Auto/Kronos, không đổi venue/risk, không gửi lệnh, không commit/push. Đây là bằng chứng kỹ thuật local cho công cụ nghiên cứu, không phải bằng chứng lợi nhuận hoặc tư vấn tài chính.

## 1. Bốn khoảng trống đã đóng

| Vấn đề | Implementation | Bằng chứng |
| --- | --- | --- |
| Fixture mở thủ công có thể rơi về runtime | `tests/browser/dashboard_fixture.py` có default state khi không có `__FIXTURE_STATE__` và badge `Offline test fixture`. `fixture_server.py` viết Next config riêng **không rewrites**, không đọc proxy config runtime. Known APIs dùng fixture; API lạ chỉ gọi cùng origin và nhận local 404. Build/server dùng TEMP root riêng, `NEXT_DIST_DIR=.next`. | Scenario `manual_open` không chèn state qua CDP, kiểm dashboard ready, badge và unknown API 404. Trap HTTP server được truyền làm `API_INTERNAL_URL` cả build/start; mọi scenario assert **zero request tới trap**. |
| Thiếu browser test đúng công tắc Kronos | `tests/test_operator_settings_kronos_browser.py` render `SettingsScreen` thật bằng `next build --webpack` + `next start`. Kiểm vị trí AI, tách khỏi Auto, confirm/reason/Save, PUT chỉ có `kronos_active_enabled`, readback, Undo và disable; domain 422 gắn đúng field. Có event fixture mô phỏng server đổi đúng key Kronos khi bản nháp đã được xác nhận. | Server change giữ draft, xóa confirm/reason cũ, khóa Save, không gửi stale PUT; review server data không ghi thêm; Auto Spot/Futures giữ nguyên. Bộ dashboard + Kronos cuối: **13 passed in 121.42s**, không skip, real Chrome và production fixtures. |
| Lookback trên 200 bị cắt âm thầm | `server/kronos_context.history_for` truyền đúng `settings.lookback_bars`, bỏ cap 200. `forecast/inputs.py` yêu cầu đủ configured count và model floor; thiếu trả unavailable kèm actual/required, không bù nến. | History tests cho **96, 201, 256, 512**; nhận 201 trong khi cần 256 bị từ chối. Fixtures cũ 40 nến đổi thành đủ 96/100 để vẫn chạm đúng validation đang kiểm. Default 96 giữ nguyên. |
| Deadline chỉ bao chờ output | `KronosSampler._communicate` đặt một absolute deadline trước serialize/write/flush/read. Parent chỉ join phần thời gian còn lại. Hết hạn thì **kill child trước**, reap, join I/O/stderr rồi close pipes; không dùng lại child/late answer. | Child thật không đọc stdin + payload 1 MB, outer watchdog và subprocess timeout: kiểm TimeoutExpired, elapsed bound, child dead, streams closed và không còn worker. Delayed write + child không trả lời kiểm read không được cấp budget mới. |

`use-settings.ts` giờ đọc `validation_error.details.fields` cho những setting key đã biết, giữ draft và hiện lỗi trên đúng input. `SettingField` nối hint/unit/error với `aria-describedby`; test SSR kiểm các ID tồn tại và `aria-invalid` đúng.

## 2. Forecast: nơi sở hữu hiện hành

| Module | Trách nhiệm |
| --- | --- |
| `forecast/settings.py` | `ForecastSettings`, mode/failure constants, `settings_from_config`, `tuning_problems`; domain validation trước mutation |
| `forecast/block.py` | `CandleSeries` và `ForecastBlock`, result/JSON shape của đường active |
| `forecast/inputs.py` | Identity/timeframe, cutoff timezone-aware, configured lookback, nến đóng, staleness, OHLC/volume và gap |
| `forecast/cache.py` | Content digest, cache identity theo OHLCV/venue/cutoff/model/params/seed và bounded cache |
| `forecast/rendering.py` | `render_forecast_block`, `render_context_section`; prompt có count/ratio/calibration/time/model |
| `forecast/service.py` | Artifact preflight, sampler orchestration, result validation và dựng forecast block |
| `forecast/sampler.py` | Process/pipe ownership, request-id framing, batching/deadline và validation answer |
| `forecast/artifact.py`, `contract.py`, `kronos_runner.py` | Giữ concern pinning, vocabulary và isolated child model runner hiện có |

Caller/import/test dùng owning module trực tiếp. Không giữ wrapper hoặc re-export tương thích từ `service.py`; bề mặt package `__init__` hiện có trỏ vào module sở hữu đúng.

## 3. Autoloop: ranh giới mới

| Module | Trách nhiệm |
| --- | --- |
| `server/autoloop.py` | Scheduler lifecycle/state, tuần tự Auto Spot/Futures trong cùng cycle budget, cycle/execution locks và composition builders |
| `server/autoloop_api.py` | HTTP autoloop/cycles/halt/reset; `server.main` đăng ký router này |
| `server/autoloop_analysis.py` | Candidate analysis: market/account → forecast → graph/Trader/PM → execution policy |
| `server/autoloop_candidates.py` | Discovery, screening timeout/cancel, preflight contract, sort/select/refill và pin contract |
| `server/autoloop_execution.py` | Predicate deadline/cancel/write, mode authorization/revocation và order guard dưới execution lock |
| `server/autoloop_futures_context.py` | Closed1h/4h, funding/open-interest, mark, direction/confirmation, ATR và seam Kronos |
| `server/autoloop_cycle_records.py` | Rejection record/snapshot/confirmation evidence qua `futures_cycle_telemetry` |
| `server/autoloop_futures_execution.py` | Proposal validation/caps và Futures policy với RiskGuard/confirmation/bracket/order guard |
| `autoloop_cycles.py`, `autoloop_telemetry.py` | Giữ owner mode dispatch và telemetry đã có |

`position_review.py` import funding/OI từ context owner. Test call/monkeypatch đã chuyển sang module thật, không thêm compatibility alias. Rating vocabulary, leverage ceiling, venue/account identity, risk/sizing, confirmation và protection rules giữ nguyên.

Các claim/write trước forecast và terminal write được đưa qua predicate deadline/cancel hiện có. Regression ở `tests/test_autoloop_cancellation.py` kiểm hủy trong market/context/account và graph success/error: không claim/write muộn; candidate Futures đã hủy trả về trước claim/order. Đây là cooperative checks giữa các stage, không phải atomic database fencing.

Test monolith `test_server_autoloop.py` đã tách thành sáu nhóm API, scheduler, spot cycle, futures cycle, proposal và cycle records; fixtures chung ở `tests/autoloop_runtime_fixtures.py`. Phép chuyển bằng AST xác nhận **68 test functions giữ nguyên body/decorators/parametrization**, không trùng/mất test. Sáu nhóm hiện hành chạy với PG disposable: **87 passed in 79.14s** (gồm parametrization). Không chạy lại script extraction trên các file đã tách.

Rà cấu trúc source, direct callers và tests đã chạm không phát hiện thêm seam bắt buộc trong scope này. Dependency graph autoloop không có cycle; không còn import helper từ owner cũ; không có compatibility wrapper mới. Rà cả untracked files, không chỉ tracked diff.

## 4. Bằng chứng focused

Mọi Python/pytest/ruff command dưới đây dùng `.venv\Scripts\python.exe`.

| Kiểm chứng | Kết quả |
| --- | --- |
| Forecast inputs/cache/context/sampler | **73 passed** |
| Futures candidates/scope/modes + active Kronos decision integration | **63 passed in 4.37s** |
| Cancellation + modes + decision integration | **42 passed in 3.06s**, gồm 4 ca cancellation mới |
| Required-browser runner contract + field accessibility SSR | **4 passed** |
| Confirmation replay sau sửa direct-owner datetime patch | **64 passed in 3.06s** |
| Settle/universe sau sửa direct-owner imports | **12 passed in 2.17s** |
| Sáu test groups sau split, PG disposable | **87 passed in 79.14s** |
| Browser production cuối, gồm manual isolation và Kronos serverAhead | **13 passed in 121.42s**, không skip; đã chạy lại sau correction process-group cleanup |
| Fixture process ownership + runner contract + accessibility + forecast context | **23 passed in 3.40s**; gồm 3 cases mới cho POSIX session ownership/cleanup, mô phỏng trên Windows |

## 5. Full acceptance và lịch sử lỗi

Command `.venv\Scripts\python.exe -u scripts\verify_research_delivery.py --full` đã kết thúc **exit 0**. Log: `.zcode/artifacts/kronos-final-20261111/final-acceptance.log`.

| Gate | Kết quả cuối |
| --- | --- |
| `ruff check .` | PASS, All checks passed |
| Clean-import `tradingagents, cli.main` | PASS; kiểm thêm `server.main` trực tiếp cũng đạt |
| `pytest -q` với PostgreSQL dùng một lần | **4403 passed, 6 skipped, 29 deselected, 24 warnings, 100 subtests passed in 1190.77s** |
| Migration integration, Compose project riêng | **2 passed in 9.01s** |
| Web production build | PASS, Next.js 16.3.7 |
| TypeScript với generated route types | PASS |
| Web ESLint | PASS |
| Dashboard + Kronos browser trong full runner | **13 passed in 123.92s**, không skip |
| Shadow model thật | **8 passed in 24.79s** |
| Active Kronos model thật | **4 passed in 15.50s** |
| Runner summary | **full run OK** |

Sáu skip được nêu cụ thể trong log: ba ca POSIX file modes, thiếu optional `langchain_aws`, hai ca POSIX upgrade commands. Integration bị deselect bởi pytest mặc định; runner gọi riêng migration, browser và model smoke bắt buộc, không suy ra mọi live-API test đã chạy.

Full pytest được collect trước khi chuyển layout test. Production behavior được kiểm trong full run; layout test mới được kiểm lại riêng qua 87 test ở trên. Scenario browser serverAhead bổ sung sau lượt full đã được chạy lại cùng toàn bộ 13 browser cases và đạt. Self-review tiếp theo phát hiện helper fixture nhánh POSIX dùng `killpg` khi chưa sở hữu process group riêng; helper giờ `start_new_session=True` trên POSIX và kill đúng session ID do nó tạo. Ba regression mới kiểm ownership/cleanup được mô phỏng trên Windows (fixture đầu của test cần bổ sung `SIGKILL` vì Windows không có constant này; lượt sửa đã đạt). Nhóm cuối 23 test và 13 browser đã chạy lại sau correction. Không có thay đổi logic forecast/trading sau full; chỉ test layout/scenario, lifecycle helper và docstring. Không dùng kết quả của file cũ để suy ra layout hoặc scenario mới đã đạt.

Các log bổ sung: `moved-autoloop-tests.log`, `final-browser-after-conflict.log` (13 passed/118.18s trước cleanup correction), `final-browser-lifecycle.log` (13 passed/121.42s sau correction). Kiểm cú pháp AST theo Python 3.11 cho **122 file Python tracked/untracked mới hoặc đã sửa** đạt; không bare `except:`; tracked tsconfig khớp HEAD và không còn `tsconfig.next-verify.json`.

Lượt full đầu (`full-acceptance.log`) **exit 1**: năm test còn patch/import ở owner autoloop cũ và một model-smoke fixture dùng 64 nến trong khi lookback là 96. Những test seams/fixture này đã sửa; source không có compatibility layer để che lỗi. Lượt test sau extraction đầu cũng thất bại ba ca do thiếu `_PM_DECISION`; hằng được khôi phục trước lượt sáu nhóm xanh. Giữ lịch sử lỗi để đối chiếu, không trình bày các lượt đó như đã pass.

Runner bắt buộc hai browser files, explicit `-m integration`, reject skip. Next-generated tracked tsconfig được khôi phục; build output test được dọn. Các PG verification projects đã được xóa; `docker ps -a --filter name=ta-verify` không còn container. `git diff --check` đạt.

## 6. Giới hạn còn lại

- `context_only` đưa dữ liệu forecast thật vào trước Trader/PM; chưa là direction prefilter hoặc bằng chứng tiết kiệm LLM calls/profitability.
- Model ownership scoped per-call, không warm reuse liên ứng viên; có thêm compute và prompt cost.
- Absolute deadline bao serialize/write/flush/read của pipe. Artifact checks, venue history, process startup và OS reap/cleanup có thời gian riêng; đây không phải end-to-end cycle SLA. Deadline/cancel checks vẫn cooperative, không distributed fencing.
- UI screenshots cũ là fixture; lượt này kiểm mechanism/isolation/conflict, không live runtime hoặc alpha. Hint/unit accessibility, fixture candle timestamp và thư mục rỗng có dấu cách cuối đã sửa; các chi tiết UX khác trong review cũ không được tự suy ra đã đóng.
- Reviewer agent mới bị provider từ chối trước khi có kết quả, kể cả lần thử cuối. **Không có review độc lập mới cho refactor này.** Coordinator đã tự rà source/callers/module seams và kiểm tests/build/browser/model thật; review cũ chỉ là lịch sử.
- Workflow `dwfrun-42ff3c4d-888e-4bd8-a1a0-1e4082f0632c` giữ nguyên trạng thái errored ở typed handoff cũ. Finishing pass này không amend, resume hoặc restart workflow.
