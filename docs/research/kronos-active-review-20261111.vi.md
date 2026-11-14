# Rà nghiệm thu Kronos active — 2026-11-11

> **Lưu trữ lịch sử — đã thay thế 2026-11-12:** Các nhận định và đề xuất về công tắc `kronos_active_enabled`, Shadow và `pass_through` trong báo cáo này không còn hiệu lực. Quy định hiện hành tại `AGENTS.md`: Kronos luôn được thử cho ứng viên Futures đủ điều kiện, không có công tắc hay Shadow; nếu không có dự báo hợp lệ thì hoãn ứng viên.

Trạng thái: có lỗi chặn đã tái hiện offline; chưa là bàn giao hoàn tất. Phạm vi vẫn là ledger/mô phỏng, experiments, dashboard và Kronos làm đầu vào thật trước Trader/PM. Không deploy, không đổi Auto/venue/risk runtime, không đặt lệnh, không commit/push.

## Bằng chứng đã tái hiện

Kiểm tra bằng interpreter `.venv\Scripts\python.exe`, dùng các fake sampler trong `tests/test_forecast_service.py`, không gọi model hoặc mạng. Kết quả:

```json
{"stale_status":"ok","mixed_symbol_status":"ok","requested_paths":16,"accepted_paths":1,"changed_candle_cache_reused":true,"timed_out_child_killed":false}
```

- `series_problem` chỉ kiểm tra nến không đóng SAU `as_of`, chưa từ chối series đã cũ một ngày; cũng không đối chiếu `candle.symbol` với symbol được yêu cầu. Dữ liệu cũ/sai mã có thể đi vào prompt như dự báo hợp lệ.
- `cache_key` gồm mốc nến nhưng thiếu nội dung OHLCV và venue/market identity. Sửa giá đóng ở cùng mốc vẫn lấy lại nguyên block cũ.
- `_block_from` nhận một path mặc dù yêu cầu 16; chưa kiểm đủ số path, độ dài horizon hoặc finite/positive price trước khi gắn `status=ok`.
- `KronosSampler.close` đặt `self._process=None` trước khi gọi `self.kill()` khi `wait` timeout. `kill()` vì vậy không còn child để dừng.

## Phát hiện từ đường gọi thực tế, cần test pin khi sửa

- `server/autoloop.py:_kronos_block_for` luôn truyền `service=None`; `server/kronos_context.py:forecast_for_cycle` dựng service mới nhưng không đóng service thuộc quyền sở hữu của nó. Process warm/cache không được tái sử dụng; model child có thể sống sau mỗi ứng viên. Chọn ownership rõ ràng và kiểm cleanup success/error/timeout. Nếu dùng one-shot có bounded cleanup, ghi đúng chi phí cold start; không tuyên bố warm reuse khi runtime không làm.
- `KronosForecastService.from_config` dựng sampler + `artifact_digest()` nhưng không gọi `verify_artifacts`. Runner active import source/weights mà không có hash preflight như `shadow.KronosProvider`. Hash khai báo chưa phải hash đã kiểm.
- `kronos_policy='defer'` mới được ghi vào payload; `_process_candidate` vẫn gọi graph khi forecast lỗi. Policy phải có tác dụng thực tế hoặc bỏ lựa chọn không có tác dụng. Với active mặc định `defer`, thiếu/lỗi/stale phải hoãn ứng viên trước LLM; `pass_through` phải được ghi rõ là tiếp tục không có dự báo. `context_only` không phải hard direction filter.
- Deadline/cancellation chỉ được kiểm trước/sau inference; child có deadline riêng 600s không theo phần thời gian cycle còn lại. Truyền budget/cancel qua seam mới và không giữ execution/protection lock.
- `paths>=16` chỉ là cấu hình sampling, không chứng minh dự báo hơn ngẫu nhiên hoặc win probability; sửa những docstring khẳng định quá mức. `generation_kind` phải phân biệt dự báo tạo trong tương lai đối với quyết định historical, không dùng nhãn `forward` làm bằng chứng OOS/alpha.

## UI và nghiệm thu còn thiếu

- Settings chưa có nhóm/metadata cho active Kronos; operator row `group=forecast, gate=true` hiện bị xếp chung vào modes. Cần hiện trong khu AI/dự báo với mô tả không phải công tắc đặt lệnh, giữ confirm/reason/audit theo backend hiện có và form save/error/draft semantics.
- Dashboard/cycle chưa hiển thị active `kronos_forecast`/`kronos_policy`; dữ liệu shadow không thay được thông tin active. Cần disclosure gọn: status/mode, symbol, input cutoff, generated time/horizon, revision, count + sample ratio/band, wall latency, lỗi/unknown; không claim saved LLM calls hoặc PnL do Kronos.
- Browser fixture gọi `next dev --webpack`, chưa phải `next start` production dù runner/docs nói production. Có 9 scenario, timeout 300s mỗi scenario; renderer DevTools ngoài process tree test không phải bằng chứng test tiến triển.
- Báo cáo dashboard hiện ghi chưa có screenshot desktop/mobile, chưa đóng W1–W6 và mobile header/attention issues. Cần hoàn thiện trong test-owned instance.
- Các test performance render vẫn import module vị thế đang được bỏ; sửa test về component sở hữu dữ liệu hiện tại, giữ nguyên currency/source/freshness/quantity assertions, không bỏ kiểm chứng.

## Lỗi tài liệu cần sửa khi hoàn thiện UI/handoff

`git diff -- docs/ui-api-contract.md` cho thấy toàn bộ phần tiếng Việt cũ bị UTF-8 đọc nhầm thành Windows-1252 rồi ghi lại (ví dụ `của` → `cá»§a`), kèm BOM mới ở đầu file. Đây không phải churn whitespace: diff có 507 additions / 421 deletions. Khi hoàn thiện UI/API docs, khôi phục đúng các dòng bị mojibake bằng đối chiếu nội dung HEAD và giải mã nghịch có kiểm tra; giữ nguyên các phần API mới thật sự đã bổ sung. Không reset file về HEAD làm mất hợp đồng mới. Dùng `encoding="utf-8"` khi đọc/ghi, không ghi BOM hoặc ký tự thay thế U+FFFD; kiểm tra diff cuối để phần cũ giữ nguyên tiếng Việt và chỉ còn thay đổi có chủ đích.

## Thứ tự đóng công việc

1. Chốt kết quả run đang chạy, không khởi chạy verifier thứ hai tranh build/PG.
2. Sửa neutral forecast + wiring nhỏ tại seam Kronos đã được giao; test regression phản ánh các lỗi trên. Không extract discovery/refill hay refactor unrelated.
3. Hoàn thiện Settings/Dashboard/i18n với UX hiện có; review độc lập một lần cho correctness và một lần rendered UX.
4. Chạy stage Kronos/dashboard rồi full acceptance đúng một lượt sau khi source ổn; khi fail sửa đúng nguyên nhân. Gate giữ exit codes, output đầy đủ và tóm tắt command/count/skip, không để warning tail che failure.
5. Bàn giao cả bốn hạng mục bằng trạng thái code hiện tại; nêu context-only, mặc định off và chưa có bằng chứng lợi nhuận. Không gọi hoàn tất nếu có test thực sự còn đỏ.
