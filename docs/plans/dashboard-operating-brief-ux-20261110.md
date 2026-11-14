# Bản thiết kế UX/UI: Bản tin vận hành + redesign dashboard — 2026-11-10

- **Người soạn:** agent chuyên gia UX/UI dashboard (phiên 2026-11-10)
- **Mốc code:** nhánh `feat/trading-research-delivery-20261110` (tách từ `main` @ `0943103`)
- **Căn cứ chính:** [docs/plans/trading-research-delivery-20261110.md](trading-research-delivery-20261110.md) (đặc biệt §5 bản tin + §7.1 seam đã duyệt) và [docs/research/2026-11-10-carm1nee-ai-trading-stack-report.vi.md](../research/2026-11-10-carm1nee-ai-trading-stack-report.vi.md) §5A–§5F, §7
- **Phạm vi:** thiết kế (không edit feature code). Stage implement tương ứng là bước 4 của plan kỹ thuật (stage `dashboard`).
- **Ràng buộc bất di bất dịch:** không có control thay đổi runtime state trên dashboard; không đổi venue/risk limits/Auto; không thêm framework/dependency; disclaimer research giữ nguyên mọi nơi user-facing; không Telegram/push.

---

## 0. Intake card (theo design-intake-and-direction-runbook)

```text
DESIGN_INTAKE:
  request: Redesign dashboard `/` thành "bàn điều hành" — bản tin vận hành tổng hợp trên cùng, giảm lặp số liệu, công bố trung thực kết quả chưa đối soát.
  primary_user_job: "Một cái nhìn đầu trang trả lời: tôi cần xử lý gì ngay, bot đang ở trạng thái nào, kết quả đã đối soát là bao nhiêu, vị thế/rủi ro đang thế nào, bot đang làm gì."
  audience_and_context: Operator duy nhất chạy bản địa, xem crypto 24/7, desktop-first và mobile check-in; gián đoạn thường xuyên; rủi ro tài chính thật (venue demo hoặc production).
  surface: web (Next.js App Router, desktop-first responsive)
  platform_class: web
  task_type: ui + flow (đọc - đánh giá - quyết định có cần hành động không)
  product_risk: high (tiền thật ở mode live; nhầm "ước tính" với "đã đối soát" là hậu quả thật)
  constraints: giữ seam đã duyệt (brief/ + card hiện có); không control runtime; i18n vi/en hiện có; token hiện có; không thêm dependency; Next 16 (không dùng pattern train-data cũ).
  success_metrics: (1) trường hợp "chưa đối soát" không bao giờ render số 0; (2) mọi lỗi section bị lỗi cục bộ, không sập cả trang; (3) mọi target tương tác ≥44px; (4) offline fixture test dashboard pass (`verify --stage dashboard` exit 0).
  anti_goals: không bịa net PnL; không nhầm "không có tín hiệu" với "pipeline lỗi"; không biến dashboard thành bảng điều khiển giao dịch; không thêm card chỉ vì có dữ liệu.
  open_questions: không có câu hỏi chặn — taste được giao cho chuyên gia (theo ask), mọi lựa chọn ghi rationale trong mục 3.
```

---

## 1. Audit hiện trạng

### 1.1 Cấu trúc source đã đọc (mốc `0943103`, nhánh hiện tại)

| Khối | File | Trạng thái dùng được |
| --- | --- | --- |
| Trang | `web/app/page.tsx:8-14` | Header + `OverviewDashboard`, không logic |
| Shell dashboard | `web/components/overview/dashboard.tsx` | 6 hook poll riêng từng endpoint (interval 10s–300s, `:44-50`); banner halted/approvals (`:323-342`); trạng thái `allLoading` toàn trang (`:273-278`) |
| Hook poll | `web/components/overview/use-panel.ts:17-24,106-112` | 4 trạng thái `loading/ready/stale/error`, giữ data cũ khi lỗi poll — **nền tảng giữ lại** |
| Lỗi chung | `web/lib/api.ts:11-76` | Envelope `{error:{code,message,details}}` + FastAPI `{detail}` — **giữ lại** |
| Contract types | `web/components/overview/contract.ts:56-287` | Types 6 endpoint, truthfulness null-first |
| Card danh mục | `web/components/overview/portfolio-card.tsx` | Spot KPI + bảng Spot + Futures KPI + bảng Futures 10 cột (`:179-313`), empty/stale/error tách nhánh (`:315-430`) |
| Card hiệu suất | `web/components/overview/performance-card.tsx:64-110` | **Lặp lại** positions từ portfolio (`performance-positions.tsx:48-77`), fills history, events |
| Card hoạt động | `web/components/overview/activity-card.tsx:79-135` | 3 hàng Autoloop/Quét/Phân tích + link "Chi tiết" |
| KPI quyết định | `web/components/overview/kpi-cards.tsx:24-81` | 4 tile, tone profit/loss kèm dấu + chữ (không dùng màu đơn độc) |
| Bảng lệnh | `web/components/overview/order-table.tsx:22-94` | Lọc executed, badge Spot/Futures/LIVE |
| Chip tự động | `web/components/exec-status-chip.tsx:94-110` | Gộp `auto_spot || auto_futures` thành 1 chip; SSE settings/events + visibility refetch |
| UI primitives | `web/components/ui/{card,badge,button,dialog}.tsx` | shadcn-style, CardTitle là `div` (`ui/card.tsx:35-46`) |
| Token | `web/app/globals.css:30-31,69-73,108-112,131-134,140-144` | `--profit/--loss` OKLCH, `min-touch` 44px, focus-visible outline |
| i18n | `web/lib/i18n/messages/{overview,common}.ts` | vi/en đầy đủ vocabulary hiện có |
| API contract | `docs/ui-api-contract.md` §0–§15 | Envelope lỗi, 4 trạng thái bắt buộc, §6 portfolio chi tiết truthfulness |
| Side server | `server/autoloop.py:2243-2284` | Vocabulary terminal `executed/awaiting_approval/risk_rejected/not_executed` + `plan_reason` + `protection_state` |
| Risk keys | `server/operator_config.py:56-107` | `risk_max_derivatives_exposure_pct`, `risk_max_derivatives_exposure_hard_pct`, `risk_max_open_heat_pct` (limits — dashboard chỉ hiển thị mức dùng, không sửa) |

### 1.2 Browser audit (chỉ-đọc, 2026-11-10)

Môi trường: dev server đã chạy sẵn ở `:3000` (netstat: `0.0.0.0:3000 LISTENING`, PID 43612 — lưu ý bind all-interfaces, khớp native memory "LAN exposure"). Chrome headless profile riêng, chỉ navigate GET + chụp, không click control nào đổi state.

- **Desktop 1440×900** (viewport + full-page screenshot, artifact `tool-result-755d…png`): thứ tự trên trang là Cập nhật/Làm mới → Hoạt động hiện tại → Danh mục → Hiệu suất quyết định → Hiệu suất giao dịch → Lệnh thực thi. Không có khối "cần xử lý" tổng hợp; banner halted/chờ duyệt chỉ hiện khi có sự kiện.
- **Mobile 390×844** (screenshot + `evaluate_script`): sidebar collapse đúng (`sidebar.tsx:33-56`); đo được `docHeight = 8341px`, **8 bảng**, 4 nút "Làm mới" hiển thị cùng lúc; bảng fills `scrollWidth = 933px` và events `850px` trên viewport 376px (bọc `overflow-x-auto` — 2D scroll có chủ đích, chấp nhận được nhưng cần giam số dòng).
- **Contrast:** `text-muted-foreground` đo được `lab(48.496 0 0)` trên nền `lab(100 0 0)` → L\*48.5 → Y≈0.172 → contrast ≈ **4.73:1** (đạt AA cho text 14px). Đây là suy toán từ L\* đo được, chưa phải report tool — ghi là "ước tính đạt".
- **Heading:** `h1=1, h2=0, h3=9` — CardTitle không phải heading (`ui/card.tsx:35` dùng `div`); navigate-by-heading của screen reader bị gãy vì 9 `h3` không có cha `h2`. 3 link "Chi tiết" trùng label.
- **Lặp số liệu nhìn thấy được:** KPI tài khoản Futures (số dư/equity/khả dụng/lãi-lỗ) và bảng vị thế Futures xuất hiện **2 lần** (Danh mục và Hiệu suất giao dịch); 4 nút làm mới riêng lẻ + 1 toàn trang.
- **Điểm tốt giữ lại:** mọi số PnL kèm dấu +/− và đơn vị tiền (`lib/format.ts:85-100`); empty-state có hướng đi (`empty-state.tsx`); stale badge + note lỗi riêng từng panel; 4 trạng thái use-panel nhất quán.

### 1.3 Phát hiện theo gate (ux-quality-gates)

| Gate | Mức | Vị trí | Ảnh hưởng | Hướng xử lý trong thiết kế này |
| --- | --- | --- | --- | --- |
| IA | Warning | `dashboard.tsx:301-421` — không khối tổng hợp; mô tả trang nói alpha (`overview.ts:26-28`) trong khi fold đầu là hoạt động+danh mục | Operator phải tự ghép 6 nguồn dữ liệu | Brief card trên cùng, mô tả trang đổi theo nội dung mới |
| Primary action | Warning | 5 nút "Làm mới" (4 đo được trong viewport + refresh per-card) | Hành động chính không rõ | 1 primary "Làm mới" toàn trang; refresh per-card rút về icon-only phụ |
| State coverage | Info | `use-panel.ts` đủ 4 trạng thái; `performance.ts` `summary: null` render đúng "chưa đủ dữ liệu" | Tốt — giữ | Chuẩn hoá vào brief |
| Minimalism | Critical | Bảng Futures + KPI Futures lặp 2 lần; 8 bảng, trang mobile dài 8341px | Số liệu trùng làm giảm tin cậy, khó so sánh | Mỗi loại dữ liệu 1 nhà duy nhất: positions thuộc Danh mục; Hiệu suất chỉ giữ fills + events |
| Component family | Info | Card/badge/button dùng chung token | Tốt | Brief kế thừa token, không thêm màu mới ngoài 3 mức priority |
| Semantic web | Warning | CardTitle là `div`, `h3` không có `h2` (`ui/card.tsx:35-46`, đo được h2=0/h3=9) | Screen reader điều hướng yếu | Card title thành `h2`, section con `h3` |

Không phát hiện gate Critical về forms/recovery: dashboard read-only, mọi lỗi có retry cục bộ.

---

## 2. Hai direction nội bộ và lựa chọn

So sánh trên cùng 8 tiêu chí của runbook (job clarity, state clarity, risk/recoverability, accessibility, responsive, implementation fit, distinctiveness, handoff precision).

### Direction A — "Bàn điều hành brief-first" (operations desk, progressive disclosure)

- **user_promise:** Vào trang là thấy "cần xử lý gì" và "bot đang thế nào" trước; chi tiết chỉ mở khi cần.
- **visual_posture:** kỹ-calmb (technical calm), data-dense ở nơi có chủ đích.
- **hierarchy:** 1) Cần xử lý (priority list) → 2) Trạng thái + Kết quả đã đối soát → 3) Vị thế/Rủi ro → 4) Hoạt động → 5) chi tiết lịch sử (fold/link).
- **density:** balanced ở brief; dense ở bảng chi tiết (đã giam số dòng).
- **composition:** brief card full-width trên cùng; phần còn lại lưới card hiện có, bảng có cap dòng + link "xem tất cả".
- **motion:** không thêm; giữ transition mặc định của component hiện có.

### Direction B — "Terminal dày đặc" (mọi thứ hiện, sticky rail)

- **user_promise:** nhìn tổng thể không phải scroll.
- **visual_posture:** terminal, multi-column.
- **hierarchy:** phẳng theo cột; brief là một panel bên trái.
- **risks:** trang dài hơn nữa khi thêm brief → vi phạm anti-goal "không thêm card"; mobile trở thành chuỗi bảng 2D scroll; truthfulness bị chôn trong số dày; mọi lỗi mới phải chen vào lưới dày.

### So sánh và quyết định

| Tiêu chí | A | B |
| --- | --- | --- |
| Job clarity (cần xử lý nổi bật) | Đạt — brief là khối đầu | Yếu — brief chen giữa số |
| Mobile reflow (W5) | Đạt — stack dọc có fold | Rủi ro — nhiều bảng 2D scroll |
| Implementation fit | Cao — chỉ thêm `brief/` + điều card hiện có | Cao nhưng đụng layout tổng |
| Anti-goal "giảm card/lặp" | Đạt | Trái |

**Chọn A.** Lý do: đúng job của operator (trả lời "cần làm gì" trước, "dữ liệu" sau), khớp seam đã duyệt (brief đặt trên cùng, giữ card hiện có), và là hướng duy nhất thoả anti-goal giảm lặp. B bị từ chối vì tăng mật độ chính là vấn đề được audit phát hiện (1.2/1.3), không phải giải pháp.

---

## 3. Quyết định cấu trúc UX (Phase 4 của /design)

1. **Primary action:** một nút **"Làm mới"** toàn trang (đã có, `dashboard.tsx:314-318`). Dashboard **không có** hành động thứ hai đổi state — mọi thứ khác là link điều hướng hoặc refresh cục bộ icon-only.
2. **Vocabulary user-facing (vi, khóa với i18n hiện có):** "Bản tin vận hành", "Cần xử lý", "Đã đối soát", "Ước tính (chưa đối soát)", "Chưa đủ dữ liệu", "Vị thế mở", "Heat đang dùng", "Exposure đang dùng", "Không có tín hiệu", "Pipeline lỗi", "Lệnh ngoài bot". Không dùng thuật kỹ thuật cho người đọc (cycle_id chỉ xuất hiện dạng mã tham chiếu, không phải nhãn chính).
3. **UX model:** `operations/data-dense` chính + secondary `progressive disclosure`. IA route giữ nguyên 5 mục sidebar; dashboard `/` là màn đọc, không phải màn thao tác.
4. **Scope và relationships:** brief tổng hợp từ store hiện có (health/audit/portfolio/cycles) + `outcome_coverage` khi đã bật; không ảnh hưởng tới approvals/settings. Vị thế thuộc Danh mục; lệnh thuộc Lịch sử; brief chỉ **trích dẫn** số + link.
5. **Dangerous actions:** không có trên dashboard (by design). Confirm/undo/recovery vẫn thuộc `/approvals` (đã có dialog riêng) — brief chỉ hiện "N lệnh chờ duyệt" + link, không nhúng nút duyệt.
6. **State model:** tái dùng đúng 4 trạng thái `use-panel.ts:17-24` (loading/ready/stale/error) + empty riêng (`empty-state.tsx`) + trạng thái dữ liệu nghiệp vụ mới: `unavailable` / `partial` / `unattributed` — tất cả có render riêng, không bao giờ render 0 thay cho null. Keyboard: mọi target ≥44px (`min-touch`), focus-visible toàn cục (`globals.css:140-144`).
7. **Platform behavior:** desktop-first responsive; mobile giữ 2D scroll có bọc cho bảng vốn 2D (W5 exception có chủ đích); không thêm hover-only thông tin; đồng hồ stale chạy 5s như hiện có (`dashboard.tsx:197-201`).

---

## 4. Hierarchy nội dung trang `/` (sau redesign)

```
H1 "Tổng quan" (+ mô tả mới ngắn: bản tin vận hành, không phải màn điều khiển)
├─ Thanh trạng thái dòng: "Cập nhật lúc HH:mm:ss" | StaleBadge | [Làm mới]  (giữ nguyên vị trí hiện tại)
├─ ① BRIEF CARD  — "Bản tin vận hành" (mới, web/components/overview/brief/)
│    ├─ Cần xử lý        (priority list, chỉ hiện khi có mục; rỗng → 1 dòng "Không có việc cần xử lý")
│    ├─ Trạng thái       (Auto Spot / Auto Futures / venue profile / halt / lệnh chờ duyệt)
│    ├─ Kết quả          (đã đối soát | ước tính | chưa đủ dữ liệu — 3 tầng, mục 7.4)
│    ├─ Rủi ro           (vị thế mở · protection không chắc · exposure/heat đang dùng)
│    └─ Hoạt động        (ứng viên/phân tích/lệnh hôm nay + 3 lý do không vào lệnh + quan sát shadow khi có)
├─ ② Danh mục            (giữ PortfolioCard — nhà duy nhất của positions)
├─ ③ Hiệu suất quyết định (giữ KpiCards + AlphaChart)
├─ ④ Hiệu suất giao dịch (giữ fills + events; BỎ khối positions lặp)
└─ ⑤ Lệnh thực thi gần đây (giữ OrderTable, cap 10 dòng + link "xem tất cả ở Lịch sử")
```

Banner halted/chờ duyệt hiện có (`dashboard.tsx:323-342`) chuyển vào khối "Cần xử lý" của brief (dedup với `needs_attention`), không còn 2 banner riêng lẻ cạnh nhau.

Điểm chốt: brief là **tổng hợp đọc-only**; không card nào dưới brief được đổi vai trò thành control.

---

## 5. Responsive layout

| Breaakpoint | Bố cục |
| --- | --- |
| ≥1280px | Brief grid 2 cột (Trạng thái+Kết quả \| Rủi ro+Hoạt động), phần dưới giữ lưới card hiện tại |
| 768–1279px | Brief 1 cột; card lưới như hiện tại |
| <768px | Mọi thứ stack dọc (đã đúng); bảng giữ `overflow-x-auto` (exception W5 có chủ đích, vì bảng vốn 2D); Cần xử lý nhảy lên ngay dưới H1 vì là nội dung hành động |

Chi tiết bắt buộc (S4 — project defaults):
- Targets ≥44×44 CSS px: `min-touch` utility (`globals.css:131-134`) áp cho mọi link/nút mới, gồm link "Chi tiết" trong brief.
- Text scale: số tiền dùng `tabular-nums` (đang có) — khi zoom 200% bảng vẫn co giãn theo %, không khoá px.
- Bảng 10 cột Futures giữ nguyên bọc scroll; brief KHÔNG nhét bảng — brief chỉ số nhãn:giá trị dạng dl/dòng ngắn, mobile không phải scroll ngang ở khối đầu trang.
- Không dùng `100vh`; layout dùng `min-h-full` hiện có (`layout.tsx:46`).

---

## 6. State matrix (áp cho brief và các section)

| Trạng thái | Nguồn | Render bắt buộc |
| --- | --- | --- |
| loading | chưa có poll đầu tiên | skeleton khớp layout khối (không spinner toàn trang) |
| ready | poll OK trong `stale_after` | dữ liệu |
| stale | poll lỗi sau khi có data (`use-panel.ts:71-82`) | giữ data cũ + `role="alert"` note + badge |
| error | chưa từng có data | khối lỗi cục bộ + retry; **không** chạm section khác |
| empty | 200 danh sách rỗng | EmptyState có hướng đi (pattern `empty-state.tsx`) |
| `unavailable` (nghiệp vụ) | endpoint trả status unavailable | dòng "Chưa đủ dữ liệu …" + lý do code — **không phải lỗi đỏ, không phải 0** |
| `unsupported` (nghiệp vụ) | sàn không được hỗ trợ (futures_trade_history.py:48; performance_history.py:201-203) | dòng riêng "Sàn không được hỗ trợ …" + warning nguyên văn; khác copy với `unavailable` (chưa bật/chưa đọc) — nếu ingest map về unavailable thì warning "unsupported_exchange" quyết định copy |
| `partial` (nghiệp vụ) | coverage/partial đọc | hiện số có được + cảnh báo coverage nguyên văn + phần còn lại explicit |
| unattributed/manual | fill không có attribution | đếm riêng "Lệnh ngoài bot: N" — không vào PnL bot |
| offline/network | fetch throw | dùng `networkHint` (`lib/api.ts:40-42`) |

Invariant test bắt buộc (khớp plan §5.1): null → "chưa đủ dữ liệu"; mixed-currency không cộng; "không có tín hiệu" (cycle completed/rejected hợp lệ) ≠ "pipeline lỗi" (error/timeout); sai số đối soát hiển thị nguyên văn.

---

## 7. API contract — `GET /api/brief` (read-only)

Tên endpoint chốt: **`GET /api/brief`** (ngắn hơn `/api/operating-brief`; một resource tổng hợp duy nhất; thống nhất với plan §5.1 phương án đầu). Không route POST/PUT/DELETE. Mọi response mang `generated_at` + `stale_after` (§0 ui-api-contract). Shape dưới đây là **hợp đồng thiết kế** cho stage implement — DDL/aggregate cuối cùng pin ở stage đó (giống cách plan §4.2 xử lý CLI).

### 7.1 Shape đề xuất

```jsonc
{
  "generated_at": "ISO-8601 UTC",
  "stale_after": 30,
  "status": {
    "auto_spot": "on|off|unknown",
    "auto_futures": "on|off|unknown",
    "venue_profile": "demo|production|null",
    "exec_mode": "string|null",
    "halted_today": true|false|null,
    "halt_reason": "string|null",
    "pending_approvals": "number|null"
  },
  "results": {
    // Vocabulary phải gồm 'unsupported': Literal thật của FuturesTradeHistory là
    // ok/partial/error/unsupported/unavailable (server/futures_trade_history.py:48;
    // performance_history.py:201-203 trả "unsupported" + warning "unsupported_exchange"
    // cho sàn != binance). Nếu stage ingest chọn map unsupported→unavailable kèm
    // warning nguyên văn thì status chỉ còn 4 giá trị — UI đọc warning để hiện đúng copy.
    "ledger_status": "ok|partial|error|unsupported|unavailable",
    "verified": {                                      // CHỈ từ outcome_positions/exits/funding đã đối soát
      "net_by_currency": [{ "currency": "USDT", "realized": 0.0, "fees": 0.0, "funding": 0.0, "net": 0.0 }],
      "mixed_currency_note": true|false
    },
    "coverage": {                                      // trích outcome_coverage cửa sổ gần nhất
      "window_start": "ISO|null", "window_end": "ISO|null",
      "fills_found": "number|null",
      "ledger_realized_total": "JSONB verbatim|null",
      "venue_realized_total": "JSONB verbatim|null",
      "drift": "number|null", "drift_pct": "number|null",
      "warnings": ["verbatim từ FuturesTradeHistory"],
      "unattributed_count": "number|null",
      "unattributed_manual_detected": "number|null"
    },
    "estimated": {                                     // nhãn bắt buộc "ước tính"
      "spot_audit_realized_total": "number|null",      // replay FIFO hiện có, tham khảo
      "futures_unrealized": "number|null", "currency": "string|null"
    }
  },
  "risk": {
    "open_positions": "number|null",
    "protection_unconfirmed": "number|null",
    "exposure_used_pct": "number|null",                // đang dùng so limit — chỉ hiển thị
    "heat_used_pct": "number|null",
    "positions_read_status": "complete|partial|unavailable|null",
    "positions_read_observed_at": "ISO|null"
  },
  "activity": {
    "candidates_today": "number|null",
    "analyses_today": "number|null",
    "orders_today": "number|null",
    "no_entry_top_reasons": [{ "code": "execution_status/plan_reason code", "count": 0, "sample_cycle_id": "id|null" }],
    "pipeline_error_cycles": "number|null",            // phân biệt với no-signal
    "shadow_observations": [                            // hạng 4; null/mảng rỗng khi chưa bật
      { "provider": "kronos:<rev>|filter:<name>", "symbol": "", "horizon_hours": 24,
        "dir_rate_text": "72/100", "band": "...", "data_open_time": "ISO",
        "generation_kind": "forward|retrospective", "status": "ok|unavailable|error" }
    ]
  },
  "needs_attention": [                                  // dedup ở SERVER
    { "id": "", "priority": 1,                          // 1 = phải làm ngay, 2 = chú ý, 3 = theo dõi
      "kind": "credential|stale_data|reconciliation|protection|halt|approvals",
      "message_code": "", "message_params": {},
      "cycle_id": "id|null", "link": "/cycles|/approvals|/audit" }
  ]
}
```

### 7.2 Scope filter Spot/Futures

- Bộ lọc là **UI-local** (không tham số API trong v1): 3 chip `Tất cả | Spot | Futures` đặt trong CardHeader brief và lưới card dưới.
- Áp dụng: brief (Kết quả/Rủi ro/Hoạt động chỉ tính nhóm chọn), Danh mục (ẩn section tương ứng), Lệnh (filter `market` đã có trên AuditItem).
- Đặc tả: `aria-pressed` trên chip, group có `role="group"` + label "Phạm vi"; lựa chọn lưu localStorage cùng pattern `theme.ts:58-70`; giá trị "Futures" bao gồm derivatives (`market === "derivatives"` như `order-table.tsx:55`).
- Không tạo filter trang mới — filter thuộc dashboard shell, mọi section nhận qua prop (`scope: "all"|"spot"|"futures"`), để section độc lập vẫn test riêng được.

### 7.3 Coverage / source freshness

- Mỗi khối brief gắn nhãn nguồn+độ tươi: `generated_at` của brief, `positions_read.observed_at` cho vị thế, `window_start/window_end` cho coverage, `stale_after` cho poll.
- Hiển thị tuổi dữ liệu dạng "dữ liệu tại sàn lúc HH:mm" cho observed_at (khác generated_at — hợp đồng §6 đã phân biệt, UI không được trình bày generated_at như thời điểm sàn — `overview.ts:133-134` giữ nguyên).
- Coverage `drift/drift_pct` và `warnings[]` render **nguyên văn** trong khối Kết quả, kèm vị trí phụ "phần chưa đủ dữ liệu ghi rõ".
- Quy tắc stale: tái dùng `isFuturesStale`/`portfolioTimestamp` (`dashboard.tsx:59-76`) cho phần vị thế; brief tự tính stale trên `generated_at` + `stale_after` của chính nó.

### 7.4 Confirmed vs estimated net values (bất biến trung thực)

| Tầng | Nguồn | Nhãn hiển thị | Vi phạm cấm |
| --- | --- | --- | --- |
| Đã đối soát | outcome ledger (verified) | "Đã đối soát" + con số + đơn vị từng currency + drift công bố | không được gộp mixed-currency |
| ước tính | Spot audit FIFO, unrealized venue | "Ước tính (chưa đối soát)" màu `muted`, không tone profit/loss mạnh | không để cạnh số đã đối soát như cùng cấp mà không có nhãn |
| chưa đủ dữ liệu | null/unavailable/unattributed | "Chưa đủ dữ liệu" / "Lệnh ngoài bot: N" | **không render 0** thay null; funding None không diễn giải 0 (`position_cost.py:21-28`) |

Visual: tầng đã-đối-soát dùng `--profit/--loss` theo dấu như hiện có; tầng ước tính dùng `text-muted-foreground` + badge `secondary`; tầng thiếu dùng glyph "—"/text, không dùng màu đỏ.

### 7.5 Unlinked / manual fills

- `unattributed_count` và `unattributed_manual_detected` (migration 0008 khung, plan §2.5) render như **đếm riêng** trong khối Kết quả, không cộng PnL bot, không vào funnel lệnh.
- Copy vi: "Lệnh không xác định được nguồn (ngoài bot): N — không tính vào kết quả bot." Link "Đối soát ở Lịch sử" khi N>0.
- Không có hành động "gán về bot" trên UI — chống hồi tố là invariant (plan §2.1 nguyên tắc 2).

### 7.6 Action priorities — không live control

- `needs_attention` sắp theo `priority` 1→3, mỗi mục: icon mức (không thêm màu mới ngoài destructive/amber hiện có), message_code → i18n vi/en, link đích duy nhất.
- P1 (phải xử lý): credential lỗi, halt đang bật, protection không xác minh được, coverage `error`.
- P2: dữ liệu quá cũ (stale vượt `stale_after` × 3), lệnh chờ đối soát, coverage `partial` có drift.
- P3: theo dõi (pipeline_error_cycles tăng, shadow `unavailable` khi thiếu package — nêu rõ, không fake).
- **Cấm trên brief:** mọi nút POST/PUT/DELETE, nút duyệt/từ chối, reset halt, bật/tắt Auto, sửa risk. Các control đó chỉ tồn tại ở `/approvals` và `/settings` (giữ nguyên quyền sở hữu hiện có). Link "Mở màn Duyệt lệnh" là tương tác duy nhất khỏi khối này.

---

## 8. Seams đề xuất (web) — độc lập theo concern

| Seam | Biên giới | Trách nhiệm duy nhất |
| --- | --- | --- |
| **Dashboard shell** | `web/components/overview/dashboard.tsx` | Orchestration: thứ tự khối, scope filter state, refreshAll, clock; KHÔNG render nội dung dữ liệu |
| **Data hooks** | `web/components/overview/use-panel.ts` (giữ) + `web/components/overview/brief/use-brief.ts` (mới) | Mỗi endpoint một hook poll độc lập dùng `usePoll`; `use-brief.ts` định nghĩa `BriefPayload` types + fetch + normalize; không import section khác |
| **Brief** | `web/components/overview/brief/` — `brief-card.tsx`, `brief-section.tsx`, `attention-list.tsx` (seam đã duyệt plan §7.1) | Render bản tin; nhận `state: PanelState<BriefPayload>` + `scope`; tự xử lý 4 trạng thái + empty |
| **Risk** | phần "Rủi ro" trong brief + dữ liệu positions_read — không tạo store riêng | Trích dẫn từ portfolio state hiện có; số exposure/heat hiển thị mức dùng, KHÔNG hiển thị/sửa limit (`operator_config.py:56-107` chỉ đọc ngữ cảnh) |
| **Positions** | `portfolio-card.tsx` (nhà duy nhất của bảng positions) | Hiệu suất card KHÔNG còn nhân bản bảng này |
| **History** | `order-table.tsx` + fills/events | Cap dòng + link `/audit`; bảng fills giữ full ở `/audit` |

Quy tắc độc lập bắt buộc (khớp pattern hiện có `dashboard.tsx:352-354` đã comment): lỗi một section không render lan sang section khác; mỗi section tự bọc try-state; shell chỉ sắp thứ tự. Không nhét dữ liệu mới vào `use-overview-activity.ts` hay `job_store` — hook mới nằm trong seam của nó. Không thêm data-fetch library: pattern `usePoll` tự quản đã đủ với 1 endpoint mới (Next16 docs xác nhận library chỉ cần khi cần shared cache phức tạp — `node_modules/next/dist/docs/01-app/02-guides/client-side-data-fetching/index.md:17`).

---

## 9. Components hiện có giữ lại + chỗ hiển thị dữ liệu mới (hạng 1/2/4)

**Giữ nguyên:** `Card/CardAction` shell, `Badge`, `Button`, `EmptyState`, `usePoll`, `parseErrorResponse/FetchError`, `format.ts` (signed currency, pnlTextClass), `min-touch`, exec chip (giữ ngữ nghĩa gộp — per-mode status thuộc brief, không đổi chip để tránh đụng sở hữu settings), sidebar/nav, PageHeader, KpiCards, AlphaChart, OrderTable, PortfolioCard (positions), ActivityCard (tái dùng như nguồn Hoạt động chi tiết).

| Dữ liệu mới | Hạng | Hiển thị ở đâu |
| --- | --- | --- |
| Outcome ledger: net đã đối soát theo currency, coverage drift, unattributed | 1 | Khối "Kết quả" trong brief (dòng ngắn + tầng nhãn mục 7.4); chi tiết bảng đối soát **không** lên trang chính — link sang `/audit` khi implement mục đó; fills history thêm tag "đã đối soát/ước tính" khi data có |
| Futures simulation/parity report | 1b | Không lên dashboard trong delivery này (artifact CLI); chỉ một dòng "Báo cáo mô phỏng: xem artifact" nếu sau này có API — để slot trống, không thiết kế card |
| Experiments OOS (so sánh phiên bản) | 2 | Không có surface dashboard trong phạm vi; brief giữ một slot "So sánh phiên bản" chỉ bật khi có API read-only sau này — chống mở rộng scope ngầm |
| Kronos + filters (shadow) | 4 | Dòng "Quan sát" trong khối Hoạt động của brief: text mô tả phân phối đúng format báo cáo §5D ("72/100 kịch bản … sau 24h, dữ liệu ngày Y, provider rev") — tuyệt đối không chữ "xác suất đã hiệu chỉnh", không ảnh hưởng funnel/lệnh; `status: unavailable` khi thiếu package hiển thị rõ |
| "Cần xử lý" (aggregate) | 3 | Khối đầu brief, thay 2 banner riêng lẻ hiện có |

---

## 10. Nguyên tắc không làm trang rối (anti-clutter budget)

1. **Một dữ liệu, một nhà:** mỗi loại số chỉ render ở một card (positions→Danh mục; lệnh→History; tổng hợp→Brief).
2. **Ngân sách card mặc định:** 5 card hiển thị (brief, danh mục, hiệu suất quyết định, hiệu suất giao dịch, lệnh gần đây) — mọi thứ thêm phải là mục trong card hoặc link.
3. **Cap bảng:** bảng trong card tối đa 10 dòng hiển thị + "Xem tất cả" link (fills/events chuyển sang gọn: 10 dòng mới nhất).
4. **Ngân sách số/lều brief:** mỗi khối brief ≤ 5 dòng; lý do không-vào-lệnh chỉ top 3; shadow chỉ 1 dòng tổng + link.
5. **Một nút primary/trang:** "Làm mới" toàn trang; refresh per-card về icon-only `size="sm"`.
6. **Màu tối đa 3 tín hiệu:** profit/loss (đã có) + destructive cho P1; amber cho P2; phần còn lại neutral. Không thêm gradient/trang trí (anti-slop).
7. **Không số lặp:** nếu brief đã có số X, card chi tiết không cần badge tổng X nữa.

---

## 11. Visual contract (nhúng trong plan — KHÔNG tạo DESIGN.md riêng, không overwrite brand contract)

Chưa có `DESIGN.md` ở root hay `web/` (đã kiểm tra `dir` — mục 1.1). Theo design-knowledge-contract, hợp đồng dưới đây là **S4 project defaults** ghi trong plan này; stage implement phải tuân theo, và việc nâng lên `DESIGN.md` chính thức là quyết định riêng của user.

- **Theme/Atmosphere:** kỹ-calmb, sáng-tối 2 mode đã có (`.dark` tokens `globals.css:93-128`); nền neutral OKLCH, không gradient trang trí, không texture thêm.
- **Màu:** giữ nguyên palette hiện có; semantic `--profit` (lãi, kèm dấu +) / `--loss` (lỗ, kèm dấu −) / `--destructive`; màu không bao giờ là tín hiệu duy nhất (đã có badge chữ "Lãi/Lỗ" — `kpi-cards.tsx:37-40`).
- **Typography:** Geist Sans/Mono hiện có (`layout.tsx:10-18`); số tiền/khối lượng luôn `tabular-nums`; CardTitle `text-base font-medium`, số hero `text-2xl`; không thêm font.
- **Component styling:** dùng `ui/card|badge|button` không variant mới ngoài `secondary/outline/destructive` đã có; bảng: `text-sm`, header `text-xs text-muted-foreground`, divide-y (pattern `portfolio-card.tsx:265-279`).
- **Layout/spacing:** container chính `p-4 md:p-6` (`layout.tsx:50`); card gap 4 (`--card-spacing`); radius theo token hiện có.
- **Depth/elevation:** ring `ring-foreground/10` của Card — không thêm shadow mới.
- **Do:** số + dấu + đơn vị; empty có hướng đi; lỗi có retry; 44px targets; focus-visible.
- **Don't (anti-slop):** không "elevate/seamless" copy; không spinner toàn trang cho khối có skeleton; không card 3 cột đều chỉ vì đẹp; không thêm icon lucide ngoài bộ đang dùng; không dùng màu profit/loss cho dữ liệu ước tính.
- **Responsive/touch:** mục 5; **agent prompt guide:** mọi code UI mới phải qua W1–W6 mục 12 trước khi ship.

---

## 12. Kế hoạch kiểm chứng (chạy khi stage `dashboard` implement)

Web evaluation — dashboard `/` sau redesign. Surface: responsive web. Input: keyboard/pointer/touch.

- **W1 target size:** mọi nút/link mới `min-touch` ≥44px — kiểm bằng snapshot a11y + đo getBoundingClientRect.
- **W2 contrast & non-color:** giữ muted ≥4.5:1 (đo hiện tại ≈4.73:1); status không chỉ màu (badge chữ); kiểm lại sau khi thêm nhãn tầng ước tính.
- **W3 keyboard/focus/semantics:** CardTitle → `h2`; "Chi tiết" link có tên truy cập riêng (thêm context "Chi tiết — Autoloop"); tab order theo DOM dọc; `aria-pressed` chips scope.
- **W4 gestures:** không thêm gesture mới (chỉ scroll/click) — N/A.
- **W5 reflow:** mobile 390: brief không scroll ngang; bảng chi tiết giữ 2D scroll có bọc (exception có chủ đích); zoom 200% không mất nội dung.
- **W6 product quality:** state matrix mục 6 đầy đủ từng khối; không có 0 thay null; "không có tín hiệu" ≠ "pipeline lỗi" được test pin.

Lệnh nghiệm thu stage (do runner chạy — plan §5.4): `node node_modules/typescript/bin/tsc --noEmit` (hoặc `npm run typecheck` sau khi thêm script), `npm run lint`, `next build` với `NEXT_DIST_DIR=.next-verify`, và `tests/browser/test_dashboard_brief_offline.py` (integration): fixture server cục bộ trả JSON canned; assert (1) brief render đúng fixture, (2) trường thiếu dữ liệu render explicit không 0, (3) không request ra ngoài localhost. **Các lệnh trên chưa chạy trong ask này** (ask này là thiết kế, không có thay đổi code web) — ghi rõ: chưa chạy, không fake pass.

**Gate runner — trạng thái sau kiểm tra độc lập (đọc trực tiếp 2026-11-10):** 3 blocker của kiểm tra độc lập hai kế hoạch đã được vá trong `scripts/verify_research_delivery.py` hiện tại: (B3) `run_stage` có nhánh dashboard riêng chạy brief pytest + web typecheck/lint/build + browser-offline cùng lúc, exit 1 khi có FAIL / exit 2 khi còn not-implemented (:218-219, :234-257) — khớp yêu cầu gate `verify --stage dashboard` exit 0 của plan §5.4/§9; (B2) `_browser_offline_check` truyền `-m integration` tường minh (:329) nên addopts `-m "not integration"` không còn deselect test; (B1) `note()` phân loại 0=pass / 2=skip / còn lại=FAIL với chú thích "a part that ran and failed can never land in skipped" (:347-356). Không phát hiện blocker còn lại thuộc phạm vi UX plan; blocker provenance/attribution (B4/B5/B7) và CHECK `unsupported` (B6 — CHECK ở migration 0008) thuộc plan kỹ thuật, owner của plan đó xử lý.

Kết quả chạy trong ask này (bằng chứng audit, không phải nghiệm thu stage): navigate + screenshot + `evaluate_script` trên `http://localhost:3000/` như mục 1.2; không POST/PUT/DELETE nào được gọi.

---

## 13. Rủi ro mở

1. `GET /api/brief` aggregate mới cần server-side dedup "Cần xử lý" — nếu để client dedup dễ lệch giữa vi/en; đã chỉ định dedup ở server (mục 7.6), stage implement pin shape cuối.
2. Bỏ khối positions khỏi PerformanceCard là điều chỉnh UX trong redesign đã duyệt (hạng mục 3) nhưng nằm ngoài mô tả "giữ nguyên card hiện có" của plan §5.2 — **mặc định khi implement là GIỮ NGUYÊN performance-card; không sửa performance-card cho tới khi user xác nhận lời rõ ràng** với đề xuất dedup này. Kiểm tra độc lập hai kế hoạch (2026-11-10) đã cờ lại điểm này.
3. Shadow observations vào brief chỉ khi API đã tồn tại; nếu stage `shadow` chưa xong, brief render slot rỗng "Quan sát: chưa bật" — không fake.
4. Server đang bind all-interfaces (`0.0.0.0:3000/:8000` đo bằng netstat trong phiên này) — ngoài phạm vi UX nhưng operator nên rà lại authentication trước khi brief hiển thị venue production.
5. Contrast muted là suy toán từ L\* đo được, chưa chạy tool kiểm chứng tự động — re-check bằng tool khi implement W2.
6. Vocabulary `unsupported` (blocker B6 của kiểm tra độc lập 2026-11-10): CHECK ở migration 0008 (plan kỹ thuật §2.5) ghi 4 giá trị trong khi Literal nguồn có 5 (ok/partial/error/**unsupported**/unavailable — `server/futures_trade_history.py:48`; `performance_history.py:201-203` trả `"unsupported"` + warning `unsupported_exchange`). Quyết nới CHECK hay map unsupported→unavailable thuộc owner plan kỹ thuật; UX plan dựng để chạy đúng cả hai (mục 6 state matrix + §7.1): nếu ingest map, UI phải đọc warning `unsupported_exchange` để hiện copy "Sàn không được hỗ trợ" thay vì copy "chưa bật/chưa đọc" của unavailable.

## 14. Nguồn tham chiếu

- Wiki: `queries/design-intake-and-direction-runbook.md`, `queries/web-ui-ux-design-runbook.md` (vault agent-wiki; đã đọc nguyên văn trong phiên).
- Skill knowledge: knowledge-retrieval (design-knowledge-contract), redesign-existing-projects (audit checklist), ux-quality-gates (gates/review output).
- Repo: `AGENTS.md`, `web/AGENTS.md`, `docs/ui-api-contract.md` §0/§6/§7/§8/§9/§12–15, `docs/plans/trading-research-delivery-20261110.md`, `docs/research/2026-11-10-carm1nee-ai-trading-stack-report.vi.md` §5A–5F/§7, các file web/ liệt kê mục 1.1.
- Next16: `web/node_modules/next/dist/docs/01-app/02-guides/client-side-data-fetching/index.md` (pattern data-fetching, không cần thêm library).
