# Hoàn thiện Kronos và tách trách nhiệm — 2026-11-11

> **Lưu trữ lịch sử — đã thay thế 2026-11-12:** Yêu cầu về `kronos_active_enabled`, công tắc Kronos và Shadow trong kế hoạch này không còn hiệu lực. Kronos luôn được sử dụng cho ứng viên Futures đủ điều kiện; chỉ chọn cặp model/tokenizer, và hoãn ứng viên khi không có dự báo hợp lệ. Xem quy định hiện hành trong `AGENTS.md` và `docs/operator-guide.md`.

Người dùng yêu cầu đóng bốn điểm còn lại (fixture khi mở thủ công, browser test đúng công tắc Kronos, lookback >200 và deadline pipe write), đồng thời duyệt tách settings/rendering khỏi forecast service và tách autoloop khi cần. Phê duyệt này áp dụng cho đợt code local hiện tại; không phải cho deploy, bật Auto/Kronos runtime, sửa venue/risk, giao dịch hoặc commit/push.

## Kết quả cần đạt

1. Fixture luôn dùng dữ liệu test dù mở bằng browser thường không có CDP init; cấu hình fixture không có rewrite tới runtime API. Test phải chứng minh default badge, unknown API local và trap upstream không nhận request.
2. Browser test render Settings thật, dùng đúng `kronos_active_enabled`; enable/confirm/reason/save/undo/disable, domain error và thay đổi server được kiểm, Auto modes không bị thay. Test có trong runner với `-m integration` và không được skip thành pass.
3. Lookback 201/256/512 được yêu cầu và sử dụng đúng, thiếu lịch sử được từ chối rõ; default 96 giữ nguyên. Không cắt âm thầm tại 200.
4. Một deadline tuyệt đối bao trùm stdin write/flush và stdout answer. Child không đọc payload lớn phải timeout, kill/reap và giải phóng thread/pipe test sở hữu. Regression có outer timeout để lỗi cũ không làm treo pytest vô hạn.
5. `forecast/settings.py` sở hữu tuning; `rendering.py` sở hữu prompt rendering. Types, inputs và cache có module riêng khi cần để service chỉ điều phối. Caller dùng trực tiếp nơi sở hữu, không wrapper cũ.
6. Autoloop tách discovery/preflight/refill, execution và context theo trách nhiệm; scheduler/API giữ orchestration. Authorization/revocation, deadline/cancel/no-late-write, venue/account identity, RiskGuard/confirmation/sizing/brackets/protection giữ semantics, chỉ sửa lỗi thật có regression riêng.
7. Rà cấu trúc các file đã chạm, không compatibility shim/catch-all/dead imports; independent review và full acceptance trên source cuối.

## Quyền sở hữu thay đổi

- Forecast worker: `tradingagents/forecast/*`, `server/kronos_context.py`, forecast imports của settings/operator-config, test forecast và smoke.
- Browser worker: fixture lifecycle, dashboard browser scenario, test riêng Kronos Settings và capture script.
- Autoloop worker: autoloop modules, direct callers và test patch sites của phần được chuyển.
- Coordinator: verifier bắt buộc browser test mới, test runner contract, sửa `SettingField` nối hint/unit/error qua aria-describedby, integration/review/gates và handoff.

## Kiểm chứng

Focused regression theo concern trước; không chạy nhiều full suite đồng thời. Final command `.venv\Scripts\python.exe -u scripts\verify_research_delivery.py --full` bao gồm PG disposable, pytest mặc định, migration integration, web build/typecheck/lint, hai browser test selection và smoke model thật. Kết quả, count, skip và command output cần đọc và lưu rõ. `.venv\Scripts\python.exe -m ruff check .`, clean imports và `git diff --check` phải đạt sau source cuối. Full runner không thay thế tuyên bố live venue/profitability; không làm các việc đó trong đợt này.

Standards: AGENTS.md, wiki cross-language/Python cookbooks đã đọc qua Obsidian, local Next16 docs và cấu trúc/test repo hiện có. Chỉ dùng toolchain venv repo, văn bản UTF-8, Python floor 3.11; không mass format hoặc thêm dependency.

## Kết quả nghiệm thu

- Bốn gap có implementation và regression đạt; forecast/autoloop cùng test monolith đã tách theo owner thật, không compatibility wrappers.
- Full runner exit 0: 4403 passed, 6 skipped, 29 deselected, 100 subtests; migration integration 2, browser 13, model thật shadow 8 + active 4; web build/typecheck/lint và ruff/import đạt.
- Sau full: sáu test groups tách layout chạy riêng với PG disposable đạt 87; browser serverAhead và POSIX fixture lifecycle correction đã chạy lại 13 browser cases đạt 121.42s. Ba regression ownership mới nằm trong nhóm cuối 23 test đạt 3.40s. Không có thay đổi logic forecast/trading sau full.
- Rà tracked/untracked module boundaries, import/callers; dependency graph autoloop không cycle. 122 file Python mới/sửa parse theo 3.11; tsconfig khớp HEAD, PG test được dọn, diff-check sạch.
- Independent reviewer bị provider từ chối trước kết quả; mục review độc lập chưa có bằng chứng mới. Coordinator self-review và verification được ghi rõ, không thay tên thành independent review.
- Bàn giao chi tiết: `docs/research/kronos-final-completion-20261111.vi.md`; handoff tổng đã cập nhật. Workflow cũ giữ nguyên, không deploy/arm/trade/commit/push.
