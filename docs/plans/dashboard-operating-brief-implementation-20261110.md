# Dashboard operating brief — trạng thái triển khai 2026-11-10

> **Cập nhật 2026-11-11:** các mục "CHƯA làm" ở §5 (screenshot, W1–W6,
> `next start` production, mobile header/refresh) đã được hoàn tất trong pass
> UI/nghiệm thu sau vòng review Kronos. Bằng chứng hiện hành nằm ở
> [kronos-active-ux-20261111.vi.md](../research/kronos-active-ux-20261111.vi.md)
> và ở cuối tài liệu này (§10). Các con số trong §1–§4 **giữ nguyên làm lịch sử**
> của lần triển khai 11-10; không dùng chúng làm bằng chứng hiện tại.

- **Phạm vi ask:** implement dashboard `/` theo [dashboard-operating-brief-ux-20261110.md](dashboard-operating-brief-ux-20261110.md) + [trading-research-delivery-20261110.md](trading-research-delivery-20261110.md), dùng API outcomes/experiments thực tế; sửa theo audit và chạy focused browser cho thay đổi.
- **Nhánh:** `feat/trading-research-delivery-20261110`
- **Trạng thái:** 5/5 lỗi chặn của lần review đầu đã sửa; 2 lỗi chặn mới do bản sửa sinh ra (B6, B7) đã sửa; **gate `--stage dashboard` đã xanh thật**. Các hạng mục còn lại chưa làm, ghi rõ ở §5.

## 1. Gate đã chạy trong phiên này

| Lệnh | Kết quả |
| --- | --- |
| `.venv\Scripts\python.exe scripts\verify_research_delivery.py --stage dashboard` | **exit 0 — `stage dashboard OK`**, cả 5 phần chạy thật: `brief-pytest` PASS, `web-build` PASS, `web-typecheck` PASS, `web-lint` PASS, `browser-offline` PASS |
| `.venv\Scripts\python.exe -m pytest -q tests/test_operating_brief.py` (PG disposable) | **26 passed** |
| `.venv\Scripts\python.exe -m pytest -q -m integration tests/browser/test_dashboard_brief_offline.py -rs` | **5 passed** |
| `.venv\Scripts\python.exe -m ruff check .` | **All checks passed!** |
| `node node_modules/typescript/bin/tsc --noEmit` (cwd web/) | sạch, khi `.next` có types; xem §4 về `LayoutProps` |
| `node node_modules/eslint/bin/eslint.js .` (cwd web/) | sạch |

## 2. Lỗi chặn đã sửa (đều có test giữ)

**B1 — coverage + lệnh ngoài bot biến mất khi `net_by_currency` rỗng.** `brief-card.tsx`: nhánh sớm `if (netRows.length === 0) return ...` bỏ luôn CoverageBlock + UnattributedBlock. Đã tách: chỉ **bảng net** phụ thuộc net rows; coverage và unattributed luôn render. Test: `coverage_only` (assert `coverageRows > 0`, `unattributed` block có mặt, `netRows === 0`).

**B2 — `%%`.** `exposureOf`/`limitValue` nhận chuỗi đã có `%` rồi thêm `%` lần nữa → "18.4%% of equity", "80%%". Đã đổi call site sang `formatNumber(...)` (số thuần, không ký hiệu) và ghi chú trong i18n rằng hàm tự thêm `%`. Test: assert không có `%%` và có `18.4%`, `80%`.

**B3 — label halted lặp chữ.** "Order placement halted | Halted today" → thêm `haltedYes/haltedNo` (Yes/No). Test: assert nửa đầu ≠ nửa sau của dòng.

**B4 — mất cảnh báo halt/chờ duyệt khi brief lỗi.** Thêm banner dự phòng đọc từ `/api/health`. Test: `brief_error` + health halted=True → vẫn thấy halt và "awaiting approval", và 4 bảng khác vẫn render.

**B5 — cửa sổ chờ brief xoá cả trang.** `allLoading` bỏ `brief`; `portfolioState` xử lý loading riêng. Test: brief error → các bảng độc lập vẫn render.

**B6 (mới, do bản sửa sinh ra) — banner halt lặp khi brief ĐANG hoạt động.** `showFallbackHalt` thiếu guard `!briefHasData` trong khi `fallbackPending` có → tắt 3 lần. Đã thêm guard. **Đã chứng minh test bắt được lỗi này**: revert tạm dòng đó và chạy `-m integration "…[halted]"` → FAIL đúng thông điệp `B6: halt banner shown while the brief still has its attention list`; khôi phục → PASS.

**B7 (mới) — test nghiệm thu 5/5 đỏ.** Fixture của chính test trả `/api/performance/fills` thiếu trường bắt buộc `warnings` (+ `source`, `coverage`, `venue_profile`), `performance-history.tsx:129` đọc `history.warnings.length` → throw → cả cây unmount → brief không bao giờ `ready`. Đã làm fixture khớp `PerformanceHistoryData` đầy đủ. Test nay **5 passed**.

## 3. Lỗi nhỏ đã sửa

- Drift: server trả **phân số** (`coverage.py:79`), UI đang render thẳng thành `%` → `0.004` hiện "0.0%". Đã ×100 (`driftRatio`) → "0.4%"; ratio null → "Lệch đối soát chưa xác định" (`driftUnknown`).
- Caption trùng heading: thêm `verifiedCaption` riêng cho `<caption class="sr-only">`.
- Label protection: bỏ mẹo `r.protectionConfigured("").trim()` → `protectionConfiguredLabel/MissingLabel/UnknownLabel` + `protectionCount`.
- `dataAtExchange`: footer giờ hiện **cả** `generated_at` (brief dựng) **và** `risk.positions_read.observed_at` (lúc đọc sàn) — trước đó key này bị bỏ rơi.
- Note "Futures path only" xuất hiện 2 lần khi scope=Spot → bỏ khỏi Results.
- Trang giới thiệu sai: h1/meta đổi thành "Tổng quan vận hành" / "Operations overview".
- **Scope filter tới được vị thế**: `PortfolioCard` nhận `scope`, ẩn block Spot/Futures bị loại. Test: scope Spot → `positions` table từ 2 còn 1; All → về 2.
- **Bỏ lặp vị thế**: `PerformanceCard` thôi render `PerformancePositions` (trước đó bảng Futures + KPI tài khoản in 2 lần trên một màn). Test: assert đúng 2 bảng vị thế (spot + futures).

## 4. Hạ tầng gate — hai lỗi false-green tìm thấy và đã sửa

1. **`brief-pytest` PASS trong khi 26/26 test SKIP.** `postgres_schema` skip khi thiếu DSN; pytest vẫn exit 0 nên stage in PASS trên đúng 0 test đã chạy. `_run_dashboard_stage` nay chạy brief suite **trong disposable PG với `reject_skips=True`**; summary được in (`[pytest] 26 passed`). `reject_skips` cũng báo lỗi trung tính (`[pytest] FAIL`) thay vì nhãn `[browser]` cứng.
2. **`web-lint` FAIL khi `.next-verify` tồn tại.** `globalIgnores` của `eslint.config.mjs` chỉ liệt `.next/**`; flat config **không đọc `.gitignore`**, nên lint quét cả build output. Đã thêm `.next-verify/**` vào `globalIgnores` **và** vào `.gitignore`.
3. **`web-typecheck` không chạy thật.** `tsc --noEmit` trần trên cây sạch báo `Cannot find name 'LayoutProps'` — global do Next sinh theo route, chỉ có sau build. Typecheck nay chạy **sau** build, dùng tsconfig tạm extends tsconfig của app + include `.next-verify/types`; thiếu types ⇒ FAIL rõ ràng thay vì kết quả vô nghĩa.
4. **`next build` tự sửa `web/tsconfig.json`** (thêm đường dẫn `.next-verify`). Đây là churn của tool, không thuộc thay đổi đang test → `_web_build()` khôi phục nguyên trạng và báo khi đã khôi phục. Cleanup `.next-verify` chuyển sang **sau** browser check (browser phục vụ chính output đó).

## 5. CHƯA làm

| Hạng mục | Trạng thái |
| --- | --- |
| `verify --full` | **Chưa chạy** |
| Screenshot desktop/mobile | **Chưa chụp** — không có ảnh nào |
| Báo cáo UX W1–W6 | **Chưa chạy**; các số đo vòng trước (target ≥44px, contrast light 4.74:1) là của reviewer, không phải của phiên này |
| `web/build` với `next start` serve | Browser test dùng `next dev`, không phải production server |
| Kiểm screen reader thật, Safari/Firefox, axe/Lighthouse | **Chưa** |
| Chạy trên PostgreSQL + store thật (app `:3000/:8000`) | **Chưa** — image đang chạy là image cũ, `/api/brief` 404 |
| Lỗi nhỏ còn lại của reviewer | **Chưa**: mobile header vỡ (152px×140px), "Cần xử lý" dưới fold mobile (y=545), 5 nút Refresh, không có skip link |

## 8. Rà integration hạng 4 (shadow/Kronos) — 2026-11-10

Stage 4 đã landed đầy đủ (không phải stub): `tradingagents/shadow/{provider,filters,kronos,recorder,evaluation,kronos_runner}.py`, `server/{shadow_store,shadow_worker,shadow_api}.py`, migration `0010_shadow_forecasts.sql`, 5 file test + `scripts/replay_shadow_observations.py`. Brief **đã** tích hợp sẵn: `_shadow_section()` đọc `shadow_store.summary()`, và card web render per-provider counts + ghi chú calibration.

**Bằng chứng đo trong phiên (không suy đoán):**

| Lệnh | Kết quả |
| --- | --- |
| `.venv\Scripts\python.exe -c "from tradingagents.shadow.kronos import KronosProvider; p=KronosProvider(); print(p.unavailable_reason())"` | **`None`** — Kronos CÀI THẬT ở `%LOCALAPPDATA%\tradingagents-research\kronos-venv` |
| `verify_artifacts(isolated_source_dir(), isolated_model_dir())` | không có problem; 5 hash khớp (Kronos-small, Tokenizer-base, model/*.py) |
| `from server.shadow_worker import observations_enabled, providers` | `enabled: False`, `providers: ['filter:donchian_breakout']` |
| `.venv\Scripts\python.exe -m pytest -q tests/test_shadow_forecasts.py tests/test_shadow_isolation.py` | **50 passed** — bất biến no-decision-impact còn nguyên |

**Lỗi tìm thấy và sửa (UI/wording, không đụng forecast logic):**

Ba trạng thái "chưa có gì" của mục Quan sát dùng **chung một câu** `shadowUnavailable` = "Chưa bật — chưa có dự báo nào được ghi." Server phân biệt ba lý do (`server/operating_brief.py:526` tắt, `server/shadow_store.py:313` bật-nhưng-rỗng, `operating_brief.py:537` đọc lỗi) nhưng UI gộp thành một. Hệ quả: operator **đang bật** worker nhưng chưa có dòng nào sẽ được bảo là "chưa bật" — **nói sai cấu hình của chính họ**. Đã sửa:

1. Backend: `_shadow_section()` thêm trường `state` ∈ `ok | disabled | empty | error` (ba nhánh unavailable trả ba state khác nhau).
2. `web/components/overview/brief/contract.ts`: `BriefShadow.state` (optional → payload cũ rơi về câu trung tính, không mặc định "đã tắt").
3. i18n vi/en: tách `shadowDisabled` / `shadowEmpty` / `shadowReadFailed` (+ giữ `shadowUnavailable` làm fallback).
4. `brief-card.tsx`: hàm `shadowStateCopy()` chọn câu theo `state`; block có `data-shadow-state` để test được.

**Test giữ lỗi này:**
- `tests/test_operating_brief.py`: +3 test — `state == "disabled"` khi tắt; `state == "empty"` + **không** chứa `shadow_observations_enabled is False` khi bật-nhưng-rỗng; `state == "error"` khi store ném. → **29 passed** (trước: 26).
- `tests/browser/test_dashboard_brief_offline.py`: +4 scenario (`shadow_disabled`, `shadow_empty`, `shadow_error`, `shadow_kronos_ready`) assert **từng state ra câu riêng** và phủ định câu sai (`EMPTY state wrongly told the operator observation is off`). → **9 passed**.
- **Đã chứng minh test bắt được lỗi**: đổi tạm nhánh `empty` sang copy `shadowDisabled` → FAIL đúng `empty copy missing: … Observation is off (disabled in Settings) …`; khôi phục → PASS.

**Giữ nguyên theo ask:** hierarchy và progressive disclosure không đổi (vẫn trong khối Hoạt động của brief); trạng thái "actual provider unavailable" vẫn hiển thị trung thực từ `kronos.available` + `reason` thật, không hardcode; **không đụng forecast logic** (`tradingagents/shadow/*` chỉ đọc, không sửa file nào).

## 9. Rủi ro mở (trung thực)

1. **`exposure` sẽ là null trên sàn có notional currency khác equity currency** (ví dụ OKX) — đúng hợp đồng "không quy đổi FX", nhưng nghĩa là ô này thường xuyên "chưa xác định".
2. **`heat` và `protection` chủ yếu "chưa xác định"** kèm lý do. Trung thực nhưng giá trị vận hành thấp — cần user quyết có chấp nhận, hay cần bổ sung binding vị thế ↔ lệnh vào.
3. **B4 phụ thuộc `/api/health` báo halt.** Nếu cả health lẫn brief cùng lỗi, không còn nguồn nào để nói về halt.
4. **Mobile header vỡ và "Cần xử lý" nằm dưới fold** (số đo của reviewer) — chưa sửa trong phiên này.
5. Toàn bộ lớp web mới chỉ được kiểm bằng browser test offline (fixture) + typecheck/lint/build; **chưa** render với PostgreSQL/store thật.

## 7. Không có ảnh chụp

Ask yêu cầu "screenshots refs trong report". **Không có ảnh nào được tạo trong phiên này**, nên để trống thay vì trỏ tới ảnh không tồn tại.

> **Cập nhật 2026-11-11:** mục này đã đóng — ảnh desktop/mobile thật đã chụp và
> W1–W6 đã đo; xem §10 và
> [kronos-active-ux-20261111.vi.md](../research/kronos-active-ux-20261111.vi.md).

## 10. Cập nhật 2026-11-11 — bằng chứng hiện hành (tách khỏi kết quả 11-10)

Toàn bộ các mục "CHƯA làm" của §5 nay đã đóng trong pass UI/nghiệm thu (ask
kronos-active-review-20261111 §UI). **Bằng chứng cũ ở §1–§4 là của lần triển
khai 11-10, chạy trên fixture `next dev` 5 scenario — không còn mô tả trạng
thái hiện tại.** Bằng chứng hiện hành:

| Hạng mục §5 cũ | Trạng thái 11-11 | Bằng chứng (đã chạy thật trong phiên) |
| --- | --- | --- |
| Screenshot desktop/mobile | **Xong** — fixture production, ảnh lưu `.zcode/artifacts/trading-delivery-20261111/` | `desktop-overview.png`, `desktop-forecast-open.png`, `mobile-overview.png`, `measurements.json` — tạo bởi `scripts/capture_dashboard_ux_evidence.py` |
| Báo cáo UX W1–W6 | **Xong** — đo thật trên Chrome/157 | W1: 15/15 target ≥44px cả hai viewport; W2: 4.73:1 light (khớp audit 11-10), 7.63:1/6.91:1 dark; W3: h1=1, h2=6, không skip level, ring focus visible; W5: không overflow ngang, header wrap; W6: heat null → "Unknown", không 0 |
| `next start` production serve | **Xong** — fixture build thật + `next start` | `pytest -m integration tests/browser/test_dashboard_brief_offline.py` → **11 passed in 349.21s** (trước: 5 scenario trên `next dev`) |
| Mobile header vỡ, "Cần xử lý" dưới fold, 5 nút Refresh | Header wrap (`flex-wrap`), status row không tràn; Refresh còn **2** (primary + Hoạt động, đo được) | `measurements.json`: `pageHorizontalOverflow: false`, `refreshButtons: 2`, card-level refresh chỉ còn "Retry" khi lỗi/stale |
| `verify --full` | **Vẫn chưa chạy** — là việc của script runner sau khi source ổn định (ask cấm chạy full acceptance trong pass này) | — |
| Screen reader thật, Safari/Firefox, axe/Lighthouse | **Vẫn chưa** | — |
| Render với PostgreSQL/app thật `:3000/:8000` | **Vẫn chưa** — mọi bằng chứng trên fixture test-owned | — |

Thêm trong pass này (mới so với 11-10, phục vụ review Kronos §UI):

1. **Block "Kronos forecast (latest recorded cycle)"** — section riêng tách
   khỏi shadow rows, compact disclosure đóng mặc định; 11 scenario browser
   gồm `forecast_off`/`forecast_stale`; server `active_forecast` từ
   `server/operating_brief.py` (đọc cycle đã persist, stale khi cửa sổ hết hạn,
   block lỗi không số). Không có chữ tiết kiệm LLM call hay claim PnL.
2. **Positions dedup hoá thân hoàn chỉnh**: assertion của component đã xoá
   chuyển sang `PortfolioCard` (precision full-digit, quantity âm, observed_at,
   issues/skipped per-symbol, unknown currency, stale) — **không test nào bị
   bỏ kiểm chứng**; thêm test pin dedup. `tests/test_performance_ui_render.py`
   → **29 passed**.
3. **Mojibake `docs/ui-api-contract.md` khôi phục**: diff từ 507+/421− về
   **87+/1−** — chỉ còn §16–§19 thật (outcomes/experiments/brief/shadow),
   tiếng Việt cũ trả về nguyên trạng, BOM bỏ.
4. Runner dashboard stage nay gộp `tests/test_performance_ui_render.py` và in
   summary số test thật cho từng phần (`_PYTEST_SUMMARIES`), giữ nguyên quy tắc
   reject-skips/full-output.

Bằng chứng chi tiết và ảnh: [kronos-active-ux-20261111.vi.md](../research/kronos-active-ux-20261111.vi.md).
