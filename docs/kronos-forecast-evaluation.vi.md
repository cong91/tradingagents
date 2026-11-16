# Đánh giá forecast Kronos và quy trình chỉnh ngưỡng

## Mục tiêu và giới hạn

Bộ đánh giá trả lời: **“Forecast đã ghi trước đó khớp với giá đóng tại horizon đến mức nào?”** Kết quả gồm lỗi giá, đúng/sai hướng và tỷ lệ giá đích nằm trong band. Những số này không tự chứng minh lợi nhuận hay alpha. Kết quả giao dịch thực phải xem riêng trong outcome ledger, với đủ bằng chứng về fill, phí, funding và phạm vi đối soát.

Kronos active vẫn được thử trước graph/LLM cho ứng viên crypto Futures đủ điều kiện; thiếu forecast dùng được thì hoãn ứng viên. `forecast_evaluation_enabled` chỉ điều khiển worker đánh giá kết quả về sau, không phải công tắc bật/tắt Kronos. Worker không tham gia graph/PM, không đổi lệnh, risk limit hay quyết định Auto. Các threshold hiện tại giữ nguyên.

## Warm reuse và đầu vào thực tế

**Warm reuse** nghĩa là dùng chung service/sampler đã khởi tạo cho các ứng viên trong **một lượt dispatch Auto Futures**. Mỗi lượt có một owner, tạo service khi thực sự cần và đóng sau khi kết thúc lượt, khi các lease đang dùng đã được trả. Lượt Futures tiếp theo có owner mới. Mỗi ứng viên vẫn đọc, kiểm tra lịch sử riêng, có seed và số nến đầu vào riêng, đồng thời chịu deadline chung của cycle.

Đây là thay đổi vòng đời model. Chưa kết luận giảm độ trễ cho đến khi đo trực tiếp trong vận hành. `latency_ms` ghi thời gian thực của lần gọi forecast, gồm đọc lịch sử và khởi tạo nếu có; cần so sánh cùng profile, timeframe, số nến, horizon và phạm vi đo trước khi nói nhanh hơn.

`timeframe` theo khung **Auto Futures đang chọn** (`auto_futures_timeframe`), không cố định 4h. Forecast phải kết thúc đầu vào ở đúng nến đã chọn của cycle. Cặp xác nhận 1h/4h của execution là một kiểm tra riêng.

Profile đặt trần lookback yêu cầu: `small_base` là 512 nến, `mini_2k` là 2048. Nếu venue trả ít hơn trần nhưng vẫn đủ tối thiểu 32 nến đóng, liên tục, đúng symbol/timeframe và cutoff, forecast có thể dùng cửa sổ thực tế đó. `effective_lookback_bars` ghi **số nến thực sự đưa vào model**, không ghi trần như thể đã đọc đủ.

Ví dụ, `effective_lookback_bars=300` nghĩa là model dùng 300 nến, không phải điểm chất lượng 300, tỷ lệ 300/512 hay bằng chứng forecast tốt hơn. Trường thiếu hoặc `null` là chưa biết số đếm; không thay bằng trần profile. `ohlc_quality` là thông tin nhất quán/hiệu chỉnh OHLC của **đường dự báo đầu ra**, không phải điểm chất lượng dữ liệu đầu vào hoặc accuracy.

Nguồn: `server/autoloop.py`, `server/kronos_lifecycle.py`, `server/kronos_context.py`, `tradingagents/forecast/settings.py`, `tradingagents/forecast/artifact.py`, `tradingagents/forecast/service.py`, `tradingagents/forecast/ohlc.py`.

## Worker thu thập những gì

| Setting | Mặc định | Ý nghĩa |
|---|---|---|
| `forecast_evaluation_enabled` | `false` | Bật worker đánh giá; env tương ứng `TRADINGAGENTS_FORECAST_EVALUATION_ENABLED` |
| `forecast_evaluation_interval_seconds` | `60` | Khoảng nghỉ giữa các lượt poll, tính bằng giây; env `TRADINGAGENTS_FORECAST_EVALUATION_INTERVAL_SECONDS` |

Đây là engine settings, đọc/ghi qua `/api/settings` và lưu PostgreSQL theo [hợp đồng settings](ui-api-contract.md#1-settings--get-apisettings-put-apisettings). Chuyển từ tắt sang bật đặt mốc thu thập mới; không chủ động backfill forecast tự khai trước mốc bật. Mỗi lần server khởi động với worker được bật cũng đặt mốc mới một cách bảo thủ, nên forecast chưa thu thập trong thời gian server ngừng sẽ không được backfill. Tắt dừng polling, giữ nguyên snapshot đã lưu; bật lại đặt mốc mới cho thu thập mới. Nếu một lượt đọc không dừng trong ngân sách shutdown, thao tác tắt báo lỗi và worker giữ trạng thái bị chặn; không khởi chạy worker thay thế khi lượt cũ chưa dừng. API báo cáo vẫn đọc được dữ liệu đã lưu khi worker tắt.

Cycle lưu snapshot `kronos_forecast` đã redact cùng `kronos_venue_scope`. Bộ đánh giá dùng bằng chứng đã lưu, không chạy model hiện tại để dựng lại forecast cũ. PostgreSQL giữ kết quả trong `forecast_evaluations`, cursor trong `forecast_evaluation_state` (migration `0015`); khóa `(cycle_id, forecast_digest, horizon_key)` ngăn ghi trùng cùng forecast/horizon. Snapshot ghi nhận profile, timeframe, effective input, thời điểm forecast, hướng, band, giá tham chiếu, venue và kết quả hoặc lý do chưa chấm.

Worker chỉ dùng **public market-data reads**, không credential, tại venue/profile đã ghi. Nó đọc market metadata và nến đóng; không đọc số dư, vị thế, fill/funding riêng tư, không đặt/hủy lệnh. Worker xử lý forecast trong cycle Futures đã kết thúc và thử lại các dòng `pending` hoặc `unavailable` có target đến hạn, kể cả snapshot đã thu thập trước khi worker được bật lại. Một forecast thất bại không có target không thể tự trở thành forecast hợp lệ bằng cách poll lại; nó được giữ lại với cảnh báo riêng. Việc cycle không vào lệnh không tự loại một forward forecast hợp lệ khỏi đánh giá giá thị trường.

Nguồn: `tradingagents/default_config.py:100`, `tradingagents/default_config.py:387`, `server/settings.py:206`, `server/autoloop_kronos.py:75`, `server/futures_cycle_telemetry.py:253`, `server/forecast_evaluation_store.py:76`, `server/forecast_evaluation_venue.py:20`, `server/migrations/0015_forecast_evaluations.sql:4`.

## Khi nào một forecast được chấm

Chỉ forecast `status="ok"`, `generation_kind="forward"` với identity, timestamps, giá tham chiếu, p50 và band hợp lệ mới đủ điều kiện. Forecast malformed, hồi cứu (`retrospective`) hoặc có thời điểm không mô tả quyết định forward bị đánh dấu `invalid`; không được tính như dự báo sai và không vào mẫu số metric. `recorded_at` do ứng dụng tạo trước khi gọi ghi cycle, **không phải thời điểm PostgreSQL xác nhận commit**. Scoring so sánh các timestamp này ở độ chính xác microsecond và gắn `provenance.timestamp_evidence="self_reported_unverified"`; báo cáo luôn cảnh báo giới hạn này. Chúng phụ thuộc đồng hồ ứng dụng đồng bộ và không chứng minh độc lập rằng snapshot đã được lưu trước khi nến dự báo đầu tiên đóng. Vì vậy nhãn `forward` và các metric hiện tại chỉ là nghiên cứu theo thời điểm tự khai, chưa đủ bằng chứng để xác nhận out-of-sample hay đổi threshold.

Mốc đích tính từ **nến đầu vào cuối cùng**:

```text
target_as_of = data_open_time + (horizon_bars + 1) × timeframe_duration
```

Các giá trị thời gian trong công thức tính bằng mili giây UTC. Một bước đầu tiên đóng nến đầu vào; `horizon_bars` bước tiếp theo là các nến dự báo. Ví dụ minh họa: đầu vào cuối cùng là nến 1h mở 08:00, đóng 09:00; horizon 2 nến kết thúc 11:00. Trước 11:00 chưa có kết quả. Sau 11:00 phải đọc đúng nến 10:00–11:00 đã đóng; nến 09:00–10:00 hoặc 11:00–12:00 không thay thế được.

| Trạng thái dòng | Diễn giải | Chấm metric |
|---|---|---|
| `pending` | Horizon chưa đóng, đang chờ đến hạn | Chưa chấm |
| `unavailable` | Thiếu client public, venue scope hoặc đúng nến target: worker thử lại khi có target; forecast thất bại không có target thì không thể thử lại snapshot đó | Chưa chấm |
| `invalid` | Forecast malformed, retrospective hoặc không đủ điều kiện forward | Không chấm |
| `evaluated` | Đã có đúng nến target đóng, khớp symbol/timeframe và identity | Chấm các metric xác định được |

Worker chỉ chấm sau khi horizon đóng và có nến đúng `target_as_of`. Nó không lấy giá hiện tại, nến gần nhất hay thời điểm poll làm kết quả thay thế. `pending`/`unavailable` là **chưa biết kết quả**, không phải sai, lỗ hoặc 0. Trường kết quả chưa xác định giữ `null`.

Nguồn: `server/forecast_evaluation_scoring.py:47`, `server/forecast_evaluation_scoring.py:144`.

## Đọc các phép đo

- `predicted_close = band.p50`: median của giá đóng cuối các đường mô phỏng.
- `absolute_error = abs(target_close - predicted_close)`: sai số giá tuyệt đối.
- `absolute_error_pct = absolute_error / abs(predicted_close) × 100`: mẫu số là **`predicted_close`**, không phải giá thực tế hoặc giá tham chiếu. Provenance ghi `error_pct_denominator="predicted_close"`.
- `target_return = (target_close - reference_close) / reference_close`: biến động thị trường từ giá tham chiếu tới đích, dạng tỷ lệ; 0.16 là 16%, không phải PnL của vị thế.
- `directional_correct`: so hướng `up`/`down`/`flat` với thay đổi từ `reference_close` tới `target_close`. Hướng `unclear` giữ `null`, không tính thành sai hay đúng; vẫn có thể tính lỗi giá và containment.
- `band_contains_target`: kiểm tra `band.low <= target_close <= band.high`. Đây là containment của band phân vị 10–90% trên mẫu model, không phải xác suất được hiệu chỉnh.

Ví dụ minh họa: p50 là 110, giá đích 116 và giá tham chiếu 100 thì lỗi tuyệt đối là 6, lỗi phần trăm khoảng 5.45% (`6/110 × 100`), `target_return=0.16`. Những số này chưa tính phí, funding, slippage hay giá vào/ra thực tế.

Nguồn: `server/forecast_evaluation_scoring.py:231`.

## Endpoint báo cáo và coverage

`GET /api/forecast-evaluations?limit=100&symbol=BTC/USDT:USDT` chỉ đọc snapshot trong database, không gọi venue và không bật worker. `symbol` lọc đúng unified Futures symbol đã lưu. `limit` mặc định 100, phạm vi 1–500, **chỉ giới hạn chi tiết `items`**; `counts` và metrics tính trên **mọi dòng khớp bộ lọc**, công bố bằng `metrics_scope="all_matching_rows"`.

- `total_count` là mọi dòng khớp; `counts` tách `pending`, `unavailable`, `invalid`, `evaluated`. `sample_count` chỉ đếm `evaluated`, không phải số trade hoặc số đường mô phỏng.
- `directional_sample_count` và `band_sample_count` là mẫu số riêng. `directional_accuracy` và `band_containment` là tỷ lệ 0–1; không có mẫu thích hợp thì `null`, không thay bằng 0%.
- Response `status="unavailable"` khi không có dòng; `complete` khi tất cả dòng khớp đã `evaluated` và không có cycle thiếu thời điểm ghi trong phạm vi kiểm tra; còn lại `partial`. `complete` chỉ mô tả các dòng đã lưu khớp bộ lọc, không chứng minh đã quan sát mọi forecast hoặc toàn tài khoản.
- `coverage` chứa các số nguyên `total_count`, `evaluated_count`, `pending_count`, `unavailable_count`, `invalid_count` và `metrics_scope="all_matching_rows"`. `nonretryable_unavailable_count` đếm forecast thất bại không có target nên không thể thử lại snapshot. `unproven_recording_time_count` đếm riêng các cycle terminal từ mốc bật hiện hành có snapshot thiếu hoặc sai định dạng `recorded_at`; không đưa chúng vào mẫu số metric vì không biết forecast có sau mốc bật hay không. Đây là số cycle cần lưu ý, không khẳng định mọi cycle đó đã được tạo sau khi bật. `coverage_warnings` luôn nêu giới hạn timestamp tự khai và thêm cảnh báo nếu có cycle không xác định được thời điểm ghi, chưa có dòng, horizon còn chờ, dữ liệu unavailable cần thử lại hoặc bằng chứng invalid bị loại. Hiển thị cảnh báo cùng metrics, kể cả khi `items` chỉ là một phần chi tiết.
- `generated_at` là lúc dựng HTTP response, không phải lần lấy nến mới. Đọc cùng `forecast_as_of`, `target_as_of`, `last_attempted_at`, `evaluated_at`, `coverage`, `coverage_warnings`, `items[].warnings` và lý do chưa chấm; refresh GET không tự làm dữ liệu thị trường mới hơn.

Đọc schema chi tiết ở [API forecast evaluations](ui-api-contract.md#20-forecast-evaluations--get-apiforecast-evaluations). Khi so các nhóm profile/timeframe/effective input khác nhau, phải giữ rõ coverage và số mẫu; không xem một trang chi tiết bị giới hạn là toàn bộ tập metric.

Nguồn: `server/forecast_evaluations_api.py:11`, `server/forecast_evaluation_store.py:236`.

## Phân biệt với kết quả giao dịch ròng

Forecast evaluation đo **giá thị trường**, gồm cả cycle không vào lệnh. Nó không biết giá khớp thực, size, đòn bẩy, stop/target đã thực thi hoặc chi phí tài khoản. Accuracy cao, band chứa đích nhiều hoặc sai số nhỏ chưa chứng minh chiến lược sinh lời.

`GET /api/outcomes` đọc sổ kết quả giao dịch thực đã ingest và các giới hạn đối soát: realized, phí, funding, currency và coverage. Net chưa đủ bằng chứng phải giữ `null`; sổ rỗng là `unavailable`, không phải 0 PnL. Không cộng lẫn currency và không biến `target_return` thành net return. Chi tiết ở [outcome ledger](ui-api-contract.md#16-outcome-ledger--get-apioutcomes-rev-7).

Nguồn: `server/outcomes_api.py:18`, `server/outcome_report.py:139`, `server/outcome_report.py:174`.

## Hiểu “chỉnh threshold”

Threshold là mức cắt mà một chính sách dùng để phân loại hoặc quyết định cách xử lý dữ liệu. Hiện `max_band_spread_ratio=0.15` khiến forecast có band quá rộng mang hướng `unclear`; forecast vẫn được đưa vào context nếu hợp lệ. Kronos active không tự lọc entry theo một ngưỡng direction-ratio. Agent quyết định với context, còn engine giữ các kiểm tra execution/risk riêng.

Direction-ratio tối thiểu hoặc expected move tối thiểu sau phí vào/ra, funding và slippage có thể là **tiêu chí nghiên cứu được đề xuất**. Đây chưa phải các gate Kronos được bộ đánh giá thêm vào. Nếu muốn áp dụng, phải nêu chính sách chính xác và kiểm chứng như một thay đổi riêng. Số nến, độ mới, tính liên tục và provenance cũng cần được kiểm tra, nhưng `effective_lookback_bars` tự nó không phải quality score.

Đổi một mức cắt có thể đổi nhãn, coverage và tập quyết định được xem xét. Không đổi threshold chỉ để giảm số lần defer hoặc tăng entry. Warm reuse, effective input và worker đánh giá không tự chỉnh threshold.

Nguồn: `tradingagents/forecast/settings.py`, `tradingagents/forecast/service.py`, `server/kronos_context.py`.

## Quy trình trước khi đề nghị thay đổi

1. Thu thập snapshot **forward** mới và kết quả đúng horizon; báo cáo coverage, số mẫu và lý do unavailable/invalid, tách theo venue/profile, timeframe, horizon, effective input. Không dựng lại forecast quá khứ rồi gọi đó là bằng chứng forward.
2. Chia dữ liệu theo thời gian thành giai đoạn phát triển và holdout. Không xáo trộn ngẫu nhiên các cycle có horizon chồng lấn; tránh để kết quả ở giai đoạn sau lọt vào giai đoạn chọn ngưỡng.
3. Khóa tiêu chí và chọn ngưỡng chỉ trên giai đoạn phát triển. So với baseline đơn giản như giữ nguyên giá cuối, và chính sách không dùng Kronos trong thí nghiệm offline; dùng cùng cửa sổ, dữ liệu và cost model.
4. Báo cáo lỗi giá/return, directional accuracy, band containment, coverage, số mẫu và khoảng bất định. Đánh giá kinh tế riêng với phí vào/ra, funding, slippage và bằng chứng khớp lệnh/attribution; nếu là mô phỏng, ghi rõ giả định và phân biệt với outcome ledger thực.
5. Giữ holdout theo thời gian hoàn toàn chưa dùng để chọn ngưỡng; khóa cấu hình trước khi đánh giá và chỉ xác nhận một lần trên holdout. Không liên tục xem holdout rồi chỉnh ngưỡng theo kết quả đó.
6. Trình báo cáo offline cùng baseline, chi phí, giới hạn dữ liệu và policy diff cụ thể để operator phê duyệt trước khi đổi policy. Kronos active không có Shadow mode hoặc chế độ report-only mới; runtime không đọc, ghi hoặc xóa các bảng Shadow lịch sử.

Cho tới khi đủ mẫu ngoài giai đoạn phát triển và có so sánh sau chi phí, các threshold hiện tại được giữ nguyên. Đây là công cụ nghiên cứu, không phải lời khuyên tài chính.
