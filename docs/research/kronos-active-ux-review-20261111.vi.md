---
purpose: Review UX độc lập (rendered inspection) — bản tin vận hành, block Kronos active, Settings Kronos
branch: feat/trading-research-delivery-20261110
date: 2026-11-11
status: review độc lập bằng browser thật trên fixture test-owned; KHÔNG deploy, KHÔNG đụng runtime, KHÔNG gửi lệnh, KHÔNG commit
method: đọc plan + bằng chứng UX đã nộp → dựng lại fixture production (dùng đúng hàm của test acceptance) → inspect DOM sống + screenshot desktop/mobile bằng Chrome (MCP) → chạy 1 file test unit focused
related: docs/research/kronos-active-ux-20261111.vi.md, docs/plans/dashboard-operating-brief-ux-20261110.md, docs/research/kronos-active-review-20261111.vi.md
---

# Review UX độc lập — Kronos active + bản tin vận hành — 2026-11-11

Review này **không thay** báo cáo `kronos-active-ux-20261111.vi.md` — nó kiểm tra lại
bằng chứng đó bằng một phiên rendered inspection riêng, trên dữ liệu fixture, và ghi
lại các defect còn lại. Mọi phát hiện dưới đây đều kèm bằng chứng: lệnh đã chạy,
DOM sống đã đọc, hoặc `file:line` trong code.

## 1. Phương pháp và lệnh đã chạy trong phiên này

Đọc trước: `docs/plans/dashboard-operating-brief-ux-20261110.md` (plan đã duyệt),
`docs/research/kronos-active-ux-20261111.vi.md` (bằng chứng UX), 3 ảnh + `measurements.json`
trong `.zcode/artifacts/trading-delivery-20261111/`, mã nguồn test/capture.

Chạy trong phiên:

| Hành động | Lệnh / cách | Kết quả |
| --- | --- | --- |
| Dựng lại fixture production pristine | script riêng `.zcode/ux-review/serve_dashboard_fixture.py` gọi **đúng hàm** `build_fixture_root` + `start_production_server` + `wait_for_server` của `tests/browser/test_dashboard_brief_offline.py` | server `next start` tại `http://127.0.0.1:49228`, `BUILD_ID = xZuZEt04qigGu_bIy40Uk` |
| Dựng fixture Settings (production build + trang `/settings` test-owned) | `.zcode/ux-review/serve_settings_fixture.py` — `FAKE_API` của acceptance giữ **nguyên văn** làm script đầu tiên, lớp fixture Settings chỉ thêm handler khi `location.pathname` là `/settings` | server tại `http://127.0.0.1:49184`, `BUILD_ID = wjsqWG_Z3HFq698FoAlAf` |
| Inspect dashboard + settings | Chrome qua chrome-devtools MCP, context cách ly riêng, viewport 1440×900 và 390×844 (DPR2, mobile), chỉ GET + click trong fixture | chi tiết §3–§5 |
| Chạy focused unit test (1 file) | `.venv\Scripts\python.exe -m pytest tests/test_performance_ui_render.py -q` | **29 passed in 3.41s** |
| Rà mojibake hợp đồng API | `git diff --stat docs/ui-api-contract.md` + quét chuỗi mojibake trong diff | **87 insertions(+), 1 deletion(-)**; không còn chuỗi mojibake (`cá»§a`, `á»`, `Ã¢`) trong diff |
| Dọn dẹp | TaskStop 2 server fixture + đóng tab review | không còn server review nào chạy |

**Không chạy trong phiên này** (nêu rõ, không fake): full acceptance, pytest integration
browser (349s — script runner sẽ chạy sau khi sửa), các test PG
(`test_kronos_decision_settings` v.v.), screen reader thật, Safari/Firefox, axe/Lighthouse.
Số 11 passed / 26 passed / 61 passed trong tài liệu UX là kết quả phiên trước, tôi chỉ
đối chiếu lại mã nguồn test và chạy lại **một** file unit nhẹ nhất có liên quan trực tiếp.

## 2. Browser acceptance có thật sự chạy production fixture không — CÓ

Bằng chứng mã (đọc trực tiếp):

- `tests/browser/test_dashboard_brief_offline.py:830-833` — fixture build bằng
  `next build --webpack` trong thư mục fixture; build fail → `RuntimeError` kèm build log → `pytest.fail`.
- `tests/browser/test_dashboard_brief_offline.py:840-849` — serve bằng
  `next start -H 127.0.0.1 -p <port>`; docstring fixture (:889-898) nói rõ production,
  build là một phần của check.
- `scripts/capture_dashboard_ux_evidence.py:35-39,317-328` — script chụp ảnh import
  **cùng** `build_fixture_root`/`start_production_server`, nên ảnh và test dùng một build.

Bằng chứng runtime (tôi tự chạy lại đúng chuỗi hàm đó): `next start` chỉ phục vụ được
khi có production build; fixture tôi dựng có `.next/BUILD_ID` và phục vụ HTML production
(chunks `/_next/static/chunks/*.js`, không có marker dev). Kết luận: **claim
"production fixture" là đúng**, không còn `next dev` như vòng review trước đã flag.

## 3. Dashboard rendered — kết quả từng mục của ask

Trạng thái fixture: `data-brief-state="ready"`, `data-active-forecast-state="ok"`,
chỉ request GET (đếm qua `window.__apiRequests`: autoloop, daily, health×2, history,
audit, brief, performance, performance/fills — không POST/PUT/DELETE nào).

- **W1 target size** — 15 phần tử tương tác, **0 mục tiêu <44px** trên desktop lẫn
  mobile 390px (đo `getBoundingClientRect` trên DOM sống; khớp `measurements.json`).
- **W2 contrast/non-color** — token computed style thực: `--muted-foreground` =
  `oklch(0.556 0 0)` trên nền `oklch(1 0 0)`. Với token không màu, Y = L³ = 0.556³ =
  0.1719 → contrast = 1.05/0.2219 ≈ **4.73:1** (AA text thường) — khớp con số trong
  tài liệu UX. Lưu ý đo lường: `measurements.json` ghi `mutedContrast: null` vì script
  đo dùng canvas không resolve được `oklch`, và `notEnoughDataLabelled: false` chỉ do
  regex phân biệt hoa thường ("not enough data" thường trong "Funding not enough data"
  không khớp mẫu "Not enough data") — **hai ô này là hạn chế của script đo, không phải
  defect UI**; số 4.73:1 trong tài liệu UX đến từ phép quy đổi độc lập và tôi xác minh lại.
  Trạng thái không dựa vào màu đơn độc: P1 có badge chữ "Act now", stale có badge
  "Stale data", band/latency là chữ.
- **W3 keyboard/semantics** — Tab thật đưa focus tới nút primary "Refresh"
  (88×44px), `:focus-visible` khớp, ring đo được `oklab(0.708 0 0 / 0.5) 3px`.
  Headings: h1=1, h2=6, h3=14, **không mức nào bị bỏ qua**; tiêu đề các card là h2
  (`[data-slot="card-title"]` của brief = H2); link attention có tên truy cập đủ ngữ
  cảnh ("Open — The bot has stopped placing new orders today.").
- **W5 reflow mobile 390×844** — `pageOverflow=false`, `briefOverflowBlocks=[]`,
  không target nhỏ; header + chips wrap đúng; ảnh mobile đối chiếu khớp artifact.
- **W6 trạng thái** — heat null hiện **"Unknown"** kèm lý do nguyên văn
  ("risk-at-stop is not in the snapshot"), không bao giờ 0; funding unknown hiện
  "Funding not enough data" (không render 0); net = realized − fees = 4.75 đúng, funding
  không bị cộng vào; coverage partial hiện drift quy đổi % (0.0125 → "1.3%") + cửa sổ +
  2 fills; unattributed có khối riêng "Fills with no identified origin (outside the bot)
  … 3 fills / Not counted in the bot's result." — không cộng PnL bot.
- **Findability** — thứ tự fold đúng plan: Cần xử lý → Trạng thái → Kết quả → Rủi ro →
  Kronos forecast → shadow (trong Hoạt động) → So sánh phiên bản. Kronos forecast là
  **section riêng có h3 "Kronos forecast (latest recorded cycle)"**, disclosure đóng
  mặc định, mở ra đủ: tỉ lệ mẫu kèm mẫu số (`up 9/16 (0.563) · down 5/16 (0.313) ·
  flat 2/16 (0.125)` — tổng đúng 16), horizon `24h (6 bars)`, paths `16 paths · 1
  samples/path`, cutoff/generated/recorded, band kèm **USDT**, spread, wall latency
  `2,512 ms`, model ghim; note "Observation labels only; nothing here is a profit
  claim" — không có claim PnL, không có "tiết kiệm LLM call".
- **Active vs shadow** — DOM sống: đúng 1 `[data-brief-block="active-forecast"]`
  (section riêng) và 1 `[data-brief-block="shadow"]` (nằm trong section "Today's
  activity", render dạng dòng gọn) — shadow không thay thế được dự báo active; cả hai
  cùng hiện và nội dung không trộn.
- **Không mislabel Auto** — dòng trạng thái tách bạch "Auto Spot On / Auto Futures
  Off"; khối Kronos không hề mang nhãn Auto; pin test:
  `tests/test_kronos_decision_settings.py:79-82` (`kronos_active_enabled` ∈ `_GATE_KEYS`,
  **∉** `auto_modes.MODE_KEYS`).
- **Attention/retry** — halted + protection-unconfirmed xuất hiện **đúng 1 lần** trong
  "Needs handling" (badge P1 "Act now"), không còn banner fallback trùng
  (`[data-fallback-banner="halt"]` = 0); link trỏ `/audit`, `/cycles` — không có nút
  duyệt nào trên brief. Portfolio giữ data cũ + badge "Stale data" + nút "Retry"; bấm
  Retry lỗi stays local, các card khác nguyên vẹn. Toàn trang còn **2 nút Refresh**
  (primary + Hoạt động) — khớp mô tả trong tài liệu UX §7.
- **Scope filter** — chips All/Spot/Futures có `aria-pressed`, group có nhãn "Scope";
  chọn Spot ẩn dòng `open_futures`/`exposure` của brief VÀ bảng "Futures positions" của
  Portfolio; All trả lại đủ — đúng định nghĩa "scope là filter cả trang".
- **Không trùng positions** — đếm trên DOM sống: đúng **2** bảng vị thế (Spot +
  Futures, cả hai thuộc Portfolio card), "Futures account" chỉ 1 nơi; card Hiệu suất
  giao dịch không còn bảng vị thế (component đã xoá). Test pin chủ sở hữu mới chạy lại
  trong phiên: `tests/test_performance_ui_render.py` **29 passed**, gồm
  `test_performance_card_does_not_duplicate_the_positions_owner`
  (`tests/test_performance_ui_render.py:743`) và các assertion trung thực
  (precision đầy đủ, unavailable ≠ partial ≠ per-symbol, stale, không 0 thay null).
- **Experiments** — "Version comparison (out of sample)": Latest/Hypothesis/Verdict
  better 4 · worse 6, note "Simulated, paired. Never a promotion." — không claim thắng.

## 4. Settings rendered — nhóm Kronos trong khu AI

Instance: production build thật, trang `/settings` test-owned, dữ liệu fixture gồm
`kronos_active_enabled` (operator, group `forecast`) + 9 key tuning `kronos_active_*`
(`/api/settings`) — đúng phân chia sở hữu như server
(`server/operator_config.py:240-263` cho managed switch; `tradingagents/default_config.py:111-119`
cho 9 key env).

- **Findability/đúng khu** — khu **Automation** không có chữ "Kronos" nào
  (chỉ Auto Spot/Futures với badge "mode"); Kronos nằm ở **AI models**: card
  "Kronos — forecast context" + panel "Kronos forecast status" (h3) + group fold
  "Kronos forecast parameters (advanced)" chứa đủ 9 key, nhãn/hint vi/en có sẵn trong
  i18n (`web/lib/i18n/messages/settings.ts:68-76,140-148`).
- **Không mislabel Auto** — switch Kronos có badge **"context switch"** (khác badge
  "mode" của Auto); hint: "Managed switch: the Kronos forecast enters the context
  before a decision. **Not an order-arming switch.**"; khối xác nhận ghi rõ:
  "This switch changes the context an automated cycle reads BEFORE deciding … **It
  does NOT arm orders and is not an Auto Spot/Futures switch.** A confirmation and
  reason are required; both are recorded in the operator log."
  (`web/components/settings/operator-config-card.tsx:139-145,332-363`).
- **Panel trạng thái** (`data-kronos-status`) — On/Off, readiness "Model unavailable"
  + lý do nguyên văn (fixture: interpreter absent), mode `context_only` kèm giải nghĩa
  (không cắt ứng viên, không nới risk, không phải lệnh), failure policy `defer` kèm ý
  nghĩa, model ghim `thuml/Kronos @ <revision 12 ký tự>`, scope `4h closed candles ·
  horizon 24h · 16 sample paths · T=0.5 · lookback 96 bars · batch 80 rows · band ≤ 0.15`,
  note "read-only, same sources the cycle would use — no model is started".
- **Confirm + reason + save + undo** (bấm thật trên DOM sống, PUT vào fixture):
  đổi switch → khối xác nhận hiện; Save khóa khi thiếu confirm/reason; điền đủ →
  PUT `/api/operator-config` (chỉ 1 request, đúng cơ chế confirm/reason của backend —
  pin 412/reason/audit/DELETE-reset tại `tests/test_kronos_decision_settings.py:119-151`,
  không chạy lại trong phiên); sau save badge "Current server state: On"; toggle mới →
  **Undo** trả về trạng thái server và xoá khối xác nhận. Switch focus được bằng
  Tab (`aria-label` "… draft", `aria-describedby` → trạng thái).
- **Validation** — (a) client: `paths = "abc"` → bấm Save → lỗi per-field
  `role="alert"` "Must be an integer, got “abc”.", `aria-invalid` + `aria-describedby`,
  **không PUT nào được phát**; (b) server: `max_band_spread_ratio = -1` → PUT →
  422 `coercion_error` → UI map đúng trường: "Rejected by server: … must be a finite
  non-negative number" (đường `use-settings.ts:377-389`). Đường domain 422
  (`tuning_problems`, ví dụ paths<16) đã pin ở test server, không có scenario browser
  riêng — xem §7 gap.
- **Mobile Settings 390px** — không tràn ngang (`docWidth=390=clientWidth`), 0 target
  <44px; ảnh viewport cho thấy panel trạng thái + switch + Save/Undo xếp dọc gọn.

## 5. Defect tìm thấy (không có mục nào chặn bàn giao, nhưng nên sửa)

Mức: ⛔ chặn / 🟠 trung bình / 🟡 nhỏ / ℹ️ ghi nhận.

1. 🟠 **Fixture nghiệm thu rơi về dữ liệu THẬT khi mở thủ công** — `FAKE_API`
   dereference `state.venueProfile` ngay lúc nạp IIFE
   (`tests/browser/test_dashboard_brief_offline.py:69` trong `portfolio` và `:97`
   trong `brief()`) nên nếu
   `window.__FIXTURE_STATE__` chưa được set (mở URL fixture bằng browser thường,
   không qua driver CDP), script head **crash trước khi override fetch**; trang vẫn
   chạy và mọi `/api/*` đi thẳng vào server Next → rewrite
   `web/next.config.ts:23-28` chuyển tiếp tới **FastAPI thật `127.0.0.1:8000`**.
   Trong phiên này tôi đã gặp đúng sự cố đó trước khi set state (log network của
   chính tôi: `/api/brief` 404, các GET khác 200 — chỉ đọc; dashboard không phát
   write nào). Test acceptance và capture script luôn set state qua
   `addScriptToEvaluateOnNewDocument` nên **bằng chứng hiện có không bị ảnh hưởng**,
   nhưng đây là bẫy nhiễm dữ liệu thật cho bất kỳ ảnh/DOM "fixture" nào chụp thủ công.
   Đề xuất sửa nhỏ (test-owned): `const state = window.__FIXTURE_STATE__ ?? {}` +
   hiện cảnh báo trên trang khi thiếu state, hoặc bỏ rewrite `/api` trong fixture
   config để request lạc trả 404 trắng.
2. 🟡 **Shadow block: tiêu đề không phải heading + note lệch ngữ cảnh** —
   "Shadow observations" là `<p class="text-xs font-medium">` (đo DOM sống;
   `brief-card.tsx:610-611`), screen-reader không điều hướng bằng heading tới đây; đồng
   thời note "A direction ratio is a sample ratio over the model's paths — not a
   calibrated probability" được gắn **vô điều kiện** ở cuối block
   (`brief-card.tsx:689`, chuỗi tại `web/lib/i18n/messages/brief.ts:424`) kể cả khi
   mọi provider là rule-based, không có ratio nào hiển thị (đúng như ảnh của tôi:
   chỉ có `filter:donchian_breakout` deterministic). Nên: promotion `<p>` → `h4` dưới
   h3 "Today's activity" và chỉ gắn note model-path khi `distribution_kind === "model_samples"`.
3. 🟡 **"Band spread 0.0765" là số trần không đơn vị** (`brief-card.tsx:864-869`,
   `maximumFractionDigits: 4`) — operator không phân biệt được 7.65% hay 0.0765×.
   Nên hiện "0.0765× (7.65%)" hoặc ít nhất kèm chữ "ratio".
4. 🟡 **Forecast "off" không có link tới Settings** — câu "Off in Settings — nothing
   is read…" (`brief.ts:437`, render `brief-card.tsx:722-731`) chỉ là text; đây là
   đúng chỗ duy nhất dashboard nói "hãy đi sửa cấu hình" mà không có lối đi. Nên bọc
   link `/settings#ai` (vẫn read-only, không thêm control đổi state).
5. 🟡 **Hint/unit của 9 key tuning không gắn a11y vào input** — `SettingField`
   render hint `<p>` không có id và `aria-describedby` chỉ trỏ tới error
   (`web/components/settings/setting-field.tsx:32,44-46`); unit cũng là span không id
   (:90-92). Hint của các key Kronos chứa **rule domain** (bội khung nến, giới hạn 72
   nến, 16–32 paths) nên ai dùng screen reader sẽ không nghe rule trước khi nhập.
   Operator card làm đúng mẫu để theo (`operator-config-card.tsx:121-126`).
6. 🟡 **Settings: card title toàn màn là `div`** (đo DOM sống; `ui/card.tsx:35-46`
   mặc định div, chỉ dashboard brief truyền `as="h2"`) — outline heading của Settings
   chỉ còn h1 → h2 khu → vài h3 nội dung; điều hướng theo heading thiếu ngữ cảnh card.
   Là convention cũ của cả màn (không phải regression của nhánh này) — ghi lại để
   quyết định riêng, không sửa lẫn vào pass Kronos.
7. ℹ️ **Copy nit**: "1 samples/path" (số nhiều khi =1, `brief.ts:457`); câu
   "Funding not enough data: USDT" hơi khó đọc; pinned model bị cắt giữa từ
   ("kronos-fixtu") không có dấu `…`/`title` đầy đủ (`operator-config-card.tsx:455`).
8. ℹ️ **Fixture data lệch năm**: `data_open_time: 1762761600000` là 2025-11-10 trong
   khi `as_of` là 2026-11-10 (`test_dashboard_brief_offline.py:181-182`) — UI render
   trung thành ("Newest candle open 11/10/25, 15:00:00") nên ảnh sẽ khiến người đọc
   tưởng lệch 1 năm. Chỉnh fixture cho cùng ngày để đỡ nhiễu khi xem bằng chứng.
9. ℹ️ Quan sát môi trường (ngoài scope UX, đã có trong ghi nhớ dự án): server runtime
   thật đang bind `0.0.0.0:3000/:8000` (netstat trong phiên: PID 43612) — nhớ rà
   authentication; review này không đụng tới hai port đó.
10. ℹ️ Workspace wart: tồn tại thư mục **rỗng** `web/.next-verify ` (tên có **dấu cách
   cuối**, mtime 04:06 cùng ngày) nên `git status` in warning
   "could not open directory 'web/.next-verify /'" và `.gitignore:235`
   (`web/.next-verify/`) không khớp tên có dấu cách. Không phải do phiên review này
   tạo (hai build fixture của tôi chạy `cwd` trong `%TEMP%` với `NEXT_DIST_DIR` chưa
   set, xác nhận `echo NEXT_DIST_DIR=[%NEXT_DIST_DIR%]` → chưa set) và thư mục rỗng nên
   không ảnh hưởng bằng chứng; nêu ra để không ai đuổi theo warning đó.

## 6. Những gì tài liệu UX nói khớp bằng chứng

Ảnh desktop/mobile + `measurements.json` đối chiếu trực tiếp với DOM sống tôi đo lại:
docHeight 4329/6790, 0 small target, h1/h2/h3, 2 nút Refresh, không tràn ngang,
forecast details đóng mặc định, heat Unknown không 0 — **đều khớp**. Con số trong
ảnh (PnL, band, latency) là fixture như tài liệu đã cảnh báo; các câu trung thực
(trạng thái ước tính, funding unknown, unattributed, "not a calibrated probability",
"never a promotion", "nothing here is a profit claim") đều có mặt trên DOM sống.

## 7. Gap còn tồn tại (trung thực, không fake pass)

- **Chưa có browser test scenario riêng cho màn Settings với đúng cặp key
  `kronos_active_enabled`** — luồng confirm/reason/save/undo tôi vừa kiểm là kiểm
  thủ công một lần trên DOM sống; không phải test pin. Gap này tài liệu UX §7 đã tự
  ghi; review xác nhận gap **vẫn còn** (browser test hiện có chỉ render
  OperatorModes/OperatorRisk với 11 key fixture không có kronos —
  `tests/test_operator_settings_ui_browser.py:60-82,118-129`).
- Đường 422 domain (`tuning_problems`) và conflict serverAhead trên key Kronos: chỉ
  pin ở test server/unit, chưa có scenario browser riêng.
- Screen reader thật, Safari/Firefox, axe/Lighthouse, locale vi chạy runtime
  Settings, trạng thái horizon null → "—" (chỉ xác minh qua code
  `operator-config-card.tsx:460`): **chưa chạy trong phiên này**.
- Full acceptance + các test PG: thuộc lượt chạy của script sau khi sửa (theo quy
  trình bàn giao), không chạy trong review này.

## 8. Kết luận

Không có defect chặn. Toàn bộ hạng mục của ask — tìm thấy được (dashboard + Settings
khu AI), tách bạch active-vs-shadow, không mislabel Auto, keyboard/mobile/attention/retry,
validation + save flow, thời gian/đơn vị/tỉ lệ/trạng thái unknown, không trùng vị thế,
assertion trung thực tài chính giữ nguyên — **đạt trên DOM sống của fixture production**,
kèm 10 mục defect nhỏ/ghi nhận ở §5 nên xử lý trước hoặc ngay sau bàn giao (mục 1
là ưu tiên nhất vì liên quan độ sạch của bằng chứng nghiệm thu). Browser acceptance
**đúng là production fixture** (mã + runtime xác minh).
