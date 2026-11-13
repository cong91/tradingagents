---
purpose: Bàn giao khôi phục từ các bước đã hoàn tất; workflow lỗi ở typed result cuối, không chạy lại workflow
branch: feat/trading-research-delivery-20261110
base_commit: 0943103
date: 2026-11-11
workflow_run: dwfrun-42ff3c4d-888e-4bd8-a1a0-1e4082f0632c
status: hoàn tất code local và finishing pass; full runner exit 0; workflow vẫn errored tại bàn giao cũ; chưa deploy hoặc bật runtime
---

# Bàn giao bốn hạng mục và Kronos tham gia quyết định

> **Cập nhật 2026-11-12 — nội dung lịch sử được thay thế:** Các mô tả bên dưới về `GET /api/shadow`, worker/switch Shadow, switch `kronos_active_enabled` và policy `pass_through` phản ánh delivery ngày 2026-11-11, không còn là hành vi hiện tại. Kronos luôn được thử cho ứng viên Futures đủ điều kiện; thiếu hoặc lỗi forecast khiến ứng viên được hoãn trước graph/LLM. Dữ liệu lịch sử được giữ nguyên. Xem [hợp đồng API hiện tại](../ui-api-contract.md) và [hướng dẫn vận hành](../operator-guide.md).

Code của ledger/mô phỏng futures, experiments, dashboard và Kronos làm đầu vào trước Trader/Portfolio Manager đã được lưu trên nhánh hiện tại. Run `dwfrun-42ff3c4d-888e-4bd8-a1a0-1e4082f0632c` đã đi qua phần kiểm chứng tổng thể, rồi lỗi ở lời gọi bàn giao cuối vì agent kết thúc mà không gọi `submit_result`. Workflow vẫn có trạng thái **errored**; tài liệu này khôi phục phần bàn giao từ code, báo cáo và trạng thái các bước đã lưu, không thay đổi hay chạy lại workflow.

Sau đó người dùng duyệt hoàn thiện bốn gap và tách forecast/autoloop. Finishing pass ngày 2026-11-11 đã đóng các điểm đó bằng code và regression; full runner trực tiếp **exit 0**. Kết quả, ranh giới module và các giới hạn kiểm chứng mới nằm tại `docs/research/kronos-final-completion-20261111.vi.md`; §5–6 dưới đây đã cập nhật theo lượt này.

Không deploy, không bật Auto hoặc Kronos trên runtime, không đổi venue/risk limits, không gửi lệnh sàn, không commit/push. Các ảnh và thử nghiệm UI là fixture; số liệu model smoke chỉ chứng minh cơ chế hoạt động, không chứng minh lợi nhuận. Đây là công cụ nghiên cứu, không phải tư vấn tài chính.

## 1. Những gì được giao

| Hạng mục | Đường dùng và trách nhiệm | Bằng chứng đã lưu |
| --- | --- | --- |
| Sổ kết quả ròng | `tradingagents/outcomes/`, `server/outcome_store.py`, `server/outcome_ledger.py`, migration `0008_outcome_ledger.sql`; ghép cycle/order/fill/exit theo bằng chứng, phí và funding theo từng đồng tiền, coverage và cursor bền vững, ghi idempotent | `docs/plans/trading-research-delivery-20261110.md` §13: gate ledger cuối **112 passed**, các probe độc lập cho attribution, gap fills, fee semantics, funding known-zero/unknown |
| Mô phỏng futures có chi phí | `tradingagents/simulation/`; dùng risk/sizing/profit protection/scale-out hiện có, nến đóng, gap fills và thứ tự cập nhật stop rõ ràng, chính sách OHLC mơ hồ, fee/funding/slippage tránh đếm đôi | Cùng báo cáo §13: parity tests, tests gap-through-stop/target, không dùng stop vừa nâng ở cuối nến để khớp ngược trong nến |
| So sánh phiên bản | `tradingagents/experiments/`, `server/experiment_store.py`, `server/experiments_api.py`, migrations `0009` và `0011`; walk-forward, purge/embargo, baseline cùng engine/cost, paired repeats, manifest và provenance khóa, sandbox dữ liệu | `docs/plans/trading-research-delivery-20261110.md` §14; sửa hợp đồng `metric_name` và comparison registration trong `docs/research/kronos-active-implementation-20261110.vi.md`: gate experiments **59 passed** |
| Dashboard và bản tin vận hành | `GET /api/brief` và `web/components/overview/brief/`; ưu tiên việc cần xử lý, tách số đã đối soát/ước tính, filter Spot/Futures, một chủ sở hữu bảng vị thế, lịch sử lệnh rút gọn có drilldown, trạng thái riêng từng nguồn | `docs/research/kronos-active-ux-20261111.vi.md`: **11 passed** browser production fixture, **29 passed** render; review DOM độc lập tại `docs/research/kronos-active-ux-review-20261111.vi.md` |
| Kronos active và quan sát | Neutral core `tradingagents/forecast/`, seam `server/kronos_context.py`, wiring nhỏ trong autoloop; dự báo vào `execution_context` trước graph, prompt Trader và PM đọc thật; shadow worker/evaluation độc lập | `docs/research/kronos-active-repairs-20261111.vi.md`: gate Kronos **137 passed + 4 smoke model thật**; review độc lập tại `docs/research/kronos-active-code-review-20261111.vi.md` |

Các số test trong bảng là kết quả các lượt gate/focused được ghi tại báo cáo tương ứng, không phải tổng số test của full run cuối.

## 2. Kronos thực sự tham gia ở đâu

Đường Futures crypto chuẩn bị account/market context và lịch sử nến đóng → khi được bật, xác minh model/source đã pin và tạo dự báo local → ghi forecast/policy vào cycle → nối dự báo vào `execution_context` → dựng graph → Trader và PM nhận cùng block dữ liệu trước khi quyết định → các cổng authorization, confirmation, RiskGuard, sizing và bracket/protection hiện có tiếp tục kiểm tra.

Mode hiện hành là **`context_only`**: dự báo là đầu vào thật của chiến lược, nhưng không loại ứng viên theo hướng lên/xuống. Hard direction gate 0.70 không được bật vì các phép thử đã ghi chưa đủ để biện minh cho quyết định đó. Sample ratio luôn đi cùng số đường và nhãn không phải xác suất thắng đã hiệu chỉnh.

Failure policy đã có tác dụng thực:

- `defer` (mặc định): model thiếu/lỗi/timeout/dữ liệu không dùng được → persist lý do và dừng ứng viên trước graph/LLM.
- `pass_through`: tiếp tục với trạng thái thiếu dự báo ghi rõ trong context; không coi đó là kiểm tra đã đạt.
- Tắt tính năng: không đọc lịch sử/model cho forecast và giữ context cũ.

Đây không phải prefilter theo hướng để giảm số lượt gọi LLM. Khi model hoạt động bình thường, context-only thêm compute và token prompt. Ownership hiện chọn **scoped per call**: service thuộc call được đóng trong `finally` cả thành công/lỗi/timeout; không có warm reuse giữa ứng viên. Benchmark model pinned trên CPU và fixture 96 nến 4h/16 path/T=0.5/horizon 24h ghi **5.6–7.1 giây mỗi ứng viên** gồm cold start. Đó là số đo cơ chế trên máy đo, không phải SLA hay bằng chứng alpha.

Các lỗi đã sửa và được review tái hiện lại: nến stale/sai symbol, OHLC/volume không hợp lệ, cache thiếu nội dung nến/venue/cutoff, nhận thiếu path/sai horizon/sai symbol, child không kill/reap đúng, preflight hash bị bỏ qua ở đường active, và policy defer chỉ ghi nhãn mà không dừng ứng viên. Finishing pass còn sửa lookback >200 và đặt một absolute deadline cho serialize/write/flush/read, với child không đọc stdin và delayed-write regressions. Artifact/history/startup/cleanup vẫn có thời gian riêng; cancellation và các checks trước/sau inference vẫn cooperative.

## 3. Tìm tính năng trên giao diện

Trên **Tổng quan `/`**, bản tin vận hành có việc cần xử lý, trạng thái bot, kết quả/rủi ro, hoạt động và thí nghiệm. Khối **dự báo Kronos của cycle được ghi gần nhất** tách khỏi quan sát shadow, chi tiết đóng mặc định: status/policy, mã, thời điểm nến/cutoff/tạo/ghi, horizon, model revision, số path và tỷ lệ mẫu, band có tiền tệ, wall latency. Forecast cũ được gắn nhãn lịch sử; ô chưa đủ dữ liệu giữ unknown, không thay bằng 0.

Trong **Cấu hình → Mô hình AI (`/settings#ai`)**, nhóm Kronos có managed switch riêng và tuning nâng cao. Nhóm này phân biệt với Auto Spot/Futures. Công tắc dùng `/api/operator-config` với confirm/reason/audit; 9 key tuning dùng `/api/settings`, persist PostgreSQL và validate trước mutation. Nơi hiển thị readiness chỉ đọc artifact status, không chạy model.

Đường code này đã hiện thực local; runtime đang chạy chưa được deploy nên tài liệu không khẳng định các màn mới đã xuất hiện tại server production.

Ảnh đã lưu, tất cả là **fixture**:

- `.zcode/artifacts/trading-delivery-20261111/desktop-overview.png`
- `.zcode/artifacts/trading-delivery-20261111/desktop-forecast-open.png`
- `.zcode/artifacts/trading-delivery-20261111/mobile-overview.png`
- `.zcode/artifacts/trading-delivery-20261111/measurements.json`

Review độc lập trước finishing pass xác nhận production fixture thật (`next build --webpack` + `next start`), target tương tác ≥44px, không overflow ngang ở viewport mobile đo, headings dashboard có thứ bậc, heat/funding unknown không bị biến thành zero, bảng vị thế không lặp. Các fix mới về fixture, browser Kronos và hint/unit accessibility có regression riêng; những chi tiết UX còn lại vẫn được ghi ở §6.

## 4. API và CLI có thể sử dụng sau khi chạy code mới

| Surface | Công dụng |
| --- | --- |
| `GET /api/outcomes` | Kết quả ledger theo từng đồng tiền, coverage và lý do thiếu dữ liệu; chỉ đọc |
| `GET /api/experiments` và `GET /api/experiments/{run_id}` | Registry/manifest/per-repeat/comparisons; chỉ đọc |
| `GET /api/brief` | Bản tin aggregate, danh mục và bằng chứng active/shadow cho dashboard |
| `GET /api/shadow` | Quan sát, provider counts và evaluation; đọc không tự gọi model |
| `GET /api/operator-config` | Công tắc managed, gồm `kronos_active_enabled`, cùng readiness mô hình |
| `PUT /api/operator-config` | Ghi managed switch có confirm/reason/audit; chưa thực hiện trên runtime |
| `GET/PUT /api/settings` | Engine tuning persist PostgreSQL; Kronos domain invalid → lỗi per-field trước khi áp dụng |
| `tradingagents outcomes` / `tradingagents experiments` | Đọc ledger/registry với DSN của instance do operator chủ động chọn |
| `python -m tradingagents.experiments.financial_experiment --help` | CLI mô phỏng trên OHLCV được nhập, holdout và chi phí; `--register-run` là ghi registry được chọn rõ |
| `python -m tradingagents.experiments.replay_experiment --help` | Replay quy tắc trên quyết định đã ghi; không chạy lại agent LLM |
| `python -m tradingagents.experiments.report <run_dir>` | Xác minh manifest và đọc artifact |

Dùng `.venv\Scripts\python.exe` cho các lệnh Python của repo. `scripts/build_candle_fixture.py` tạo fixture có nến dẫn/gap tổng hợp: fixture này chỉ kiểm đường chạy, không phải lịch sử thị trường thật để kết luận lợi nhuận. Entry biến thể LLM `tradingagents.experiments.run` vẫn exit 2 có hướng dẫn; không có rerun LLM hoặc chi phí LLM ngầm trong delivery này.

## 5. Kiểm chứng cuối và lỗi workflow

Run được đổi model qua GUI sang `vc-cheap/deepseek-v4.1-flash-1017$max`, giữ cùng script và cache. `GetWorkflowRun` ngày 2026-11-11 trả:

- `status=errored`, `error=DriverError: The typed ask ended without a submit_result call, so there is no result.`
- Agent lỗi: **Kỹ sư chốt nghiệm thu hệ thống**, ở phase **Bàn giao bốn hạng mục với bằng chứng cuối**.
- Phase kiểm chứng tổng thể có 3 bước settled; sau đó workflow đã vào ask bàn giao cuối.

Đối chiếu script đã chạy (`.zcode/workflow-drafts/Nâng-cấp-tuần-tự-dữ-liệu-kiểm-chứng-dashboard-UX-và-bộ-lọc-quan.dwf.ts:124-138`): chỉ được tới ask bàn giao khi `verify_research_delivery.py --full`, ruff và `git diff --check` đều exit 0; mọi nonzero còn lại đều throw trước đó. Vì vậy **run đã qua các điều kiện gate trước khi lỗi bàn giao**, nhưng snapshot công cụ không cung cấp stdout từng command hoặc tổng passed/skipped của full run cuối. Tài liệu này không bịa những số đó và không gọi workflow completed.

Kiểm lại trực tiếp sau notification, không chạy lại workflow:

| Lệnh | Kết quả |
| --- | --- |
| `.venv\Scripts\python.exe -m ruff check .` | exit 0, `All checks passed!` |
| `.venv\Scripts\python.exe -c "import tradingagents, cli.main"` | exit 0, không lỗi import |
| `git diff --check` | exit 0, không output lỗi whitespace |
| `docker ps -a --filter name=ta-verify` | không còn container verification khớp prefix tại thời điểm kiểm |

Full runner bao gồm pytest với PG disposable, migration integration, web build/typecheck/lint, offline browser production fixture và smoke model shadow/active. Pytest mặc định của repo deselect integration; runner gọi riêng các integration bắt buộc. Không suy ra mọi integration/live-API test của repo đã chạy chỉ từ `--full`.

Bản bàn giao này là file markdown do phiên chính khôi phục sau lỗi typed result. Lệnh `artifact.file("handoff", ...)` ở workflow nằm SAU ask lỗi nên chưa chạy; do đó chưa có thẻ handoff của workflow. Chỉ có hai artifact cũ (progress và nghiên cứu Kronos). Không amend, resume hoặc restart theo chỉ dẫn GUI gần nhất.

Finishing pass được người dùng duyệt sau đó chạy trực tiếp, giữ nguyên workflow. Command `.venv\Scripts\python.exe -u scripts\verify_research_delivery.py --full` đã **exit 0**; log `.zcode/artifacts/kronos-final-20261111/final-acceptance.log` có summary `full run OK`:

| Gate cuối | Kết quả |
| --- | --- |
| Pytest mặc định với PG disposable | **4403 passed, 6 skipped, 29 deselected, 24 warnings, 100 subtests passed**, 1190.77s |
| Migration integration | **2 passed**, 9.01s |
| Web build / typecheck / lint | **PASS** |
| Production browser dashboard + Kronos trong full runner | **13 passed**, 123.92s; không skip |
| Model thật shadow / active | **8 passed / 4 passed**, 24.79s / 15.50s |
| Ruff / clean imports / diff whitespace | **PASS** |
| Sáu nhóm test autoloop sau tách layout | **87 passed**, 79.14s, PG riêng |
| Browser chạy lại sau Kronos serverAhead và correction POSIX fixture cleanup | **13 passed**, 121.42s, không skip |
| Fixture ownership + runner/accessibility/forecast focused cuối | **23 passed**, 3.40s; ba ca mới kiểm session/process-group sở hữu riêng |

Sáu skip gồm ba POSIX file-mode cases, hai POSIX upgrade-command cases và optional `langchain_aws` chưa cài. Full pytest collect trước khi đổi layout test; 68 functions được chuyển nguyên AST và sáu nhóm đã kiểm lại riêng. Browser serverAhead bổ sung sau full cũng đã chạy lại đầy đủ selection. Không cộng các số test lặp thành một tổng mới. Container PG test được dọn; không dùng runtime DSN. Báo cáo hoàn thiện ghi cả lượt full đầu thất bại và nguyên nhân test seam/fixture đã sửa.

## 6. Đã đóng sau phê duyệt và giới hạn còn lại

| Khoảng trống đã giao ở bản bàn giao đầu | Trạng thái finishing pass 2026-11-11 |
| --- | --- |
| Fixture mở thủ công | **Đã đóng:** default state + badge, Next fixture config không rewrites; manual scenario và upstream trap chứng minh không rơi về API runtime. |
| Browser riêng cho Kronos | **Đã đóng:** đúng key `kronos_active_enabled`; enable/confirm/reason/Save/readback/Undo/disable, domain 422, serverAhead xóa authorization cũ và khóa stale write. Auto modes không đổi. |
| Lookback >200 | **Đã đóng:** history request theo configured lookback 96/201/256/512; không silent cap, thiếu số nến được từ chối rõ. |
| Deadline khi ghi pipe | **Đã đóng trong phạm vi pipe:** absolute serialize/write/flush/read deadline, kill/reap/join/close; blocked-write và delayed-write child regressions đã đạt. |
| Forecast/service nhiều vai trò | **Đã duyệt và tách:** settings, block, inputs, cache, rendering là owner riêng; service điều phối. Caller/import/test dùng owner trực tiếp. |
| Autoloop nhiều vai trò | **Đã duyệt và tách:** API, scheduler, candidates, analysis, execution guards, futures context, cycle records và futures execution. Test monolith tách sáu nhóm tương ứng; không compatibility shims. |
| Một số chi tiết UX/fixture | **Đã sửa và kiểm:** hint/unit/error `aria-describedby`, domain-error mapping; fixture candle timestamp sửa đúng năm; thư mục sinh rỗng có dấu cách cuối đã dọn riêng. |

Các giới hạn còn lại:

1. **Kiểm chứng độc lập:** reviewer agent mới bị provider từ chối trước kết quả; refactor mới có coordinator self-review và tests/build/browser/model smoke, chưa có independent review mới. Các review cũ vẫn là lịch sử.
2. **Deadline và hủy:** pipe deadline không bao hết artifact checks, venue history, process startup hoặc cleanup OS. Cancellation và claim/write guards cooperative; không atomic database fencing hoặc distributed revocation.
3. **Chi tiết UX ngoài scope fix:** off-state link Settings, một số band-spread/unit, shadow heading/calibration và copy/plural trong review UX cũ chưa được nghiệm thu lại từng điểm. Không suy từ browser xanh rằng mọi chi tiết review cũ đã đóng.
4. **Dữ liệu và thí nghiệm:** bản export quyết định cũ không đủ future candles/terms cho mọi cycle; không hồi tố gán PnL. `soft_cap_enabled`/`quick_model` ablation còn giới hạn/no-op như báo cáo experiments; chưa phải so sánh model thực. Heat và một số protection evidence còn unknown. Chưa đủ prospective data để chứng minh alpha Kronos; model training cutoff chưa xác minh.
5. **Vận hành:** chưa deploy/apply migrations runtime, bật ledger/shadow/Kronos hoặc Auto, chưa kiểm code mới với runtime store/venue thật. Không PnL nào được quy cho Kronos. Screenshots là fixture.

Bốn gap và các split đã được hoàn tất trong code local theo phê duyệt sau bàn giao đầu. Workflow cũ giữ trạng thái errored; không amend/resume/restart. Báo cáo `kronos-final-completion-20261111.vi.md` là bằng chứng chi tiết cho finishing pass, không sửa lại lịch sử workflow.

## 7. Tài liệu bằng chứng

- `docs/research/2026-11-10-carm1nee-ai-trading-stack-report.vi.md` — báo cáo nguồn và ánh xạ ý tưởng.
- `docs/plans/trading-research-delivery-20261110.md` — kế hoạch và bản ghi triển khai từng phần; một số đoạn là lịch sử trước khi có active Kronos.
- `docs/research/kronos-trading-integration-20261110.vi.md` — nghiên cứu vai trò forecast/prefilter và các phép đo.
- `docs/research/kronos-active-review-20261111.vi.md` — lỗi gốc tái hiện.
- `docs/research/kronos-active-repairs-20261111.vi.md` — sửa và focused/stage evidence.
- `docs/research/kronos-active-code-review-20261111.vi.md` — kiểm tra độc lập correctness.
- `docs/research/kronos-active-ux-20261111.vi.md` — implementation/ảnh và phép đo fixture.
- `docs/research/kronos-active-ux-review-20261111.vi.md` — rendered review độc lập và khoảng trống tại thời điểm review.
- `docs/research/kronos-final-completion-20261111.vi.md` — finishing pass đã duyệt: bốn gap, module/test split, full runner exit 0 và giới hạn còn lại.
