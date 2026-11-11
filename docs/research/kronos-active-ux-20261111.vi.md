---
purpose: UX acceptance evidence — dashboard operating brief + Kronos active forecast block + settings forecast section
branch: feat/trading-research-delivery-20261110
base_commit: 0943103
date: 2026-11-11
status: measured locally on a test-owned production fixture; NOT deployed, no runtime touched, no order sent
fixture: production build (`next build --webpack` + `next start`), fixture API injected — mọi ảnh/dữ liệu trong tài liệu này là FIXTURE, không phải dữ liệu runtime thật
evidence_dir: .zcode/artifacts/trading-delivery-20261111/
related: docs/research/kronos-active-review-20261111.vi.md, docs/plans/dashboard-operating-brief-ux-20261110.md, docs/plans/dashboard-operating-brief-implementation-20261110.md
---

# Bằng chứng UX nghiệm thu Kronos active — 2026-11-11

Tài liệu này đóng các mục "UI và nghiệm thu còn thiếu" của
`kronos-active-review-20261111.vi.md` §UI: screenshot desktop/mobile thật,
đo W1–W6 thật trên instance do test sở hữu, và bằng chứng trình duyệt cho block
**dự báo Kronos active** tách khỏi các dòng quan sát shadow.

**Cách dữ liệu được tạo (đọc trước khi nhìn ảnh):** mọi số liệu trong ảnh là
**fixture** — server `next start` serving một production build thật của app,
trang chèn fixture API qua script trong `<head>`; Chrome chỉ kết nối được
127.0.0.1 (`--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1`). Không
request nào ra ngoài localhost; không POST/PUT/DELETE nào được phát (test pin
"GET only"). PnL, band, latency trong ảnh là số fixture, không phải kết quả
giao dịch.

## 1. Bằng chứng acceptance trình duyệt (test, không phải đo tay)

Lệnh đã chạy trong phiên này (đúng lệnh runner dùng):

```
.venv\Scripts\python.exe -m pytest -m integration tests/browser/test_dashboard_brief_offline.py -q
→ 11 passed in 349.21s
```

Fixture nay **build production thật** và serve bằng `next start`
(`tests/browser/test_dashboard_brief_offline.py:build_fixture_root` +
`start_production_server`) — trước đây nó gọi `next dev --webpack` trong khi
runner/docs ghi "production"; khiêm khớp này đã được sửa (review §UI, mục 3).
Bundler build là `--webpack` vì node_modules của fixture là junction ra
`web/node_modules` (ngoài filesystem root) mà Turbopack build từ chối; đây là
cùng toolchain app vẫn hỗ trợ.

11 scenario phủ: brief ready, coverage-only (B1), brief lỗi 500 (B4), halted
(B6), venue production, shadow disabled/empty/error/kronos-ready (4 câu riêng
từng trạng thái), **forecast_off** (mới — công tắc tắt phải nói đúng câu của
nó) và **forecast_stale** (mới — cửa sổ hết hạn phải nói là lịch sử). Mỗi CDP
command/websocket/boot có timeout riêng (30s/20s/40s), socket + pending timers
được đóng trong `finally`, và driver tự `taskkill /T /F` đúng process-tree
Chrome mà nó spawn kể cả khi assertion fail; server Next được terminate/reap
tương tự (không đụng process nào khác).

## 2. Screenshot (fixture — không phải runtime thật)

| Ảnh | Viewport | Nội dung |
| --- | --- | --- |
| `.zcode/artifacts/trading-delivery-20261111/desktop-overview.png` | 1440×900 | Brief fold: Cần xử lý, Trạng thái, Kết quả, Rủi ro, Hoạt động + Quan sát shadow; block "Kronos forecast (latest recorded cycle)" đóng gọn |
| `.zcode/artifacts/trading-delivery-20261111/desktop-forecast-open.png` | 1440×900 | Disclosure dự báo mở: tỉ lệ mẫu kèm mẫu số, horizon 24h (6 nến), cutoff/generated/recorded, band kèm đơn vị USDT, spread, wall latency 2.512 ms, model ghim |
| `.zcode/artifacts/trading-delivery-20261111/mobile-overview.png` | 390×844 (DPR 2) | Stack dọc: header wrap, không overflow ngang ở khối brief, bảng chi tiết giữ 2D-scroll có bọc |
| `.zcode/artifacts/trading-delivery-20261111/measurements.json` | — | Số đo thô của lần chạy tạo ảnh |

Đo bằng `scripts/capture_dashboard_ux_evidence.py` — dùng cùng fixture
production với test acceptance (import `build_fixture_root` từ file test, không
nhân bản logic).

## 3. W1–W6 đo thật (Chrome/157.0.8103.38, production fixture)

Nguồn số đo: `.zcode/artifacts/trading-delivery-20261111/measurements.json`
(desktop và mobile cùng lúc chạy trong một phiên Chrome).

**W1 — target size:** 15 phần tử tương tác hiển thị mỗi viewport; **0 target
nhỏ hơn 44px** trên cả desktop lẫn mobile (`smallTargets: []` — mọi nút, link,
summary, input ≥44px nhờ `min-touch`).

**W2 — contrast & non-color:** quy đổi WCAG từ token theme thật (oklch → sRGB,
chuỗi oklab chuẩn; script đo lưu trong phiên):
`--muted-foreground oklch(0.556 0 0)` trên nền `oklch(1 0 0)` =
**4.73:1** (đạt AA cho text 14px; khớp con số 4.73:1 đo bằng L\* của vòng audit
UX 11-10 — hai phép đo độc lập cùng giá trị); dark mode **7.63:1** trên nền
và **6.91:1** trên card. Trạng thái không dựa vào màu đơn độc: P1 dùng badge
chữ "Act now", stale dùng badge chữ "Stale data", band/latency là chữ.

**W3 — keyboard/focus/semantics:** `h1 = 1`, `h2 = 6`, `h3 = 14`,
**không có mức heading nào bị bỏ qua** (`headingSkipped: null` — lỗi h2=0/h3=9
của vòng audit đã hết). Tab từ đầu trang đưa focus tới nút primary "Refresh"
và indicator hiển thị bằng ring component
(`focus-visible:ring-3 focus-visible:ring-ring/50` — box-shadow đo được
`… oklab(0.708 0 0 / …)`); global `:focus-visible` outline 2px vẫn áp cho mọi
phần tử khác không đè `outline-none`. Browser test pin đúng 1 h1, tiêu đề
section là h2, và không có mức bỏ qua trên DOM sống.

**W4 — gestures:** không có gesture mới trong pass này (chỉ click/scroll/tab);
`<details>` dùng cho disclosure là tương tác keyboard-native — N/A đúng như UX
plan §12.

**W5 — reflow 390px:** `pageHorizontalOverflow: false`; `briefOverflowBlocks:
[]` (không block nào của brief tràn ngang); chiều cao trang 6.790px, header
trạng thái wrap được (`flex-wrap`); các bảng chi tiết giữ 2D-scroll có bọc
(exception có chủ đích của UX plan §5).

**W6 — state matrix:** `heatUnknownShown: true` và `heatZeroFabricated: false`
— heat là null sẽ hiện "Unknown", **không bao giờ 0**; block lỗi dự báo không
số (browser test `forecast_off`/`unavailable` assert không `<details>` số nào);
"không có tín hiệu" và "pipeline error" vẫn hai nhóm riêng (test pin từ vòng
trước). Bốn trạng thái panel (loading/ready/stale/error) được browser test
assert từng cái.

## 4. Block "Kronos forecast (latest recorded cycle)" — tách khỏi shadow

Bằng chứng DOM sống (browser test, scenario ready):

- Đúng **một** `[data-brief-block="active-forecast"]` và đúng **một**
  `[data-brief-block="shadow"]` — hai khối riêng, tiêu đề section riêng
  (`/Kronos forecast/i` trên h3 của section); shadow row không thay thế được
  dự báo active (server ghi ở `server/operating_brief.py::_active_forecast_section`).
- Disclosure **đóng mặc định** (`forecastDetailsClosed: true` trên cả hai
  viewport) — compact progressive disclosure theo UX plan; mở ra có đủ: tỉ lệ
  mẫu kèm mẫu số (9/16), horizon (24h/6 nến), cutoff `as_of`, nến mở cuối,
  generated/recorded time, số path (16 · 1 mẫu/đường), band kèm **đơn vị tiền
  quote (USDT)**, band spread, **wall latency bao gồm cold start** (2.512 ms),
  model revision — chính xác bộ trường ask yêu cầu.
- `forecast_off` → câu riêng "Off in Settings — nothing is read, no model
  runs…"; `forecast_stale` → "the forecast window has fully elapsed … history,
  not a current sample"; block lỗi → lý do nguyên văn + "không có số dự báo".
- Không có chữ nào về **tiết kiệm LLM call** cho context_only và không có
  claim PnL nào gán cho Kronos (note khối: "context for the agents — never an
  order, a veto or a profit claim").

## 5. Settings — nhóm Kronos trong AI/forecasting

Đã hiện thực (typecheck/lint/build xanh; build là kiểm chứng compile — phần
tương tác runtime của màn Settings chưa có browser test, ghi ở §7):

- Section **"Kronos — dự báo ngữ cảnh"** nằm trong khu **Mô hình AI**
  (`settings-screen.tsx` khu `id="ai"`), tách hẳn khỏi card Auto Spot/Futures ở
  khu Tự động — công tắc riêng, badge riêng **"công tắc ngữ cảnh"** (không phải
  badge "chế độ" của Auto), và panel trạng thái riêng `data-kronos-status`.
- Panel trạng thái (từ `GET /api/operator-config` → `kronos`, server đọc cùng
  nguồn chu kỳ sẽ dùng): bật/tắt hiện tại, readiness
  (`verify_artifacts` memoized — model sẵn sàng hoặc unavailable kèm lý do thật),
  giải nghĩa **context_only**, failure policy (defer/pass_through kèm ý nghĩa),
  model ghim (repo@revision), phạm vi (timeframe/horizon/paths/T/lookback/batch/
  band). Không bao giờ tự chạy model.
- Confirm + reason + audit giữ nguyên cơ chế backend
  (`server/operator_config.py::_GATE_KEYS`): thay đổi công tắc hiện khối xác
  nhận riêng với cảnh báo "KHÔNG mở cổng đặt lệnh"; save không có
  confirm/reason → server 412 (test pin: `test_the_operator_api_refuses_an_
  unconfirmed_switch_write`).
- Draft/undo/uncertain-save/server-refresh dùng lại controller operator hiện
  có (draft giữ khi server đổi, clear xác nhận khi gate đổi trên server, nút
  "Nạp dữ liệu mới và giữ bản nháp"). Luồng confirm/reason/save/undo/đổi từ
  server bên ngoài được test trình duyệt thật
  (`tests/test_operator_settings_ui_browser.py` → **3 passed**): enable cần
  confirm + reason, reason bị khoá khi đang save, undo trả về xác nhận, và
  xác nhận bị xoá khi server thay đổi gate.
- Roundtrip enable/save/error/tuning bằng fixture PG đã chạy
  (`tests/test_kronos_decision_settings.py` → **26 passed**, gồm PUT không
  confirm → 412, PUT confirm → persist + audit + DELETE reset, tuning key
  persist, và 11 case từ chối giá trị không chạy được).
- 9 key tuning `kronos_active_*` chuyển vào nhóm **"Tham số dự báo Kronos
  (nâng cao)"** trong khu AI (`settings-layout.ts:areaForSetting`), có nhãn/
  hint vi/en; server từ chối giá trị không chạy được (422 per-field) — test
  roundtrip turn-on/turn-off/tuning bằng fixture PG đã chạy (26 passed,
  `tests/test_kronos_decision_settings.py`).
- Không secret nào hiển thị; không control nào arm Auto (kronos không thuộc
  `MODE_KEYS` — test pin).

## 6. Positions: assertion chuyển sang chủ sở hữu mới (không xoá kiểm chứng)

`tests/test_performance_ui_render.py` giờ render **PortfolioCard** (chủ sở hữu
bảng vị thế sau dedup) cho mọi case cũ của component đã xoá; **29 passed**.
Giữ nguyên: precision `0,123456789123` (helper full-precision mới
`quantityValue` — Intl 8-digit từng làm tròn mất precision), quantity âm có
dấu, observed_at (dòng "Thời điểm đọc vị thế tại sàn" khi có + alert khi thiếu),
incomplete read (status unavailable ≠ partial ≠ issue per-symbol
`scope_unverified`/`monitor_position_missing` — PortfolioCard nay render
issues + skipped như component cũ làm), đơn vị tiền chưa rõ ("đơn vị chưa rõ"
cho cột giá khi venue không báo currency), stale snapshot. Thêm test pin
dedup: `test_performance_card_does_not_duplicate_the_positions_owner`.
Audit-derived spot không bị render thành vị thế hiện tại (chỉ `venue` mới được
phép) — invariant cũ của component xoá được giữ trong chủ sở hữu mới.

## 7. Refresh giảm lặp + những gì chưa có bằng chứng

- Dashboard còn **2 nút "Refresh"** (đo được): 1 primary toàn trang + 1 của
  Hoạt động hiện tại (nguồn autoloop/daily không nằm trong refreshAll — giữ để
  có control riêng cho nguồn đó). Trước vòng này: 4–5 nút cùng lúc.
- Card Danh mục / Hiệu suất giao dịch / Bản tin chỉ còn nút **"Thử lại"** và
  chỉ khi poll lỗi hoặc stale — healthy state không còn nút trùng.
- **Browser test runtime cho màn Settings**: luồng operator card (confirm +
  reason + save + undo + đổi từ server) đã có test trình duyệt thật
  (`test_operator_settings_ui_browser.py` — 3 passed; `test_settings_
  navigation_guard_browser.py` — 2 passed). **Chưa có** scenario browser riêng
  cho đúng cặp key `kronos_active_enabled` trên màn Settings — luồng máy đã
  được pin qua test API + test trình duyệt của cùng cơ chế, nhưng scenario
  riêng cho key này chưa viết trong pass này (không fake pass).
- **Chưa chạy**: screen reader thật, Safari/Firefox, axe/Lighthouse, render với
  PostgreSQL/app thật `:3000/:8000` — giữ nguyên trạng thái "chưa" của report
  triển khai, không suy diễn.

## 8. Lệnh đã chạy trong phiên này (tóm tắt bằng chứng)

| Lệnh | Kết quả |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -m integration tests/browser/test_dashboard_brief_offline.py -q` | **11 passed in 349.21s** (production fixture) |
| `.venv\Scripts\python.exe -m pytest tests/test_performance_ui_render.py -q` | **29 passed** |
| `.venv\Scripts\python.exe -m pytest tests/test_operating_brief.py tests/test_kronos_decision_settings.py -q` (PG disposable) | **61 passed** |
| `.venv\Scripts\python.exe -m pytest tests/test_operator_settings_ui_controller.py tests/test_settings_navigation_ui.py tests/test_settings_api.py -q` (PG disposable) | **117 passed** |
| `.venv\Scripts\python.exe -m pytest tests/test_operator_settings_ui_browser.py tests/test_settings_navigation_guard_browser.py -q` | **3 passed** |
| `cd web && NEXT_DIST_DIR=.next-verify node node_modules/next/dist/bin/next build` | build OK (TS pass) |
| `node node_modules/typescript/bin/tsc --noEmit -p tsconfig.next-verify.json` | sạch |
| `node node_modules/eslint/bin/eslint.js .` | sạch |
| `.venv\Scripts\python.exe -m ruff check .` | All checks passed |
| `git diff --stat docs/ui-api-contract.md` sau khi khôi phục mojibake | **87 insertions(+), 1 deletion(-)** — chỉ còn §16–§19 thật, không còn churn 507/421 |
| `scripts/capture_dashboard_ux_evidence.py` | desktop + mobile screenshots + `measurements.json` |
