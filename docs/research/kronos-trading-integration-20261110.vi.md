---
purpose: Active integration research for Kronos as a trading-decision input (not shadow-only)
branch: feat/trading-research-delivery-20261110
base_commit: 0943103
date: 2026-11-10
status: research-only; no runtime source changed in this pass
machine: Windows 10.0.26200 x64, CPU-only (torch 2.9.1+cpu), Python 3.10.20 isolated venv
---

# Kronos vào đường giao dịch thật — thiết kế khả thi, chi phí đo thật, so sánh thực nghiệm

Tài liệu này trả lời một câu hỏi duy nhất: **có thể đưa Kronos vào đường quyết định giao dịch
thật, một cách trung thực và đo được, ngay trên code hiện tại hay không — và nếu có thì cắt ở
đâu, cấu hình gì, test gì.**

Đây là báo cáo nghiên cứu. Không có source runtime nào bị sửa trong lượt này (yêu cầu của ask).
Mọi con số dưới đây đến từ **inference thật** với weights pinned thật trên **nến đóng thật** lấy
read-only từ sàn công khai, hoặc từ đọc source/đo arithmetics. Chỗ nào chưa đo được thì ghi rõ là
chưa đo, không suy diễn thành số.

---

## 0. TL;DR (đọc 2 phút)

1. **Không thể dùng `tradingagents/shadow/*` cho đường quyết định.** Có một test pin cứng
   (`tests/test_shadow_isolation.py:281-305`) cấm `tradingagents/graph`, `tradingagents/agents`
   và `server/autoloop.py` import `tradingagents.shadow` / `server.shadow`. Muốn Kronos thành đầu
   vào chiến lược thì **phải extract một core service độc lập chữ "shadow"** — đây là seam bắt buộc,
   không phải lựa chọn thẩm mỹ.
2. **Ba lỗi ordering/API phải sửa TRƯỚC khi code** (review vòng 2, đã kiểm lại source — §7.4, §5.2,
   §4.3): (a) prefilter **phải** chèn ở `_select_cycle_candidates:637-638` **trước** `[:limit]`, và
   điều kiện refill ở `:722,732` phải tính cả Kronos — nếu không, lọc sạch top-3 sẽ làm refill
   **không bao giờ chạy**; (b) `paths`/`temperature` **không** phải per-request — phải set qua
   `KronosSettings`, và `DEFAULT_PATHS = 8` đang **dưới** floor 16; (c) `execution_lock` **không**
   bị giữ suốt đường candidate (candidate chạy thread riêng), nên ràng buộc lock phải phát biểu lại.
3. **Chi phí thật (CPU-only, máy này), đo trên pool THẬT 60 symbols** (§2.6b/§2.6c): cold-load
   ~2.4-2.7 s mỗi process; sampler ~35 ms/bar/path. Cấu hình đề xuất (4h, 6 bar, 16 paths):
   - 10 symbols (160 rows): **22.5 s**
   - **60 symbols (960 rows): 192 s và peak RSS 6.07 GiB** — ngoại suy cũ (~135 s) **lạc quan ~42%**,
     và **chưa ai từng đo RAM**. Máy này chỉ còn **7.2 GiB available** lúc đo.
   - Chunk 5 symbols/lần: 210 s, **874 MiB** (rẻ hơn 7× RAM, chậm hơn 13%) → **chunking là bắt buộc**.
   Cách chạy hiện tại (spawn subprocess mỗi lần gọi) trả cold-load **mỗi symbol**.
4. **`0.7` cần tối thiểu 16 paths.** Với 3 paths, "3/3 = 1.00" có Wilson 95% CI [0.44, 1.00] —
   **không phân biệt được với tung đồng xu**. Floor 16 là điều kiện để con số có nghĩa.
5. **Phát hiện quyết định, đã đo lại ở đúng quy mô: cổng hướng KHÔNG ổn định, và `T=0.5` KHÔNG cứu
   được.** Cùng nến, chỉ đổi seed, **10 symbols × 10 seeds** (§3B.3b): `T=1.0` → **2/10** ổn định;
   `T=0.5` → **3/10**. Cả hai **fail** ngưỡng merge ≥8/10. (Số `T=0.5` = 2/3 trong bản trước là
   **mẫu nhỏ 3 symbols × 6 seeds**, không scale — đã tự đính chính.) Các phương án khác đều thất bại
   (§3B.3): thêm paths, dead band, K-bar consensus, magnitude gate, ranking.
   → **Cổng hướng cứng không dùng được → Kronos phải vào dạng `mode=context_only`.**
6. **Bug RNG toàn cục: forecast phụ thuộc BATCH, làm cache theo symbol không hợp lệ** (§2.6d). Cùng
   nến + cùng seed, đổi batch hoặc đổi thứ tự → **kết quả khác**. Nguyên nhân: `torch.multinomial`
   trên RNG toàn cục (`kronos.py:438,442`). **Sửa:** seed tường minh trước **mỗi** path — đã chứng
   minh là đủ. Không có bước này thì **không được cache** và không được so sánh giữa cycle.
7. **Horizon nên là 24h *trên khung 4h*, không phải 24 bar trên 1h.** Chi phí tuyến tính theo số bar
   (`kronos.py:420`), nên 24h trên 1h = 24 bar = 24× đắt hơn 1 bar; trên 4h chỉ 6 bar.
8. **So sánh thực nghiệm** (§6, lưu ý đọc kèm caveat): Kronos giữ 65/200 eligible, **30 long + 35
   short**; simple donchian 67/200 nhưng 29 long + 38 short với hit 0.299. No-filter cho LLM chạy
   200 lần. **Số này đo ở `T=1.0`, một seed** — đo độ phủ và chi phí, không đo chất lượng lọc.
9. **Chưa có bằng chứng lợi nhuận, và tôi không tuyên bố có.** 200 quan sát/20 điểm quyết định là
   quá nhỏ; hit rate là hướng, không phải PnL; **model card không công bố training cutoff**.
10. **Khuyến nghị cuối:** **Design B ở dạng `mode=context_only`** — compact typed forecast block cho
    analysts/PM, **không** cắt ứng viên, **không** hứa `saved_llm_calls`. `paths≥16`, horizon 24h trên
    4h, chunking bắt buộc, per-path seeding bắt buộc, defer-first khi lỗi, mặc định off. Không bao
    giờ là thứ nới risk/leverage hay bỏ qua PM.

---

## 1. Hiện trạng code (đã đọc, không suy đoán)

### 1.1 Adapter và runner

`tradingagents/shadow/kronos.py` — adapter chạy model **out-of-process**:

- Pin theo **nội dung**: hash SHA-256 của 3 file source + 2 file weights (`kronos.py:57-67`), verify
  lại trước mỗi lần chạy, memoize theo `(size, mtime)` (`kronos.py:113-144`).
- Gọi con bằng `[python, "-I", runner]` với payload JSON qua stdin (`kronos.py:288-299`).
- Timeout → `timeout`; thiếu interpreter/weights → `unavailable`; crash/non-JSON → `error`
  (`kronos.py:300-325`). Lỗi **là kết quả**, không phải exception — đây là tính chất tốt, giữ lại.
- Chỉ hỗ trợ `1h`/`4h` (`kronos.py:83`), horizon phải chia hết cho bar (`kronos.py:249-256`),
  `MAX_HORIZON_BARS = 72` (`kronos.py:81`), `MIN_LOOKBACK_BARS = 32`, default 96
  (`kronos.py:76-78`).

`tradingagents/shadow/kronos_runner.py` — tiến trình con:

- Loop `for index in range(paths)` gọi `predictor.predict(..., sample_count=1)`
  (`kronos_runner.py:73-83`). **Đây là điểm nghẽn số 1**: mỗi path là một lần forward đầy đủ,
  không vector hoá.
- `duration_ms` **chỉ đo vòng lặp path** (`kronos_runner.py:71,91`) — **không** gồm `import torch`
  và load weights. Hệ quả: `duration_ms` trong DB **đánh giá thấp chi phí thật** khoảng 30-40× ở
  cấu hình nhỏ (đo được: 63.6 ms sampler vs 2765 ms wall).

### 1.2 Vì sao runner loop thay vì `sample_count=N`

Đây là chi tiết quyết định và dễ hiểu sai. `auto_regressive_inference` **vector hoá `sample_count`
thành batch** (`kronos.py:394-396`) nhưng sau đó **lấy trung bình nội bộ**:

```
z = z.reshape(-1, sample_count, z.size(1), z.size(2))
preds = np.mean(preds, axis=1)        # kronos.py:465-467
```

Nghĩa là `sample_count=16` trả **một** đường trung bình, **không** trả 16 đường. Adapter cần 16
đường riêng để đếm tỷ lệ hướng (`summarise_paths`, `kronos.py:405-433`), nên loop với
`sample_count=1` là **đúng về mặt thống kê**.

**Nhưng** `predict_batch` (`kronos.py:562-661`) nhận **list các series** và trả **list kết quả**;
đặt `sample_count=1` và lặp cùng một symbol N lần trong `df_list` cho ta N đường **riêng biệt**,
vector hoá qua batch dimension. Đã kiểm chứng bằng đo thật: 16/16 paths distinct cho cả 10 symbols
(`data/SUMMARY.json` → `universe-4h-6bar-16paths.distinct_ends`, mỗi symbol `16`).

### 1.3 Contract dữ liệu (dùng lại được nguyên vẹn)

`tradingagents/shadow/provider.py` định nghĩa `ForecastRequest` (chỉ nến đóng, `as_of` là mốc quyết
định, `provider.py:68-92`), `PathSummary` giữ **toàn bộ** `closes/highs/lows` để trả lời "mức bảo vệ
nào bị chạm trước" (`provider.py:95-127`), và `CALIBRATION_SAMPLE_RATIO` nói rõ tỷ lệ mẫu **không
phải** xác suất calibrated (`provider.py:51-64`).

`tradingagents/shadow/recorder.py` là nơi đặt các luật trung thực: `validate_candles` từ chối nến
đang hình thành / lẫn khung / gap / nến đóng sau `as_of` (`recorder.py:81-116`);
`generation_kind` phân biệt forward vs retrospective bằng so sánh `prediction_created_at` với
`first_realized_close` (`recorder.py:217-222`). **Đây chính là logic mà cơ chế cache phải tôn trọng**
(xem §5.5).

### 1.4 Điểm chèn thật trong autoloop (đã kiểm lại current source)

Codebase-Memory xác nhận và source khớp: `_process_candidate` gọi `_closed_futures_context`
(`server/autoloop.py:1188`) → `build_graph` (1286) → `graph.propagate` (1302) →
`_process_futures_candidate` (sau, ở 1912).

Trình tự trong `_process_candidate` (1141-1302), futures mode:

| Bước | Dòng | Ghi chú cho thiết kế |
|---|---|---|
| guard mode + deadline | 1175-1177 | cancellation điểm 1 |
| `_closed_futures_context` | 1188 | **đã có nến 1h + 4h đóng** — chính là input Kronos cần |
| `read_futures_account` | 1217 | |
| `claim_futures_cycle` | 1236 | chống trùng cycle |
| `build_futures_portfolio` | 1251 | |
| brackets long+short | 1254-1261 | **đã có cả hai hướng** |
| `render_futures_decision_context` | 1262-1270 | chuỗi ngữ cảnh đưa vào graph |
| guard mode + deadline | 1284 | **phải kiểm lại sau inference** |
| `graph.propagate` | 1289-1302 | nơi LLM tiêu tiền |

Hai sự thật quan trọng:
- Nến đóng mà Kronos cần **đã có sẵn** tại 1188, trước khi LLM chạy → không cần đọc vendor lần hai.
- `execution_context` là **một string** được hash vào run signature
  (`tradingagents/graph/trading_graph.py:145-176`) và được truyền qua `propagate`
  (1300-1301, `_accepts_execution_context` ở 1799-1807). Đây là **seam ngữ cảnh tự nhiên**:
  forecast compact đi vào cùng đường string đó, không cần tham số graph mới.

### 1.5 Lựa chọn ứng viên và refill — chỗ phải cắt

- `AUTO_TOP_N = 3` (`autoloop.py:94`), `AUTO_FUTURES_SCREEN_POOL = 60` (102),
  `AUTO_FUTURES_MAX_UNIVERSE = 240` (108), `AUTO_FUTURES_REFILL_ROUNDS = 2` (107).
- `CYCLE_TIMEOUT_SECONDS = 3600.0` (113) — **ngân sách 1 giờ/cycle**, và 3 ứng viên × ~10 phút LLM
  là thứ đã làm cycle timeout (comment 90-93).
- `_select_cycle_candidates` (625-650) cắt `runnable[:limit]` **sau preflight**, và
  `_refill_futures_candidates` (663-811) mở rộng pool mỗi vòng (gấp đôi, cap 240).
- **Refill hiện có đã là cơ chế đúng** cho yêu cầu "lọc hết thì refill": nó tái screen pool rộng
  hơn. Thiết kế mới chỉ cần nói rõ prefilter **không được** làm refill chạy vô hạn.

`autoloop.py` là **2585 dòng** với `# ai-note: split declined again` ngay dòng 3 — tức repo đã biết
đây là file quá lớn và **đã từ chối split một lần**. Ask yêu cầu nêu split proposal trước khi chạm:
xem §7.

### 1.6 Dashboard/settings hiện có

- `server/shadow_api.py:43` `get_shadow(...)` — **GET-only**, test pin read-only
  (`tests/test_shadow_isolation.py:319`).
- `server/shadow_store.py` có `summary()` (284) tổng hợp ratio/evaluation/provider.
- Settings: `shadow_observations_enabled` default `False` (`default_config.py:394-396`), env keys
  ở 104-106. `_settings_payload` đánh dấu `editable` theo `_READONLY_ENV_KEYS` (`settings.py:126-134`).
- `server/operator_config.py` là nơi các switch Auto được ghi có confirmation + reason + audit
  (`operator_config.py:310-399`) — **mẫu đúng** cho config mới của Kronos.

### 1.7 Bug/limit thật trong code hiện có (ảnh hưởng nếu dùng active)

1. **`duration_ms` bỏ cold-load** — `started = time.perf_counter()` ở `kronos_runner.py:72`, tức
   **sau** `import torch` (`:43`) và sau khi load weights (`:48-49`); `duration_ms` đo ở `:91` chỉ
   phủ vòng lặp path. Hệ quả: **luôn** đánh giá thấp chi phí, mức bao nhiêu **phụ thuộc cấu hình** —
   đo của tôi ở 96 bar/1 path: 63.6 ms vs 2 765 ms (**~43×**); reviewer đo ở 40 bar/4h/24h:
   1 048.2 ms vs 3 610.4 ms (**3.44×**). Cả hai đúng vì cùng một nguyên nhân; **đừng** dùng một tỷ lệ
   cố định. **Phải sửa thành wall-clock của tiến trình** trước khi dashboard dùng con số này.
2. **`paths`/`temperature` không phải per-request** — `forecast()` đọc `self.settings.*`
   (`kronos.py:281,283`), bỏ qua `request.params`; `DEFAULT_PATHS = 8` (`:79`) nằm **dưới** floor 16.
   Một caller truyền `params={"paths":16}` **âm thầm nhận 8**. Đo thật: `data/paths-api.json`.
3. **Selection bias trong evaluation** — `server/shadow_worker.py:299-363` chỉ quan sát **cycle đã
   terminal**; `outcome_group_for` (`recorder.py:133-149`) tách `executed/rejected/abstained` đúng,
   nhưng tập "executed" **không phải mẫu của toàn universe**. Nếu đem hit-rate của tập này ra làm
   "độ chính xác của Kronos" thì đó là selection bias. Ghi rõ trong dashboard.
4. **`_evaluate_pending` tự nhận `net_r=None`** (`shadow_worker.py:400-404`) — không nối ledger.
   Đúng là trung thực, nhưng nghĩa là **chưa có PnL nào từ shadow**. Không được nói "shadow có lãi".
5. **Không có cache** — `forecast_cache`/`cache_key` không tồn tại trong `tradingagents/shadow/*`
   hay `server/shadow_store.py`/`shadow_worker.py` (findstr → rỗng). §5.5 là thiết kế phải build.
6. **Chỉ 1h/4h** (`kronos.py:83`) trong khi cache local 173 file là **daily**
   (`~/.tradingagents/cache/*-YFin-data.csv`, ví dụ BTC: 1827 dòng 2021-10-28→2026-10-28). Không thể
   dùng trực tiếp để backtest cấu hình đề xuất.
7. **`_venue_candle_source` mở executor mỗi lần** (`shadow_worker.py:209-227`) — chấp nhận được cho
   worker nền, **không** chấp nhận được trên đường quyết định (xem §5.3).

---

## 2. Chi phí thật — đo, không ước lượng

### 2.1 Điều kiện đo

- Weights pinned: `Kronos-small` + `Kronos-Tokenizer-base`, hash khớp `PINNED_WEIGHT_HASHES`
  (`kronos.py:62-67`); interpreter isolated `kronos-venv` Python 3.10.20, torch 2.9.1+cpu,
  `cuda=False`. **Không cài lại gì.**
- Dữ liệu: **nến đóng thật**, read-only public endpoint, không key, không lệnh:
  `binanceusdm` 1h × 600 bar và 4h × 1000 bar cho BTC/ETH/SOL; 4h × 400 bar cho 10 symbols
  (`data/ohlcv-*.json`). Bar cuối bị loại vì có thể còn đang hình thành.
- Chạy đúng runner của repo qua `subprocess` với `-I`, y như `KronosProvider` làm.

### 2.2 Cold-load vs sampler (36/36 run OK)

Đơn vị ms. `wall` = cả tiến trình; `sampler` = `duration_ms` của runner; `load = wall - sampler`.

| Config | wall median | sampler median | load median |
|---|---|---|---|
| 96 bar, 1 path, 1h, pred_len=1 | **2765** | 63.6 | 2715 |
| 96 bar, 1 path, 1h, pred_len=24 | **3248** | 858.3 | 2396 |
| 96 bar, 8 paths, 1h, pred_len=24 | **14477** | 11987 | 2488 |
| 96 bar, 32 paths, 1h, pred_len=24 | **31427** | 28911 | 2361 |

Nguồn: `data/bench-inference.json`, 9 run mỗi config (3 symbol × 3 repeat), tất cả `ok`.

**Kết luận chi phí:** `import torch` + load weights ≈ **2.4-2.7 s** và **không phụ thuộc** kích
thước công việc. Với một process spawn-per-call, mỗi lần gọi trả 2.4 s bất kể làm gì — với 160 rows
sẽ là ~160 × 2.4 s ≈ 6.4 phút chỉ để load. Đó là lý do **persistent process là bắt buộc**, không
phải tối ưu hoá.

### 2.3 Ba cách chạy, cùng 3 symbols × 8 paths, pred_len=24 (1h)

| Shape | Tổng median | Ghi chú |
|---|---|---|
| spawn mỗi symbol (hiện tại) | **30 710 ms** | 3 subprocess, mỗi cái trả cold-load |
| 1 process, `predict()` tuần tự | **23 916 ms** | `load_ms` 255 (warm), sampler chiếm phần lớn |
| 1 process, `predict_batch()` | **15 252 ms** | 24 rows một lần gọi, `distinct_paths = 8/8` |

Nguồn: `data/shape-spawn.json`, `shape-sequential.json`, `shape-batched.json`.

### 2.4 Scale theo horizon (chi phí ~ tuyến tính theo **số bar**)

1 path, 3 symbols, median:

| pred_len (bar) | median ms | ms/bar |
|---|---|---|
| 1 | 60 | 60 |
| 6 | 229 | 38 |
| 12 | 412 | 34 |
| 24 | 846 | 35 |

Nguồn: `data/bench-scaling.json`. Vì `kronos.py:420` lặp `for i in range(pred_len)`, horizon 24h
trên 1h (24 bar) đắt gấp ~4× so với 24h trên 4h (6 bar). **Đây là lập luận chọn 4h.**

### 2.5 Scale theo sample, và lợi ích batch

`sample_count=1`, paths = số draw, 3 symbols:

| paths | rows | tuần tự | batched | speedup | distinct |
|---|---|---|---|---|---|
| 8 | 24 | 5 819 | 3 619 | 1.61× | 8 |
| 16 | 48 | 10 487 | 7 408 | 1.42× | 16 |
| 32 | 96 | 22 582 | 13 133 | 1.72× | 32 |

Nguồn: `data/bench-scaling.json` → `sample_scaling`. Speedup khiêm tốn vì batch chỉ vector hoá
decode, nhưng **distinct = N** xác nhận `predict_batch` **không** trung bình nội bộ (khác
`sample_count=N`).

### 2.6 Cấu hình đề xuất, đo trên universe thật

4h, pred_len = 6 bar (= 24h), 16 paths/symbol:

| Universe | rows | batch call | wall tổng | distinct/symbol |
|---|---|---|---|---|
| 3 symbols | 48 | 6 867 ms | **6 719 ms** | 16/16 |
| 10 symbols | 160 | 22 481 ms | **22 492 ms** | 16/16 |

Nguồn: `data/shape-4h-6bar-16paths.json`, `data/universe-4h-6bar-16paths.json`.

**↔ Ngân sách:** `CYCLE_TIMEOUT_SECONDS = 3600` (`autoloop.py:113`). 22.5 s cho 10 symbols là
**0.6%** ngân sách cycle. Nhưng nếu prefilter chạy trên **pool 60** (AUTO_FUTURES_SCREEN_POOL),
ngoại suy tuyến tính ≈ 135 s ≈ **3.8%** — và đây là **ngoại suy**, chưa đo trực tiếp ở 60 symbols
(xem §8, blocker BL-3).

### 2.6b BL-3 — đo THẬT pool 60 symbols, và phát hiện RAM

Bản trước chỉ có ngoại suy (~135 s). Nay đã lấy **60 symbol thật** (linear USDT perp đang active,
200 bar 4h mỗi symbol, read-only) và chạy đúng cấu hình đề xuất (`4h / 6 bar / 16 paths / T=0.5`,
một persistent process, một `predict_batch`):

| | đo được | ghi chú |
|---|---|---|
| Wall (median 2 run) | **191 603 ms (≈192 s)** | **nhiều hơn ngoại suy 135 s ~42%** |
| Inference thuần | 191 576 ms | gần như toàn bộ wall |
| Tỷ lệ ngân sách cycle | **5.3 %** | của 3600 s |
| Load model (warm process) | 575 ms | |
| RSS sau load | 340 MiB | model + tokenizer + torch |
| **Peak RSS** | **6 211 MiB (6.07 GiB)** | **đây là con số chưa ai có** |
| Eligible @0.70 | 35-40 / 60 | ở `T=0.5` |
| Rows | 960 | 60 × 16 |

**Ngoại suy đã sai theo hướng lạc quan** — reviewer nghi đúng. Nhưng phát hiện quan trọng hơn là
**RAM**: 6.07 GiB cho một lần gọi batch. Máy này có **31.7 GiB total, và tại thời điểm đo chỉ còn
**7.2 GiB available** (load 77%). Một persistent process 6.07 GiB trong server là **không chấp nhận
được** — nó gần như bằng toàn bộ RAM còn trống.

### 2.6c Chunking: đổi RAM lấy thời gian, nhưng làm **sai** kết quả

Quét chunk theo số symbol (`data/pool60-chunks.json`, cùng seed):

| chunk (symbols) | rows/call | số call | tổng | **peak RSS** | eligible | verdict khớp chunk=5? |
|---|---|---|---|---|---|---|
| 5 | 80 | 12 | 209.9 s | **874 MiB** | 40 | (chuẩn) |
| 10 | 160 | 6 | 194.8 s | 1 395 MiB | 38 | **không** |
| 20 | 320 | 3 | 186.5 s | 2 882 MiB | 39 | **không** |
| 60 | 960 | 1 | 185.5 s | 6 219 MiB | 37 | **không** |

Chunking rẻ hơn về RAM gần **7×** (874 MiB vs 6 219 MiB) và chỉ chậm hơn **13%**. Nhưng
**`verdicts_identical_across_chunks: false`** — cùng seed, cùng nến, chỉ đổi cách chia batch mà
verdict đổi. Đó không phải nhiễu được phép: nó nghĩa là **forecast của một symbol phụ thuộc vào
những symbol cùng batch với nó**.

### 2.6d Nguyên nhân: sampling dùng RNG **toàn cục**, không theo hàng

Kiểm trực tiếp (`data/batch-dependence.json`, target `0G/USDT:USDT`, 4 paths):

| Kiểm tra | Kết quả |
|---|---|
| Cùng symbol, chạy một mình vs batch với 3 symbol khác | **kết quả KHÁC** |
| Cùng symbol, đổi thứ tự trong batch | **kết quả KHÁC** |
| Seed **theo từng path** trước mỗi lần draw | **kết quả GIỐNG NHAU** bất kể batch |

Nguyên nhân nằm ở model gốc: `sample_from_logits` gọi `torch.multinomial` trên RNG toàn cục
(`kronos.py:438,442`), nên **hàng thứ k tiêu thụ đoạn RNG phụ thuộc số hàng đứng trước nó**. Đây
đúng là giả định mà `stable_seed(cycle_id, provider_id)` (`provider.py:172-182`) dựa vào — nó chỉ
seed **một lần cho cả request**, không theo path.

**Hệ quả cứng cho thiết kế:**
1. **Cache key theo symbol là KHÔNG hợp lệ** trừ khi draw được seed theo từng path. Cùng nến + cùng
   seed nhưng batch khác → forecast khác → cache sẽ trả một con số không tái lập được.
2. **Bắt buộc:** seed tường minh trước **mỗi** path (đã chứng minh là đủ — hàng 3 bảng trên), nếu
   không thì không được cache, không được so sánh giữa các cycle, và `T=0.5` stability đo được ở
   §3B có thể bị nhiễu bởi chính hiệu ứng này.
3. **Chunking là bắt buộc** vì RAM (6.07 GiB → 874 MiB), nhưng **chỉ được dùng sau khi** có per-path
   seeding.

### 2.7 Cái gì **chưa** đo

- **GPU.** Máy này `cuda=False`; mọi số trên là CPU. Không được dùng để tuyên bố về GPU.
- **Swap/contention** khi chạy song song với LLM stage.
- **RAM ở cấu hình per-path seeding** (seed lại từng path có thể đổi profile bộ nhớ/time) — chưa đo.
- **Pool 240** (`AUTO_FUTURES_MAX_UNIVERSE`), tức refill round 2 — chưa đo; 60 đã là 192 s.

---

## 3. `sample_count` — vì sao 3 paths không thể là bằng chứng cho 0.7

Arithmetics thuần (không phải experiment), Wilson score 95%, `data/ratio-power.json`:

| n paths | k đạt 0.70 | ratio | Wilson 95% | phân biệt được với 0.5? |
|---|---|---|---|---|
| 2 | 2 | 1.000 | [0.342, 1.000] | **không** |
| 3 | 3 | 1.000 | [0.438, 1.000] | **không** |
| 8 | 6 | 0.750 | [0.409, 0.929] | **không** |
| 10 | 7 | 0.700 | [0.397, 0.892] | **không** |
| **16** | 12 | 0.750 | [0.505, 0.898] | **có** (min) |
| 24 | 17 | 0.708 | [0.508, 0.851] | có |
| 32 | 23 | 0.719 | [0.546, 0.844] | có |
| 64 | 45 | 0.703 | [0.582, 0.801] | có |

**Đọc đúng:** với n=3, "cả 3 paths đều lên" **không** nói được gì hơn tung đồng xu —
CI chạm 0.438. Đây chính là điều ask cảnh báo, và đây là cơ sở số học để đặt `min_paths = 16`.

**Caveat bắt buộc phải giữ:** đây là 16 draw **tuần tự từ một model**, có temperature/top_p, không
phải 16 mẫu độc lập từ phân phối đã calibrate. Sai số thật **rộng hơn** binomial. Vì vậy:
- Interval trên là **cận dưới lạc quan** của nhiễu (hướng sai an toàn khi chọn floor).
- Trong dashboard **không** được gọi đây là "xác suất thắng". Nhãn đúng đã có sẵn:
  `CALIBRATION_SAMPLE_RATIO` = `"model_sample_ratio_not_probability"` (`provider.py:51`).

**Ngưỡng granularity:** n=16 → bước nhảy 0.0625, nên ngưỡng 0.70 thực tế là "≥12/16 = 0.750".
Đặt ngưỡng 0.70 với n=16 là **tự lừa** về độ chính xác. Ghi rõ trong config là "≥ ceil(ratio×paths)".

---

## 3B. Ổn định của cổng — phát hiện quyết định (đo thật)

§3 trả lời "cần bao nhiêu paths để con số có nghĩa". Nó **không** trả lời câu hỏi quan trọng hơn:
**cùng một cây nến, chạy lại với seed khác, verdict có giữ nguyên không?** Nếu không, cổng đang đo
seed chứ không đo thị trường, và một cycle duy nhất là một lần rút thăm trá hình.

Thí nghiệm: **giữ nguyên nến**, chỉ đổi `seed`, cùng model, cùng params. Đếm số symbol giữ **một**
verdict duy nhất qua mọi seed.

### 3B.1 Ở default `T=1.0`, cổng 0.70 không ổn định

4h, pred_len=6, **16 paths**, 10 seeds, 10 symbols (`data/gate-stability.json`):

| Symbol | verdicts qua 10 seeds | ratio_up min-max |
|---|---|---|
| BTC | {flat: 6, up: 4} | 0.438 - 0.875 |
| ETH | {flat: 2, up: 8} | 0.562 - 0.938 |
| SOL | {flat: 2, up: 8} | 0.500 - 0.938 |
| BNB | {down: 2, flat: 8} | 0.125 - 0.438 |
| XRP | {flat: 4, up: 6} | 0.625 - 0.938 |
| DOGE | {flat: 6, up: 4} | 0.500 - 0.938 |
| ADA | {down: 2, flat: 8} | 0.188 - 0.688 |
| AVAX | {flat: 10} | 0.312 - 0.500 |
| LINK | {flat: 10} | 0.375 - 0.688 |
| LTC | {flat: 7, up: 3} | 0.375 - 0.875 |

**2/10 ổn định; 6/10 có ratio vắt qua ngưỡng.** Cùng nến, cùng model — chỉ khác seed.

### 3B.2 Sweep threshold: chỉ ổn định khi gần như không bật

Cùng dữ liệu trên, quét ngưỡng (`data/threshold-sweep.json`). Cột `fire_rate` = tỷ lệ lần cổng thực
sự bật (up/down) trên mọi (symbol × seed):

| threshold | need (n=16) | ổn định | fire_rate |
|---|---|---|---|
| 0.65 | ≥10 | 1/10 | 0.66 |
| 0.70 | ≥11 | 0/10 | 0.47 |
| 0.75 | ≥12 | 2/10 | 0.37 |
| 0.80 | ≥13 | 2/10 | 0.29 |
| 0.85 | ≥14 | 3/10 | 0.15 |
| 0.95 | ≥15 | 6/10 | 0.04 |
| 1.00 | ≥16 | 10/10 | **0.00** |

**Không có dòng nào vừa ổn định vừa bật thường xuyên.** "Ổn định" ở 0.95-1.00 chỉ là "không bao giờ
chạy" — một cổng như thế không lọc gì, chỉ chặn tất cả.

### 3B.3 Năm phương án sửa — bốn thất bại, một thành công

| Phương án | Kết quả | Nguồn |
|---|---|---|
| Tăng paths 16→32 | ổn định 3→4/10, spread 0.25→0.156. Không đủ | `grid-stability.json` |
| Tăng paths 16→64 | spread 0.25→0.062 (nhiễu là binomial, **giảm được**), nhưng ổn định vẫn chỉ 1/3 | `pathcount-stability.json` |
| Dead band ±0.03…±0.10 | **0 thay đổi** (2/10) — flip là giữa *pass* và *unclear*, không phải giữa up/down | `deadband.json` |
| Magnitude gate (median ≥ 1-5× cost 0.1%) | stable 2-3/10 ở mọi bội số | `cost-gate.json` |
| K-bar consensus (3 nến) | 3/3 "ổn định" nhưng **fire_rate = 0** — lại là cổng không bật | `consensus-gate.json` |
| **Hạ temperature** | **`T=0.5` → 2/3 ổn định, fire_rate 0.889** | `temperature-stability.json` |

Bảng nhiệt độ (4h, 6 bar, 16 paths, 6 seeds, BTC/ETH/SOL):

| T | ổn định | fire_rate | ratio spread | band (full range) |
|---|---|---|---|---|
| 1.0 | 0/3 | 0.333 | 0.312 | 0.0253 |
| 0.7 | 0/3 | 0.389 | 0.312 | 0.0182 |
| **0.5** | **2/3** | **0.889** | 0.188 | 0.0151 |
| 0.3 | 3/3 | 1.000 | 0.000 | **0.0043** |

**Đọc đúng:** `T=0.3` ổn định hoàn toàn nhưng `ratio_spread = 0.000` và band co còn 0.0043 — model đã
**sụp về trung bình của chính nó**. Đó không phải lọc, đó là một điểm ước lượng đội lốt tỷ lệ.
`T=0.5` là điểm cân bằng: ổn định tốt hơn nhiều (0/3 → 2/3), vẫn bật thường xuyên (0.889), và vẫn
giữ phân phối (band 0.0151). **Đây là cấu hình để đề xuất, không phải `T=1.0`.**

**Giới hạn phải nói:** 3 symbols × 6 seeds cho bảng nhiệt độ là **nhỏ**. Nó đủ để bác `T=1.0` và đủ
để đề xuất `T=0.5` như **giả thuyết cần xác nhận trên universe rộng hơn**, không đủ để tuyên bố
`T=0.5` chắc chắn ổn định ở mọi nơi. Đây là lý do acceptance test §9 phải đo lại stability như một
điều kiện merge, không phải một lần đo rồi thôi.

### 3B.3b BL-7 — đo lại ở đúng quy mô đã hứa: `T=0.5` **KHÔNG** đạt

§3B.3 kết luận `T=0.5` là lời giải, nhưng chỉ từ **3 symbols × 6 seeds** và brief đã tự ghi đó là
mẫu nhỏ (BL-7). Nay đã đo đúng quy mô merge condition: **10 symbols × 10 seeds**, cùng nến, chỉ đổi
seed (`data/bl7-stability-wide.json`):

| Config | ổn định | fire_rate | đạt merge (≥8/10 và fire ≥0.30)? |
|---|---|---|---|
| `T=1.0` | **2/10** | 0.38 | **KHÔNG** |
| `T=0.5` | **3/10** | 0.55 | **KHÔNG** |

Chi tiết `T=0.5`: chỉ BTC (`up`, ratio 0.812-1.0), ADA (`down`, 0.0-0.125) và LINK (`down`,
0.0-0.25) giữ một verdict; 7 symbol còn lại vẫn flip (ETH 0.25-**0.562**, BNB 0.062-**0.5**,
XRP 0.5-0.75, DOGE 0.312-0.75, SOL 0.688-1.0, AVAX 0.312-0.75, LTC 0.5-0.75).

**Đính chính thẳng vào kết luận §3B.3:** "`T=0.5` → 2/3 ổn định" **không scale**. 3/10 ở mẫu lớn hơn
gần đúng với tỷ lệ 2/10 của `T=1.0`; chênh lệch 2/3 vs 3/10 là **nhiễu mẫu nhỏ**, không phải cải
thiện thật. Nói cách khác: **hạ temperature không cứu được cổng hướng.**

Ghi chú thêm: ba symbol "ổn định" của `T=0.5` ổn định theo hai kiểu khác nhau — BTC ổn định vì
**luôn vượt** ngưỡng (0.812-1.0), còn ADA/LINK ổn định vì **luôn dưới** (0.0-0.25). Chỉ 1/10 symbol
(BTC) ổn định **và** thường xuyên bật theo hướng dương. Đó không phải một bộ lọc dùng được.

### 3B.4 Hệ quả cho kiến trúc (đã sửa theo BL-7)

1. **`paths ≥ 16` vẫn là điều kiện cần** (§3) — nhưng **không đủ**, và **`T=0.5` cũng không đủ**
   (§3B.3b). Kết hợp `paths=16 + T=0.5` vẫn chỉ 3/10 ổn định.
2. **Cổng hướng cứng KHÔNG được dùng** ở cấu hình nào đã đo. Kronos phải vào như
   **`mode=context_only`**: cung cấp band/median/tỷ lệ cho analysts/PM, **không** chặn ứng viên nào.
3. **Hệ quả trực tiếp lên Design B:** phần "prefilter" của Design B — thứ tạo ra `saved_llm_calls` —
   **không đứng vững** trên bằng chứng hiện có. Cái còn đứng vững là phần **context**. Xem §4.1/§4.2.
4. **Stability phải là số hạng hạng nhất trên dashboard**, không phải ghi chú. Một verdict không có
   `ratio_spread` đi kèm là chưa đủ để người vận hành tin.
5. **Bất biến cần giữ:** nếu ai đó sau này muốn thử lại cổng cứng, điều kiện tiên quyết là **đo lại
   stability ở ≥10 symbols × ≥10 seeds** với đúng cấu hình đề xuất, và phải đạt ≥8/10 + fire ≥0.30.
   Test đó đã có (`bench_stability_bl7.py`) và hiện **fail**.

---

## 4. Kiến trúc đề xuất

### 4.1 Design B được chọn (kèm lý do loại A và C)

```
universe liquidity/contract preflight   (~_preflight_candidates_with_metadata, autoloop.py:534)
        │
        ▼
Kronos closed-candle forecast   (4h, horizon 24h = 6 bar, paths ≥16, T=0.5, batch, persistent)
        │                       ⚠ T=1.0 → cổng không ổn định, chỉ dùng context_only (§3B)
        │
        ▼
verdict: long-eligible | short-eligible | unclear      ← HAI HƯỚNG, chỉ để BÁO CÁO (§3B.3b)
        │                                                 KHÔNG dùng để cắt ứng viên
        ▼
compact typed forecast block  →  execution_context (string, trading_graph.py:145-176)
        │                       →  LLM analysts → debate → Trader → PM
        ▼
existing confirmation / RiskGuard / bracket / authorization / venue
        │
        ▼
order (không đổi gì)
```

**Design A — "Kronos thay PM/sizing" (loại).** Model tự quyết size/leverage. Loại vì: vi phạm
`MAX_AUTO_LEVERAGE` (`autoloop.py:131`) và toàn bộ chuỗi RiskGuard; `Kronos-small` là model dự báo
chuỗi giá, không có khái niệm margin/heat/authorization. Không có đường nào để nó "biết" cap.

**Design C — "shadow roadmap mở rộng" (loại).** Giữ observation-only rồi hy vọng đủ dữ liệu.
Loại vì **ask đã bác shadow-only**, và quan trọng hơn: shadow hiện **không nối ledger**
(`shadow_worker.py:400-404`, `net_r=None`) nên không bao giờ trả lời được câu hỏi PnL; đồng thời
test pin (`test_shadow_isolation.py:281`) chặn vĩnh viễn việc dùng nó trên đường quyết định.

**Design B — prefilter + context (CHỌN, nhưng đã hạ cấp — xem dưới).** Kronos **không** phán quyết
cuối; nó (a) cắt bớt ứng viên trước khi tiêu LLM, và (b) đưa ngữ cảnh định lượng có cấu trúc cho
analysts/PM. Hai hướng long/short đối xứng. Không nới risk, không bypass PM.

> **ĐÍNH CHÍNH BẮT BUỘC (§3B.3b, BL-7): phần "filter" của Design B KHÔNG đứng vững.**
> Ở đúng quy mô merge (10 symbols × 10 seeds), cổng hướng chỉ ổn định **2/10** ở `T=1.0` và **3/10**
> ở `T=0.5` — cả hai **fail** ngưỡng ≥8/10. Nghĩa là một bộ lọc cứng sẽ loại/nhận ứng viên theo
> seed, không theo thị trường. Vì vậy **Design B được chọn ở dạng `mode=context_only`**:
> - **Giữ:** compact typed forecast block (§5.7) đưa band/median/tỷ lệ cho analysts/PM.
> - **Bỏ:** cắt ứng viên trước LLM. Không có `saved_llm_calls` nào được hứa.
> - **Giữ nguyên** toàn bộ §5.2–§5.5 (cancellation, isolated process, failure policy, cache) vì
>   chúng áp dụng cho cả context_only.
> Hệ quả chi phí: không còn tiết kiệm LLM; toàn bộ chi phí Kronos là **thêm vào**. Ở pool 60 đó là
> 192 s + 0.87 GiB (chunked) cho **mỗi** cycle — xem §2.6b/§2.6c trước khi bật.

### 4.2 Vì sao **cả** filter **và** context (không chọn một)

> **Mục này giữ lại để ghi lý do đã cân nhắc, nhưng kết luận đã đổi:** bằng chứng §3B.3b loại bỏ
> vế "filter", nên chỉ còn "context". Đọc phần dưới như **lịch sử lập luận**, không phải khuyến nghị.

- **Filter-only** có hại khi `unclear` — mất luôn ngữ cảnh mà vẫn tốn inference; tệ hơn, nếu ngưỡng
  sai thì nó chặn cơ hội mà không ai thấy lý do.
- **Context-only** không tiết kiệm gì: vẫn chạy LLM cho mọi ứng viên (200 lần thay vì 65 lần trong
  đo của tôi).
- **Kết hợp** cho phép đo **hai** hiệu ứng tách bạch: `saved_llm_calls` (từ filter) và
  `context_influence` (từ block ngữ cảnh). Đây cũng là cách duy nhất để **tắt filter mà giữ context**
  nếu filter chứng minh có hại — một policy switch, không phải revert code.

### 4.3 Ngưỡng / horizon / sample / volatility / cost / regime — kèm rationale

Đề xuất **structured config** (schema validate, mặc định `enabled=false`):

| Tham số | Giá trị đề xuất | Rationale (đo được) |
|---|---|---|
| `timeframe` | `"4h"` | 24h = 6 bar thay vì 24 bar → ~4× rẻ (§2.4). 4h cũng là khung mà `confirmation` và bracket của runtime đang dùng (`autoloop.py:1254-1261`) |
| `horizon_hours` | `24` | Khớp nhịp cycle/confirmation hiện tại; 6 bar |
| `paths` | `16` (min 16, cap 32) | n=16 là floor để 0.70 phân biệt được với 0.5 (§3); 32 nếu muốn CI hẹp hơn, chi phí ×2. **Phải set qua `KronosSettings(paths=...)`, không qua request** — xem cảnh báo dưới |
| **`temperature`** | **`0.5`** | **Đây là tham số quyết định.** Ở `T=1.0` (default hiện tại, `kronos.py:182`), cổng 0.70 chỉ ổn định 2/10 symbols qua các seed (§3B.1). `T=0.5` → 2/3 ổn định với fire rate 0.889 (§3B.3). **Không** hạ tới `T=0.3`: band co còn 0.0043, mất phân phối. Cũng set qua `KronosSettings` |
| `direction_ratio` | `0.70` | Nghĩa thực tế với n=16 là **≥12/16**; dưới 0.70 coi là `unclear` |
| `min_paths` | `16` | Dưới ngưỡng này **không được** ra verdict long/short, chỉ được ra `unclear` |
| `stability_check` | bật | Đo lại verdict với ≥2 seed **trước khi** firm; nếu verdict đổi → hạ xuống `unclear` (xem §3B.4). Chi phí: ×2 inference cho các ứng viên sắp pass |
| `band_width_ratio` | báo cáo, chưa chặn | `p10/p90` của `end_close` (`kronos.py:420-428`); chặn theo band khi chưa có dữ liệu là đoán |
| `max_band_spread_ratio` | `0.15` (đề xuất, **chưa hiệu chỉnh**) | Nếu `(p90-p10)/reference` quá rộng, dự báo vô dụng cho sizing → `unclear`. **Chưa có dữ liệu để chọn số này**, ghi rõ |
| `atr_volatility_gate` | bật, dùng `atr_targets_signed` sẵn có | `tradingagents/dataflows/screening.py:591`; bỏ qua ứng viên có ATR/giá quá nhỏ (không đủ biên cho phí) |
| `cost_gate` | bắt buộc | Nếu `expected_move_p50 < round_trip_cost × 1.5` → `unclear`. Không có gate này thì mọi tín hiệu nhỏ đều bị đẩy qua LLM rồi bị RiskGuard chặn ở cuối — tốn hai lần. `CostModel.taker_rate = 0.0005`/leg (`tradingagents/simulation/costs.py:29-31`) → round trip ~0.1% |
| `regime` | **không** thêm regime classifier | Không có dữ liệu để hiệu chỉnh; thêm vào là thêm một tham số không kiểm chứng được. Thay bằng `max_band_spread_ratio` phản ánh bất định trực tiếp từ model |
| `on_missing/timeout/stale` | `defer` (mặc định) | Xem §5.4 |
| `failure_policy` | `defer` \| `pass_through` | `defer` = bỏ ứng viên này, thử ứng viên kế; `pass_through` = cho qua không có ngữ cảnh Kronos. **Mặc định `defer`** vì "không có gate" ≠ "gate passed" |
| `scope` | `futures_crypto_only` | Stock/manual report **không** bị chặn (§4.4) |

> **CẢNH BÁO API (review vòng 2, đã đo thật): `paths` và `temperature` KHÔNG phải per-request.**
> `KronosProvider.forecast()` build payload bằng `self.settings.paths` (`kronos.py:281`) và
> `self.settings.temperature` (`:283`) — nó **không đọc `request.params`**. Đo trên adapter thật
> (`data/paths-api.json`):
>
> | Cách truyền | `len(attempt.paths)` | wall |
> |---|---|---|
> | `params={"paths": 16}`, settings default | **8** | 4 744 ms |
> | `KronosSettings(paths=16)` | **16** | 8 218 ms |
>
> Nghĩa là một caller truyền `params={"paths":16}` **âm thầm nhận 8** — dưới floor §3 mà không có
> lỗi nào. Và `DEFAULT_PATHS = 8` (`kronos.py:79`) **đang thấp hơn floor 16**. Do đó config mới
> **bắt buộc**: (a) set `paths`/`temperature` qua `KronosSettings`; (b) validate `paths >= min_paths`
> **lúc dựng provider**, không lúc gọi; (c) test pin rằng truyền `params={"paths":16}` một mình
> **không** đổi số path trả về (để hành vi ngầm này không bị lặng lẽ đổi nghĩa).

**Không** đặt ngưỡng theo "0.7 nghĩa là 70% thắng". 0.70 là **tỷ lệ đường mẫu cùng hướng**, và nhãn
`model_sample_ratio_not_probability` phải đi cùng mọi chỗ hiển thị.

### 4.4 Phạm vi áp dụng — không chặn nhầm stock/manual

- **Futures crypto (Auto Futures):** áp dụng đầy đủ. Đây là nơi có `_closed_futures_context` +
  hai bracket long/short.
- **Stock:** `_process_candidate` đã nói rõ stock là **advisory** (`autoloop.py:1150-1159`) và
  `_preflight_candidates_with_metadata` loại stock khỏi futures (541-576). Kronos **không** được
  chặn stock, và adapter cũng không đọc được daily bar (chỉ 1h/4h).
- **Spot auto & manual report** (`server/daily.py:282-327`) chạy `propagate_with_telemetry` **không**
  có `execution_context`. Thiết kế đặt prefilter ở **autoloop futures path**, nên spot/manual
  **giữ nguyên hành vi** — đúng yêu cầu "off-mode giữ incumbent".

Nói cách khác: prefilter nằm ở **ranh giới chọn ứng viên của futures mode**, không nằm trong graph.
Nhờ vậy spot/manual không bao giờ bị ảnh hưởng, và graph không phải import gì mới.

---

## 5. Chính sách vận hành (phần dễ làm hỏng hệ thống)

### 5.1 Enabled / off-mode

- `enabled=false` (mặc định): **không đọc, không gọi, không đổi gì**. Đúng mẫu
  `observations_enabled()` hiện có (`shadow_worker.py:90-91`).
- Config đi qua `operator_config.py` (confirmation + reason + audit, 310-399) **không** qua `.env`
  (`AGENTS.md`: runtime settings persist in PostgreSQL; `.env` là deployment-only). Research này
  **không** ghi config runtime, không bật gì.

### 5.2 Cancellation & lock — viết lại theo code thật (review vòng 2)

Yêu cầu: "Dừng mode/cancellation kiểm lại trước/sau inference, inference không giữ execution/symbol
lock hay ngăn protection."

**Đính chính:** bản trước của mục này nói `execution_lock` "đang giữ cả đường candidate" và vì thế
inference có thể chặn `bracket_monitor`. **Điều đó sai.** Đọc lại source:

- Lock chỉ được acquire ở **đúng 2 chỗ**: `autoloop.py:205` (bọc chính lời gọi `execute()` bên trong
  `_execute_with_deadline`) và `:862` (barrier no-op sau khi timeout, để đợi lời gọi đang chạy).
- `_run_candidate_with_timeout` chạy candidate trong **thread riêng** (`:851`), và docstring của nó
  nói thẳng mục đích: *"Run one candidate off-cycle so an LLM stall cannot hold the lock"* (`:837`).
- `execution_lock` chỉ được **truyền xuống** để dùng trong lambda `execute_guard` (`:2239-2241`),
  không bao quanh phần analysis.
- `bracket_monitor.py`, `position_monitor.py`, `position_review.py`: **0 tham chiếu
  `execution_lock`** (kiểm bằng `findstr /s /c:"execution_lock"` trên ba file → rỗng).

Vậy inference 22 s **không** chặn được protection. Ràng buộc "đặt prefilter ngoài vùng lock" là
không cần thiết — và nếu vẫn ép theo nó thì lại đẩy prefilter ra **sau** `[:limit]`, tái tạo đúng
BLOCKER của §7.4.

**Điều gì thật sự cần giữ** (đây là các checkpoint có thật trong code, không phải suy diễn):

1. **Mode-revoke checkpoint.** Có ở `:1284`, `:1287`, `:1296` (trong `check_cancelled` của graph),
   và `:1306`. Theo yêu cầu "kiểm lại trước/sau inference": gọi `_raise_if_mode_disabled` **trước**
   khi inference và **ngay sau** khi nó trả về, trước khi dùng kết quả. Giữ nguyên.
2. **Cycle deadline.** `_cycle_timeout` (`:159`) trước/sau inference — một inference 22-135 s phải
   tính vào ngân sách, và nếu vượt thì bỏ phần còn lại chứ không cắt ngang một batch đang dở.
3. **Không truyền `execution_lock` vào tầng inference.** Không phải vì nó đang bị giữ, mà vì tầng
   này không có việc gì với lock; truyền vào chỉ mở đường cho một acquire sai chỗ sau này.
4. **Protection không đi qua prefilter.** Đúng, nhưng vì lý do kiến trúc (prefilter là bước chọn
   ứng viên, protection là bước bảo vệ vị thế), **không** phải vì tranh lock.

**Hệ quả cho vị trí:** prefilter đặt ở `_select_cycle_candidates:637-638` (§7.4) là **an toàn** —
nó chạy trước khi candidate thread được spawn ở `:851`, nên không tương tác với lock ở bất kỳ đường nào.

### 5.3 Isolated process, không phải subprocess-per-call

- Persistent process giữ model **warm** (load 255-290 ms thay vì 2.4-2.7 s mỗi lần, §2.3).
- Bắt buộc `-I` + interpreter **ngoài repo** như hiện tại (`kronos.py:94-111`) — giữ nguyên tính
  chất "runtime venv không có torch".
- Giao thức: JSON-lines qua stdin/stdout, **một** process, batch theo cycle.
- Timeout **wall-clock** cho cả request, và process bị kill + respawn nếu vượt; trạng thái trả về
  `timeout` như hiện tại (`kronos.py:300-307`).
- **Không** dùng `_venue_candle_source` (mở executor mỗi lần, `shadow_worker.py:209`) trên đường
  quyết định: nến đóng đã có từ `_closed_futures_context` (1188); chỉ đọc thêm lịch sử qua cùng
  bridge đã dựng.

### 5.4 Missing / timeout / stale — policy minh bạch, ưu tiên defer

Yêu cầu: "missing/timeout/stale phải có policy minh bạch (ưu tiên defer/skip trước tốn LLM, không
silently bỏ gate)".

| Trạng thái | Hành vi | Lý do |
|---|---|---|
| interpreter/weights thiếu (`unavailable`) | **defer**, ghi `unavailable`, **không** chạy LLM cho ứng viên đó | Không có gate ≠ gate pass. Ghi rõ lý do để người vận hành thấy |
| hash mismatch | **defer** + audit warning | `verify_artifacts` đã fail-closed (`kronos.py:147-173`) |
| `timeout` | **defer** + đếm | 22-135 s đã vượt ngân sách phần dành cho prefilter |
| series không đủ bar / stale | **defer**, ghi `stale` | `MIN_LOOKBACK_BARS=32` (`kronos.py:76`), `validate_candles` từ chối gap (`recorder.py:111-115`) |
| verdict `unclear` | **không** defer — vẫn cho qua **kèm** ngữ cảnh, hoặc bỏ theo policy | Xem dưới |

Điểm tinh tế: `unclear` **không** phải lỗi. Nếu `unclear` bị coi là lỗi và defer, prefilter thành
"chỉ giao dịch khi model tự tin" — một hành vi trading mới, chưa kiểm chứng. Vì vậy:
`unclear_action: "skip" | "pass_through"`, **mặc định `skip`** (đúng ý "lọc ứng viên"), nhưng
**đổi được bằng config** để đo cả hai.

**Không silently bỏ gate:** mọi nhánh đều ghi một bản ghi `kronos_prefilter` với `verdict`,
`reason`, `status`, `paths`, `ratio`, `duration_ms` — kể cả nhánh defer. Không có "im lặng".

### 5.5 Cache key — đóng nến, không dùng kết quả sau cutoff

> **Trạng thái: cache CHƯA TỒN TẠI.** Kiểm bằng
> `findstr /s /n /c:"forecast_cache" /c:"cache_key" tradingagents\shadow\*.py server\shadow_store.py server\shadow_worker.py`
> → **rỗng**. Mục này là **thiết kế phải build**, không phải mô tả code hiện có. Đừng đọc nó như
> "đã có cache, chỉ cần nối". Ngược lại: hiện **không có look-ahead nào** trên đường quyết định,
> đơn giản vì đường đó **chưa hề gọi Kronos** (0 tham chiếu kronos/shadow/forecast trong
> `autoloop.py`, `daily.py`, `main.py`).

Yêu cầu key: symbol/venue/market/timeframe/model revision/params/seed, **và** không dùng kết quả
sau cutoff.

```
key = sha256(
    symbol | venue | market_type | timeframe
    | data_open_time            # nến đóng cuối cùng dùng làm input
    | model_revision           # provider.model_revision (kronos.py:203)
    | artifact_hash            # provider.artifact_hash (kronos.py:219,436-441)
    | params                   # paths, lookback, temperature, top_p, horizon
    | seed                     # stable_seed(cycle|provider) — provider.py:172-182
)
```

Hai điều kiện bắt buộc, lấy từ logic đã có:
1. **Chỉ cache theo nến ĐÓNG.** `data_open_time` là nến cuối (`kronos.py:263`), và
   `validate_candles` đảm bảo không có nến đóng sau `as_of` (`recorder.py:107-110`). Nến đang hình
   thành **không** được sinh key.
2. **Không dùng kết quả sinh sau cutoff.** Đây chính là `generation_kind`
   (`recorder.py:217-222`): nếu `prediction_created_at > first_realized_close` thì bản ghi là
   `retrospective`. Cache **phải lưu `generation_kind` và `prediction_created_at`**, và **từ chối**
   phục vụ một entry `retrospective` cho một quyết định forward. Nếu không, replay sẽ âm thầm bơm
   look-ahead vào đường thật — đúng loại lỗi mà `AGENTS.md` cấm.

Với `-I` + hash verify, cache chỉ **tăng tốc**, không bao giờ đổi kết quả: `stable_seed` là SHA-256
theo `(cycle_id, provider_id)` (`provider.py:172-182`) nên cùng input → cùng seed → cùng paths.

### 5.6 Refill — không biến top vài mã thành "không giao dịch mãi"

Yêu cầu: "Nếu lọc hết phải refill từ ranked eligible universe."

Cơ chế có sẵn: `_refill_futures_candidates` (663-811) đã lặp vòng, **gấp đôi** pool mỗi lần
(`_futures_discovery_limit`, 653-660), cap `AUTO_FUTURES_MAX_UNIVERSE = 240`, tối đa
`AUTO_FUTURES_REFILL_ROUNDS = 2`, và giới hạn ngân sách `AUTO_REFILL_BUDGET_SHARE = 0.5`.

Thiết kế chỉ cần **một** bổ sung: prefilter phải là một **stage trong vòng refill**, không phải
một cổng chặn sau refill. Cụ thể:

```
round 0: screen(pool=60)  → preflight → Kronos prefilter → eligible
         nếu eligible < AUTO_TOP_N:  refill round 1 (pool=120) → prefilter lại
         nếu vẫn thiếu:              refill round 2 (pool=240) → prefilter lại
         hết round: chạy với những gì có, ghi shortfall (đúng như 641-650 hiện tại)
```

Điểm phải giữ: `_select_cycle_candidates` hiện **sort crypto trước** (637) rồi cắt `[:limit]`.
Prefilter phải chèn **trước** bước cắt đó, nếu không ta cắt 3 mã rồi mới lọc → lọc hết → refill
vô ích.

**Chống "kẹt top vài mã":** candidate đã bị Kronos loại trong cycle này **không** được vào lại
trong cùng cycle (tránh vòng lặp), nhưng **không** bị blacklist lâu dài — nến mới ở cycle sau là
input mới. Cache key theo `data_open_time` tự nhiên cho điều này.

### 5.7 Compact typed forecast block (đưa vào `execution_context`)

Yêu cầu: "Trader/PM được cung cấp compact typed forecast + timestamp/horizon/range/sample-ratio".

Đề xuất block (ngắn, có kiểu, có nhãn trung thực) — nối vào string đã có ở
`render_futures_decision_context` (`futures_decision_context.py:129`) hoặc như một đoạn riêng nối
vào `execution_context` trước khi truyền (1300-1301):

```
Kronos forecast (model sample ratio, NOT a probability)
  symbol=BTC/USDT:USDT timeframe=4h horizon=24h bars=6
  data_open_time=2026-11-08T16:00:00Z  as_of=2026-11-08T20:00:00Z
  generation_kind=forward  anchor_close=74200.0
  paths=16  per_path_samples=1
  direction: up=12/16 (0.750) down=3/16 (0.188) flat=1/16 (0.063)
  end_close p10=..., p50=..., p90=...   band_spread=0.041
  model=NeoQuasar/Kronos-small@901c26c1  artifact=<sha256 prefix>
  latency_ms=22491  status=ok
```

Quy tắc:
- **Luôn** có `paths` + `per_path_samples` cạnh tỷ lệ — để không ai đọc "0.75" thành xác suất.
- **Luôn** có `data_open_time` + `horizon` + `generation_kind` — để PM biết dự báo nói về khoảng nào.
- **Không** đưa `stdev` một mình; đưa `p10/p50/p90` (đã có trong `summarise_paths`,
  `kronos.py:420-428`) vì đó là thứ dùng được cho sizing/bracket.
- PM Trader **vẫn** phải tự quyết; block này là **dữ liệu**, không phải chỉ thị. Không có câu
  "buy/sell" trong block.

---

## 6. So sánh thực nghiệm khả thi — kết quả và giới hạn

### 6.1 Thiết lập

Cùng nguồn, cùng khung, cùng chi phí, cùng số sample:

- Dữ liệu: 4h, **10 symbols**, 400 bar thật, `binanceusdm`, read-only (`data/universe-4h.json`).
- 20 điểm quyết định (index 124→390, cách 14 bar), horizon 6 bar = 24h → **200 quan sát**.
- Kronos: 16 paths/symbol, `sample_count=1`, batch một lần/điểm quyết định, threshold 0.70.
- Simple: **chính** `donchian_breakout` của repo (lookback 3), cùng nến
  (`tradingagents/shadow/filters.py:77-99`).
- Realized direction đọc từ **cùng chuỗi** (`records[].realized_direction`).

### 6.2 Kết quả

| Arm | eligible | ratio | long | short | hit rate | mean signed return | missed up | missed down |
|---|---|---|---|---|---|---|---|---|
| **Kronos** | 65/200 | 0.325 | **30** | **35** | 0.677 | +0.0148 | 38 | 97 |
| Simple (donchian) | 67/200 | 0.335 | **29** | **38** | 0.299 | −0.0133 | 38 | 95 |
| No filter | 200/200 | 1.000 | — | — | — | — | — | — |

Inference overhead: **21 874 ms median/điểm quyết định** (160 rows), tổng **438 s** cho 20 điểm.

Nguồn: `data/compare-prefilter.json`.

> **Caveat bắt buộc đọc kèm:** phép so sánh này chạy ở **`T=1.0`** với **một seed cho mỗi điểm
> quyết định**. Theo §3B.1, ở cấu hình đó verdict thay đổi 4-8/10 lần trên 10 seeds khi giữ nguyên
> nến. Nghĩa là các con số `long=30`, `short=35`, `hit=0.677` dưới đây **là một lần rút thăm của một
> cổng không ổn định**, không phải giá trị kỳ vọng của nó. Chúng đo **chi phí và độ phủ** (những thứ
> ổn định về mặt cơ học), **không** đo chất lượng lọc. Muốn đo chất lượng lọc phải chạy lại ở `T=0.5`
> và **lặp qua nhiều seed** — việc đó chưa làm trong ask này (xem §10 BL-7).

### 6.3 Đọc kết quả cho đúng

**Điều đọc được (chắc chắn):**
- Kronos **giữ cả hai hướng**: 30 long / 35 short — không thiên một phía. Đây là kiểm tra sống còn
  cho yêu cầu "giữ hai hướng long/short, không mặc định chỉ buy", và nó **pass**.
- Kronos cắt **67.5%** ứng viên → `saved_llm_calls` = 135/200 (mỗi ứng viên tiết kiệm 8-10 LLM call
  theo `autoloop.py:90-93`).
- Simple filter ở cấu hình này **lệch short** (29 vs 38) và hit rate 0.299 — nó **không** phải một
  baseline "yếu hơn chút"; trên khung 4h/6-bar nó gần như phản tín hiệu. Đây là lý do phải so sánh
  với chính filter của repo thay vì tự viết một filter "đơn giản" khác.
- Chi phí: 22 s/điểm cho 10 symbols. Với pool lớn hơn, đây là hạng mục phải đo tiếp (§8).

**Điều KHÔNG đọc được (và tôi không tuyên bố):**
- **Không** có bằng chứng lợi nhuận. Hit rate 0.677 là **hướng**, không phải PnL: không có size,
  không có phí, không có stop/target ordering, không có slippage. `mean_signed_return` +1.48% chỉ là
  trung bình return có dấu nếu vào đúng hướng — **không** trừ phí, **không** tính drawdown.
- **200 quan sát là quá nhỏ.** Với 65 eligible, khoảng tin cậy của hit rate 0.677 rất rộng. Không
  đủ để nói "Kronos giỏi hơn donchian"; chỉ đủ để nói "đáng đo nghiêm túc".
- **Chồng lấn cửa sổ:** 20 điểm quyết định trên 10 symbols trong ~6 tuần có tương quan chéo cao
  (crypto beta chung). Số quan sát **hiệu dụng** thấp hơn 200 nhiều.
- **Training cutoff chưa biết** (xem 6.4) → chưa loại trừ được việc "hit" đến từ việc model đã thấy
  dữ liệu. Với cửa sổ 2026-09-25 → 2026-11-08, nếu model train tới giữa 2026 thì kết quả trên là
  **memory test**, không phải dự báo.

### 6.4 Model training cutoff — chưa xác định được (blocker)

Đã kiểm tra:
- `config.json` của `Kronos-small`: chỉ có siêu tham số kiến trúc (`d_model`, `n_layers`, ...),
  **không** có mốc thời gian.
- Metadata cache HF: chỉ có commit hash + blob sha + timestamp tải về.
- Model card công khai `NeoQuasar/Kronos-small`: chỉ nêu **"over 45 global exchanges", "over 12
  billion K-line records"** — **không** công bố khoảng thời gian huấn luyện hay cutoff.

→ **Không thể chứng minh cửa sổ 2026-09→2026-11 là out-of-sample.** Đây là blocker phải nêu, không
được lấp bằng giả định. Mọi kết quả §6.2 phải đọc kèm cảnh báo này.

### 6.5 Prospective / out-of-sample — con đường trung thực

Vì cutoff không xác định, chỉ có **một** cách đo sạch: **prospective**. Cơ chế đã có sẵn:
`generation_kind=forward` chỉ khi `prediction_created_at <= first_realized_close`
(`recorder.py:217-222`), và `evaluate_forecast` **từ chối** chấm khi horizon chưa realized
(`evaluation.py:54-76`, trả `()`).

Nghĩa là: bật prefilter, để nó chạy forward vài tuần, rồi chấm bằng `evaluate_forecast` trên những
dòng `forward` **thật**. Đó là con đường duy nhất không bị nhiễm cutoff.

### 6.6 Dữ liệu lịch sử local — đã kiểm, không dùng được trực tiếp

- 173 file CSV thật trong `~/.tradingagents/cache` (`*-YFin-data.csv`), nhưng là **daily**
  (`Date, Close, High, Low, Open, Volume`; BTC: 1827 dòng, 2021-10-28 → 2026-10-28).
- Adapter chỉ nhận `1h`/`4h` (`kronos.py:83`), `_TIMEFRAME_HOURS` chỉ có hai khoá đó.
- Fixture `tests/fixtures/futures_candles_4h.json` khai báo `"kind": "synthetic"`, 15 bar, trong đó
  chỉ 9 bar không-flat và 6 lead-in bar **là phẳng, tự suy ra**, ghi rõ trong metadata. **Không dùng
  để đo hiệu năng.** (Ask cấm rõ điều này.)
- Kết luận: cần OHLCV 1h/4h thật. **Đã lấy được** read-only public (không key, không lệnh) — 600×1h,
  1000×4h, 400×4h×10 symbols. Không cần escalation vì network capability **được phép và đã hoạt động**
  (4/4 sàn public trả dữ liệu, `probe_egress.py`).

---

## 7. Seam & module — split proposal trước khi chạm

### 7.1 Vấn đề: test pin chặn đường

`tests/test_shadow_isolation.py:281-305` khẳng định `tradingagents/graph`, `tradingagents/agents`
và `server/autoloop.py` **không** import `tradingagents.shadow*` / `server.shadow*`. Đây là pin
**đúng** cho shadow. Nhưng nó có nghĩa: **không thể** đặt forecast service trong `shadow/` rồi gọi
từ autoloop.

→ **Bắt buộc extract core service ra ngoài chữ "shadow"**. Không phải refactor thẩm mỹ; là điều
kiện kỹ thuật.

### 7.2 Đề xuất: `tradingagents/forecast/` (core, trung lập)

Chuyển (không chép) các phần **không** thuộc shadow:

```
tradingagents/forecast/
    contract.py    ← từ shadow/provider.py: ForecastRequest, PathSummary,
                     ForecastAttempt, direction_of, stable_seed, các hằng
                     DISTRIBUTION_*, STATUS_*, CALIBRATION_*
    sampler.py     ← MỚI: client cho persistent isolated process (JSON-lines),
                     batch predict_batch, timeout wall-clock, respawn
    artifact.py    ← từ shadow/kronos.py: hash pin, verify_artifacts, *_dir,
                     isolated_python
    service.py     ← MỚI: (symbols, timeframe, horizon, paths, as_of) -> list[ForecastResult]
                     + cache theo §5.5
```

`tradingagents/shadow/` **giữ** phần đúng là shadow và **import lại** core:
`kronos.py` (adapter mỏng trên `forecast.artifact` + `forecast.sampler`), `filters.py`,
`recorder.py`, `evaluation.py`, và `server/shadow_worker.py` không đổi hành vi.

Lợi ích: test pin ở `test_shadow_isolation.py:281` vẫn **đúng và còn mạnh hơn** (graph/agents
không import `shadow`), trong khi đường active dùng `tradingagents.forecast` không dính "shadow".

**Không** tạo wrapper tương thích cho đường cũ (AGENTS: "Do not preserve backward compatibility").
`shadow.kronos` re-export từ `forecast` là chấp nhận được **chỉ** vì shadow vẫn là consumer thật;
nếu muốn sạch hơn thì sửa thẳng `shadow.kronos` thành lớp mỏng.

### 7.3 Đề xuất: `server/candidate_policy.py` (trích từ autoloop)

`server/autoloop.py` = **2585 dòng**, và dòng 3 đã có `# ai-note: split declined again` cho đúng
block futures discovery/refill — tức repo đã cân nhắc và hoãn. Ask yêu cầu **split proposal cụ thể
theo AGENTS trước khi chạm**, không mở broad refactor.

Đề xuất (không làm trong ask này):

- **Seam:** block futures discovery/refill + prefilter, hiện tự nhận là "self-contained" trong
  chính ai-note đó. Đó là ranh giới trách nhiệm đúng, không phải ranh giới số dòng.
- **Module mới:** `server/candidate_policy.py` chứa `_screen_with_timeout`, `_preflight_candidates*`,
  `_select_cycle_candidates`, `_futures_discovery_limit`, `_refill_futures_candidates`,
  và mới: `apply_kronos_prefilter`.
- **Vì sao đúng lúc:** prefilter thêm một stage vào **đúng** block này; thêm nó vào autoloop 2585
  dòng sẽ làm block đó phình thêm và bít luôn khả năng extract sau này (AGENTS: "repeated edits in
  different places by unrelated tasks" + "just one more function on a module already showing split
  signals").
- **Không** đụng cycle/scheduler/endpoint concerns — chúng ở lại `autoloop.py`, đúng như ai-note nói.
- **Cần người quyết định.** AGENTS escalation covenant: nêu proposal, **dừng** cho user chọn. Ask này
  chỉ nêu, không thực hiện.

### 7.4 Điểm chèn chính xác (khi implement)

**Đính chính quan trọng (review vòng 2, đã kiểm lại source):** bản trước của bảng này trỏ điểm #1
tới `autoloop.py:1038-1048`. **Đó là chỗ SAI** và nó sẽ chặn cứng refill. Lý do, đọc từ source:

- `_select_cycle_candidates:638` làm `selected = runnable[:limit]` — **cắt top-3 TRƯỚC**;
- `:1035-1037` trả `candidates` **đã cắt** vào `_refill_futures_candidates`;
- trong đó `:722` seed `vetted = list(selected)` (đã cắt), và vòng `while len(vetted) < limit`
  ở `:732` chỉ chạy khi preflight ra **dưới `limit`**.

Nghĩa là **điều kiện refill được quyết định hoàn toàn bởi preflight, trước khi Kronos chạy một lần
nào**. Chèn prefilter ở 1038-1048 = chèn **sau** cả truncation lẫn quyết định refill → nếu Kronos
loại sạch thì refill **không bao giờ chạy**, và ta tái tạo đúng "candidate starvation" mà §5.6 cảnh
báo.

| # | File:dòng | Thay đổi |
|---|---|---|
| **1** | **`server/autoloop.py:637-638`** | **Prefilter chạy TRƯỚC `runnable[:limit]`.** Đây là điểm chèn duy nhất đúng: lọc xong mới cắt top-N |
| **2** | **`server/autoloop.py:722,732`** | **Điều kiện refill phải tính cả Kronos:** `vetted = [x for x in selected if x.kronos_eligible is not False]`, để `while len(vetted) < limit` bật khi Kronos (không chỉ preflight) làm thiếu slot |
| 3 | `server/autoloop.py:766-780` | vòng refill cũng phải prefilter `round_vetted` trước khi merge vào `vetted` — nếu không, ứng viên mới từ refill vào thẳng mà không qua Kronos |
| 4 | `server/autoloop.py:1262-1270` | nối compact block vào `execution_context` |
| 5 | `tradingagents/graph/trading_graph.py:145-176` | **không đổi** — `execution_context` đã vào signature hash |
| 6 | `tradingagents/default_config.py:394-396` | thêm key mới (default off) + `_ENV_OVERRIDES` nếu cần env |
| 7 | `server/operator_config.py:32-39` | thêm switch có confirmation + audit (mẫu Auto) |
| 8 | `server/migrations/0012_*.sql` | bảng `kronos_prefilter` (verdict/reason/paths/ratio/latency/kind) |
| 9 | `server/shadow_api.py` → hoặc `server/kronos_api.py` | GET evidence **cho prefilter**, tách khỏi shadow |

**Acceptance test bắt buộc cho ordering này** (nếu không có, lỗi này sẽ quay lại):

```
# Kronos loại tất cả top-3 nhưng pool còn ứng viên đủ điều kiện
→ refill PHẢI chạy, và cycle kết thúc với 3 ứng viên eligible hoặc
  ghi shortfall rõ ràng — KHÔNG được kết thúc với 0 ứng viên trong khi
  runnable còn ứng viên chưa thử.
```

Test này phải fail trên thiết kế cũ (chèn ở 1038) và pass trên thiết kế đã sửa — đó là bằng chứng
ordering đã đúng.

---

## 8. Dashboard evidence — đồng bộ, có nhãn đúng

Yêu cầu: "Kronos passed/rejected/unclear/error, saving calls, actual latency, forecast sample ratio
không gọi calibrated win probability."

Đề xuất surface (GET-only, như `shadow_api.py:43`):

| Trường | Nguồn | Nhãn bắt buộc |
|---|---|---|
| `verdict` | `long_eligible` / `short_eligible` / `unclear` / `skipped` / `error` | — |
| `paths`, `per_path_samples` | `summarise_paths` | luôn hiện cạnh ratio |
| `direction_ratio` | `samples.direction_ratio` (`kronos.py:419`) | **"model sample ratio — not a probability"** (dùng `CALIBRATION_NOTES`, `provider.py:54-64`) |
| `band` (p10/p50/p90) | `samples.end_close` (`kronos.py:420-427`) | — |
| `latency_ms` | **wall-clock của process** | **phải sửa** `duration_ms` (xem §1.7-1) |
| `saved_llm_calls` | `rejected × LLM_CALLS_PER_CANDIDATE` | **là estimate**, phải ghi "estimated" |
| `generation_kind` | `recorder.py:220-222` | `forward` vs `retrospective` hiển thị nổi bật |
| `outcome_group` | `recorder.py:133-149` | kèm note rằng `executed` **không** đại diện universe |
| **`ratio_spread`** | đo stability (§3B.4) | **hạng nhất**, không phải ghi chú: verdict không có spread là chưa đủ tin |
| **`temperature`** | `KronosSettings` (`kronos.py:182`) | hiển thị cạnh verdict — cùng verdict ở `T=1.0` và `T=0.5` nghĩa là hai độ tin khác nhau |

Bốn điều **cấm** trên dashboard:
1. Cấm nhãn "win probability", "accuracy", "confidence" cho `direction_ratio`.
2. Cấm hiển thị `saved_llm_calls` như số thật — LLM calls/ứng viên là 8-10 (`autoloop.py:90`),
   nên đây là khoảng, phải ghi `estimated_range`.
3. Cấm gộp `retrospective` với `forward` trong một con số hit-rate.
4. Cấm hiển thị `net_r`/PnL như của Kronos — `evaluate_forecast` chỉ nhận `net_r` pass-through
   (`evaluation.py:88-104`), shadow hiện truyền `None` (`shadow_worker.py:400-404`).
5. Cấm hiển thị verdict mà **không** kèm `ratio_spread`/`temperature`: ở `T=1.0` verdict đó là một
   lần rút thăm (§3B.1), và một con số không có ngữ cảnh đó là thông tin sai lệch.

---

## 9. Acceptance tests & benchmark commands (đề xuất, chưa implement)

### 9.1 Unit/contract tests (không cần model, không cần DB)

```
.venv\Scripts\python.exe -m pytest -q tests/test_forecast_contract.py
.venv\Scripts\python.exe -m pytest -q tests/test_forecast_cache.py
.venv\Scripts\python.exe -m pytest -q tests/test_candidate_policy.py
```

Phải phủ:
- Cache key **đổi** khi `data_open_time`, `model_revision`, `artifact_hash`, `params`, `seed`,
  `venue`, `timeframe` đổi (7 biến riêng biệt). **`temperature` phải nằm trong `params`** — nếu không,
  một entry `T=1.0` sẽ được phục vụ cho một cycle `T=0.5` và mọi phân tích stability thành vô nghĩa.
- Cache **từ chối** phục vụ entry `retrospective` cho quyết định forward.
- `paths < min_paths` → **không** ra `long`/`short`, chỉ `unclear`.
- `0.70` với `paths=16` → đúng `≥12`, không phải `11` (dùng `math.ceil`, xem lỗi thật đã gặp §9.4).
- `unavailable`/`timeout`/`stale` → `defer`, **không** chạy LLM, **có** ghi bản ghi.
- Prefilter **không** acquire `execution_lock` (assert qua mock).
- `_raise_if_mode_disabled` được kiểm **sau** inference (assert thứ tự gọi).
- Off-mode: prefilter không đọc/call gì; kết quả cycle **giống hệt** baseline.
- **`stability_check`**: hai seed cho hai verdict khác nhau → kết quả cuối **phải** là `unclear`,
  không được lấy seed đầu. Test này pin đúng lỗi mà §3B phát hiện.

Phải phủ thêm cho **stability** (điều kiện merge, không phải nice-to-have):
- `T=0.5` là giá trị mặc định của cổng active; `T=1.0` chỉ được phép khi `mode=context_only`.
- Với `T=1.0` + `mode=filter`, config validation **từ chối** (vì verdict không ổn định — §3B.1-3B.2).

### 9.2 Isolation pins (phải giữ xanh)

```
.venv\Scripts\python.exe -m pytest -q tests/test_shadow_isolation.py
```

`test_graph_and_agents_never_import_the_shadow_layer` phải **tiếp tục pass**. Nếu implementation
khiến nó fail, đó là dấu hiệu đã đi sai seam.

### 9.3 Benchmark (đã chạy được trong ask này)

```
"%LOCALAPPDATA%\tradingagents-research\kronos-venv\Scripts\python.exe" -I ^
  "%LOCALAPPDATA%\tradingagents-research\kronos-integration-20261110\scripts\bench_shapes.py" ^
  --data "%LOCALAPPDATA%\tradingagents-research\kronos-integration-20261110\data\universe-4h.json" ^
  --out <out.json> --shape batched --paths 16 --pred-len 6 --repeats 1
```

Đo được: 160 rows / 22.5 s. **Đây là benchmark đã chạy thật**, không phải điểm chưa làm.

Benchmark **stability** (điều kiện merge — đã chạy, phải chạy lại ở `T=0.5`):

```
"%LOCALAPPDATA%\tradingagents-research\kronos-venv\Scripts\python.exe" -I ^
  "%LOCALAPPDATA%\tradingagents-research\kronos-integration-20261110\scripts\verify_temperature_stability.py" ^
  --data "%LOCALAPPDATA%\tradingagents-research\kronos-integration-20261110\data\universe-4h.json" ^
  --out <out.json> --paths 16 --pred-len 6 --seeds 10
```

Điều kiện pass đề xuất: ở `T` chọn, **≥8/10 symbols** giữ một verdict duy nhất qua ≥10 seeds **và**
`fire_rate ≥ 0.30`. Dưới mức đó thì cấu hình chỉ được dùng `mode=context_only`.

Việc **chưa làm**: benchmark với pool 60 symbols, đo RAM, đo trên GPU, và stability ở `T=0.5` trên
universe rộng. Ghi ở §8/§10 (BL-3, BL-7).

### 9.4 Một lỗi thật đã gặp trong chính harness (ghi lại để không lặp)

Lần chạy grid đầu tiên tính `need` bằng `-(-int(round(0.70 * 16)) // 1)` → ra **11** thay vì
**⌈11.2⌉ = 12**, tức cổng bị nới lỏng âm thầm và kết quả "ổn định" bị thổi lên. Đã sửa thành
`math.ceil(args.threshold * paths)` và **chạy lại toàn bộ**; số liệu trong §3B là số của lần chạy đã
sửa. Đây đúng là loại lỗi mà test "0.70 với paths=16 → đúng ≥12, không phải 11" ở §9.1 phải chặn.

### 9.5 Trước khi merge (theo AGENTS)

```
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q
```

Ask này **không** chạy full suite (đúng yêu cầu "Không chạy full suite").

---

## 10. Blockers & cần người quyết định

| # | Blocker | Ảnh hưởng | Đề xuất |
|---|---|---|---|
| **BL-1** | **Training cutoff của Kronos không công bố** (model card chỉ nêu "45 exchanges / 12B K-lines") | Không chứng minh được cửa sổ 2026-09→11 là out-of-sample → §6.2 có thể là memory test | Chỉ tin **prospective** (`generation_kind=forward`); đừng dùng backtest làm bằng chứng. Cần người quyết định chấp nhận đo prospective vài tuần |
| **BL-2** | **Split `autoloop.py` (2585 dòng) / extract `tradingagents/forecast/`** | Chạm file lớn đã có ai-note từ chối split; đổi cấu trúc module | **Cần user chọn** theo AGENTS escalation covenant trước khi implement. Đề xuất §7.2+§7.3 |
| **BL-3** | **ĐÃ ĐÓNG — đo thật pool 60** | 192 s (không phải 135 s) và **peak 6.07 GiB**; chunk 5 → 874 MiB / 210 s | Không còn blocker. **Bắt buộc chunk** (§2.6c); đặt `max_batch_rows` trong config, mặc định 80 rows (5 symbols × 16 paths) |
| **BL-4** | **`duration_ms` đánh giá thấp chi phí thật** | Mọi quyết định dựa trên `duration_ms` trong DB là sai. Đo của tôi: 63.6 ms sampler vs 2 765 ms wall ⇒ **~43×** ở cấu hình 96 bar/1 path. Reviewer đo độc lập qua adapter thật ở cấu hình 40 bar/4h: 1 048.2 ms self-reported vs 3 610.4 ms wall ⇒ **3.44×**. **Tỷ lệ phụ thuộc cấu hình** — điều bất biến là `duration_ms` *luôn* bỏ cold-load (`kronos_runner.py:72` bắt đầu timer sau `import torch` ở `:43` và load weights ở `:46-51`), nên đừng dùng một con số cố định | Sửa thành wall-clock trước khi dashboard dùng (§1.7-1). Ghi tỷ lệ **kèm cấu hình đo**, không phổ quát hoá |
| **BL-5** | **CPU-only trên máy này** | Không nói được gì về hiệu năng GPU/nơi khác | Ghi rõ môi trường đo; nếu deploy ở máy khác phải đo lại |
| **BL-6** | **Không có bằng chứng lợi nhuận** | 200 quan sát, không phí, không drawdown, tương quan chéo cao | Bắt buộc dùng `tradingagents/experiments` với `report()` đầy đủ (phí, drawdown, sensitivity) trước khi coi là "hoạt động" |
| **BL-7** | **ĐÃ ĐÓNG — và kết quả ĐỔI KẾT LUẬN** | Đo ở đúng quy mô merge (10 symbols × 10 seeds): `T=1.0` → **2/10** ổn định; `T=0.5` → **3/10**. Cả hai **fail** ≥8/10 | Cổng hướng cứng **bị loại**; Design B hạ cấp thành **`mode=context_only`** (§4.1, §3B.3b). Kết luận cũ "`T=0.5` → 2/3" là **mẫu nhỏ sai** — đã tự đính chính |
| **BL-10** | **Forecast phụ thuộc batch (RNG toàn cục)** | Cùng nến + cùng seed, đổi batch/thứ tự → kết quả khác (`kronos.py:438,442` dùng `torch.multinomial` trên RNG toàn cục). Làm **cache key theo symbol không hợp lệ** | **Bắt buộc** seed tường minh trước **mỗi** path (đã chứng minh đủ — §2.6d). Là điều kiện tiên quyết của cả cache (§5.5) và so sánh giữa cycle |
| **BL-8** | **Đánh đổi `T` chưa được người vận hành chấp thuận** | `T=0.5` làm cổng ổn định nhưng **hẹp phân phối** so với `T=1.0` mà shadow đang dùng (`kronos.py:182`). Hai chế độ cho hai mục đích khác nhau | **Cần user chọn**: (a) `T=0.5` cho active + giữ `T=1.0` cho shadow-observation, hoặc (b) một `T` duy nhất và chấp nhận cổng không ổn định → context-only |
| **BL-9** | **Contract `metric_name` lệch — đang FAIL thật trên delivery (không phải giả thuyết)** | §11 dựa vào experiments làm "đường test thật"; đường `--register-run` hiện **hỏng**. Chạy `verify_research_delivery.py --stage experiments` → **`1 failed, 58 passed`** (`test_cli_registration_populates_the_registry`, `KeyError: 'metric_name'`). Gốc: `experiment_store.record_results` index `row["metric_name"]` (`server/experiment_store.py:113`) nhưng rows sinh ở `runner.py:246-254` **không có key đó**; `metric_name` xuất hiện **0 lần** trong `tradingagents/experiments/*.py`. Unit test xanh vì `tests/test_experiments_registry.py:33` **tự viết tay** `"metric_name"` vào fixture | **Phải sửa trước khi dùng §11**: thống nhất contract (thêm `metric_name` vào row, hoặc cho `record_results` đọc từ `manifest["criteria"]["metric"]`). Đây là code của delivery hiện có, **không phải feature Kronos** — sửa riêng, không gộp vào ask này |

---

## 11. Tái dùng hạ tầng experiments (đường test thật)

Không cần viết harness mới. `tradingagents/experiments/` đã có đúng thứ cần:

- `runner.run_experiment(...)` chạy N variant trên **cùng** `candles_holdout`, mỗi repeat seed riêng,
  chấm bằng `paired_stats` với criteria khoá (`runner.py:126-196`, `build_manifest` 81-123).
  Có `assert_no_leakage()` (153) và sandbox isolation (171-191).
- `baselines.rule_source(rule, recorded_cycles)` dùng **chính** `server.futures_replay.RULES`, không
  fork (`baselines.py:131-209`); `recorded_source` replay bot như nó đã quyết (212+).
- `runner._llm_usage(source)` đã đếm `llm_calls`/`llm_tokens`/`llm_latency_ms` (`runner.py:283-291`) —
  **đây là chỗ lấy `saved_llm_calls` thật**, không phải nhân bằng hằng số.

Đề xuất 3 variant cho một run:
1. `baseline` — `recorded_source` (bot như đã quyết).
2. `kronos_prefilter` — prefilter + context.
3. `simple_prefilter` — `donchian_breakout` cùng contract.

Metric: `report()` đầy đủ (net PnL sau phí, drawdown) **không** phải hit rate. Với
`min_paired_repeats` hiện có, một run 20 quan sát sẽ **không** đạt; cần prospective hoặc dữ liệu
1h/4h dài hơn.

**Nhưng đọc BL-9 trước:** đường `--register-run` của experiments đang **fail thật** (`KeyError:
'metric_name'`, đã tái hiện `1 failed, 58 passed`). Cho tới khi contract đó được sửa, §11 **chưa**
là đường test end-to-end dùng được — chỉ phần `run_experiment`/`report()` là dùng được.

---

## 11B. Đối chiếu review vòng 2 — cái gì đúng, cái gì tôi phải cải chính

Review vòng 2 nêu 4 finding. Tôi kiểm lại từng cái bằng source + đo, và **một** trong số đó cần
phản biện. Ghi lại đầy đủ để người đọc không phải tin ai:

| Finding | Verdict | Bằng chứng |
|---|---|---|
| **B1 — điểm chèn §7.4 sai, chặn refill** | **ĐÚNG, đã sửa** | `_select_cycle_candidates:638` cắt `[:limit]` trước; `:722` seed `vetted` từ đã-cắt; `:732` `while len(vetted) < limit` chỉ phụ thuộc preflight. §7.4 nay trỏ `:637-638` + sửa điều kiện `:722,732` |
| **C — contract `metric_name` fail thật** | **ĐÚNG, đã thêm BL-9** | Chạy `--stage experiments` → `1 failed, 58 passed`, `KeyError: 'metric_name'`; `experiment_store.py:113` index `row["metric_name"]`, `runner.py:246-254` không sinh key đó; `metric_name` có **0** lần trong `tradingagents/experiments/*.py`; test xanh nhờ fixture tự viết tay ở `test_experiments_registry.py:33` |
| **D — §5.2 giải sai locking** | **ĐÚNG, đã viết lại** | Lock chỉ acquire ở `:205` và `:862`; candidate chạy thread riêng (`:851`) với docstring `:837` "so an LLM stall cannot hold the lock"; `bracket_monitor/position_monitor/position_review` có **0** tham chiếu `execution_lock` |
| **E — `paths` không per-request** | **ĐÚNG, đã thêm cảnh báo** | Đo trên adapter thật: `params={"paths":16}` → **8 paths** (4 744 ms); `KronosSettings(paths=16)` → **16** (8 218 ms). `kronos.py:281,283` đọc `self.settings.*` |
| **"verifier exit 0 dù test fail"** | **KHÔNG đúng — cải chính** | Đo bằng subprocess không pipe: `--stage experiments` → **returncode 1**, `gate_blocks_on_failure: true` (`data/verifier-exitcode.json`). Code khớp: `run_stage` trả `_run_pytest(...)` (`verify_research_delivery.py:267`), `main` trả `run_stage` (`:586`), `sys.exit(main())` (`:590`). Quan sát "exit 0" trước đó là do **pipe `\| more`** che mã thoát — chính tôi cũng mắc lỗi này một lần khi đo. Gate **có** chặn; cái còn thiếu chỉ là BL-9 |

**Bài học giữ lại:** hai lần trong quá trình này, một phép đo qua pipe `| more` / `%ERRORLEVEL%`
trong CMD cho mã thoát sai. Mọi kết luận về exit code trong báo cáo này đều lấy từ
`subprocess.run(...).returncode` trong Python, không qua shell.

---

## 12. Artifact & tái lập

Tất cả trong `%LOCALAPPDATA%\tradingagents-research\kronos-integration-20261110\` (ngoài repo,
không secret, không key):

| File | Nội dung |
|---|---|
| `data/ohlcv-1h-binanceusdm.json` | 600 bar 1h × 3 symbols, read-only public |
| `data/ohlcv-4h-binanceusdm.json` | 1000 bar 4h × 3 symbols |
| `data/universe-4h.json` | 400 bar 4h × 10 symbols |
| `data/bench-inference.json` | cold/warm, 4 configs × 9 run |
| `data/bench-scaling.json` | horizon & sample scaling |
| `data/shape-{spawn,sequential,batched}.json` | 3 kiến trúc chạy |
| `data/shape-4h-6bar-16paths.json` | cấu hình đề xuất, 3 symbols |
| `data/universe-4h-6bar-16paths.json` | cấu hình đề xuất, 10 symbols |
| `data/compare-prefilter.json` | so sánh Kronos / simple / none, 200 quan sát |
| `data/ratio-power.json` | Wilson CI theo n paths |
| `data/gate-stability.json` | **verdict qua 10 seeds khi giữ nguyên nến (T=1.0)** |
| `data/threshold-sweep.json` | sweep threshold vs stability vs fire rate |
| `data/grid-stability.json` | grid pred_len × paths (6 seeds) |
| `data/pathcount-stability.json` | 16 vs 64 paths |
| `data/deadband.json` | dead band ±0.03…±0.10 |
| `data/cost-gate.json` | magnitude gate theo bội số cost 0.1% |
| `data/consensus-gate.json` | K-bar consensus |
| `data/rank-stability.json` | ranking (Kendall tau, top-N overlap) |
| `data/temperature-stability.json` | **T=1.0/0.7/0.5/0.3 — phát hiện quyết định** |
| `data/path-independence.json` | cùng seed tái lập / khác seed khác kết quả |
| `data/median-stability.json` | median return vs ratio stability |
| `data/paths-api.json` | **`paths` bị bỏ qua khi truyền qua request params (8 vs 16)** |
| `data/verifier-exitcode.json` | **exit code của `--stage experiments` = 1 (gate có chặn)** |
| `data/SUMMARY.json` | tổng hợp artifact đo chi phí |
| `scripts/*.py` | harness (`fetch_candles`, `bench_kronos`, `bench_shapes`, `bench_scaling`, `compare_prefilter`, `ratio_power`, `probe_egress`, `summarize`) + verify (`verify_citations`, `verify_numbers`, `verify_constants`, `verify_*_stability`, `verify_gate_stability`, `verify_deadband`, `verify_cost_gate`, `verify_threshold_sweep`, `verify_rank_stability`, `verify_path_independence`, `verify_median_stability`) |

Chạy lại: mọi script nhận `--data/--out`, chạy bằng `.venv\Scripts\python.exe -I` (harness) hoặc
`kronos-venv\Scripts\python.exe -I` (cần torch). Không cài lại dependency.

Đã tự kiểm tính đúng của báo cáo bằng script (chạy thật trong ask này):
- `verify_citations.py` → **91/91** citation `file:line` khớp nội dung dòng thật (0 fail).
- `verify_numbers.py` → **58/58** con số khớp artifact JSON (0 mismatch).
- `verify_constants.py` → **14/14** hằng số/layout của `autoloop.py` khớp (0 fail).
- `verify_stability_claims.py` → **0 mismatch** trên toàn bộ số §3B.
- `check_paths_api.py` → xác nhận `params={"paths":16}` bị bỏ qua (8 vs 16).
- `check_verifier_exitcode.py` → xác nhận `--stage experiments` trả **returncode 1**.

---

## 13. Kết luận

**CHƯA READY để implement — nhưng đã đủ để sửa tài liệu và chốt cấu hình.**

Kronos là một **tầng lọc + ngữ cảnh** khả thi về mặt chi phí: 22.5 s cho 10 symbols trên CPU, trong
ngân sách 3600 s của cycle. Nó giữ được cả hai hướng long/short (30/35 trong đo thật) và cắt được
67.5% ứng viên trước khi tiêu LLM.

Nhưng **cổng hướng không ổn định ở mọi cấu hình đã đo**, và đây là phát hiện đổi kết luận: ở đúng
quy mô merge (**10 symbols × 10 seeds**), `T=1.0` → **2/10** ổn định, `T=0.5` → **3/10**; cả hai fail
ngưỡng ≥8/10. Kết luận trước đó rằng `T=0.5` là lời giải **đến từ mẫu nhỏ 3 symbols × 6 seeds và đã
được tự đính chính** (§3B.3b). Ngoài ra phát hiện thêm: **forecast phụ thuộc batch** vì sampling dùng
RNG toàn cục (§2.6d), nên **cache theo symbol không hợp lệ** nếu không seed từng path.

**Sáu điều kiện không thể bỏ:**
1. **Kronos vào dạng `mode=context_only`** — cung cấp band/median/tỷ lệ cho PM, **không** cắt ứng
   viên. Cổng hướng cứng đã bị loại bằng đo (§3B.3b), không phải bằng phán đoán.
2. **Extract core service ra khỏi `shadow/`** — test pin `test_shadow_isolation.py:281` chặn đường,
   và đây là điều kiện kỹ thuật, không phải sở thích.
3. **`paths ≥ 16`, set qua `KronosSettings`** — dưới ngưỡng đó `0.70` không phân biệt được với đồng
   xu (Wilson CI chạm 0.438 ở n=3); và truyền qua `params` **âm thầm nhận `DEFAULT_PATHS = 8`**.
4. **Seed tường minh theo TỪNG path** (BL-10) — nếu không, cache không hợp lệ và kết quả không tái
   lập được khi batch đổi (§2.6d). Đây là điều kiện tiên quyết của cả cache (§5.5).
5. **Chunking bắt buộc** — pool 60 một-lần-gọi là **6.07 GiB**; chunk 5 symbols → 874 MiB (§2.6c).
6. **Chỉ tin prospective** — model card không công bố training cutoff, nên backtest trên cửa sổ
   2026-09→11 chưa chứng minh được out-of-sample.

**Khuyến nghị cuối:** chọn **Design B ở dạng `mode=context_only`** (compact typed forecast block,
defer-first, scope futures-crypto-only). Bật sau một schema config có validation, mặc định off, có
dashboard evidence đúng nhãn. **Không** hứa `saved_llm_calls` — context-only không tiết kiệm LLM, chi
phí Kronos là **thêm vào** (192 s + chunked RAM mỗi cycle, §2.6b). Không để nó nới risk/leverage/cap,
không để nó bypass PM/RiskGuard.

**Không** implement trong ask này. Thứ tự trước khi code: (1) sửa điểm chèn §7.4; (2) sửa contract
`metric_name` (BL-9, code của delivery, sửa riêng); (3) chốt `paths`/`temperature`/`max_batch_rows`
qua `KronosSettings` có validation; (4) build cache **sau khi** có per-path seeding (§5.5, BL-10).
Rồi chờ user quyết BL-1/BL-2/BL-8.
