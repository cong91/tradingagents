# TradingAgents Control Panel

Giao diện quản trị cho fork TradingAgents: theo dõi các agent từ phân tích → tranh luận → quyết định, quản lý watchlist, duyệt lệnh thật, và xem lịch sử/audit — thay cho CLI tương tác.

Kiến trúc: **Next.js 16 (App Router) + Tailwind v4 + lightweight-charts** ở thư mục này, gọi **backend FastAPI** tại `../server/` (engine LangGraph/ccxt giữ nguyên trong `../tradingagents/`). Toàn bộ endpoint được quy định trong `../docs/ui-api-contract.md` — frontend không tự chế API ngoài hợp đồng.

## 6 màn hình

| Route | Màn hình | Nguồn dữ liệu |
| --- | --- | --- |
| `/` | Tổng quan lợi nhuận (P&L, alpha, lệnh gần đây) | `/api/health`, `/api/history`, `/api/audit` |
| `/pipeline` | Agent Pipeline — chạy phiên + stream SSE | `/api/runs` |
| `/scanner` | Tìm cặp giao dịch — watchlist + quét | `/api/watchlist`, `/api/daily` |
| `/approvals` | Duyệt lệnh — cổng duyệt trước khi vào lệnh thật | `/api/approvals` |
| `/settings` | Cấu hình LLM / rủi ro / sàn & lịch | `/api/settings` |
| `/audit` | Lịch sử quyết định, audit log, backtest | `/api/audit`, `/api/history`, `/api/backtest` |

Mode bar toàn cục hiển thị **SANDBOX/LIVE** đọc từ `/api/settings` — LIVE (lệnh thật) luôn hiển thị bằng chữ + biểu tượng, không dùng màu làm tín hiệu duy nhất.

## Chạy

Từ thư mục gốc repo:

```powershell
# 1. Backend (lần đầu: .venv\Scripts\python.exe -m pip install -e ".[web]")
.venv\Scripts\python.exe -m uvicorn server.main:app --port 8000

# 2. Frontend (cửa sổ khác)
cd web
npm install
npm run dev   # http://localhost:3000
```

Next.js rewrite mọi `/api/*` về `http://127.0.0.1:8000` (xem `next.config.ts`), nên không cần CORS. Pipeline có chế độ **Mock** phát lại sự kiện mẫu — chạy được UI đầy đủ mà không cần API key LLM.

## Phát triển

- **Typecheck**: `npx tsc --noEmit` — strict, không dùng `any`.
- **Lint**: `npm run lint`.
- **Build**: `npm run build` — toàn bộ route prerender tĩnh.
- Tầng lỗi API dùng chung ở `lib/api.ts` (envelope `{error:{code,message,details}}` của hợp đồng §0.1 + `{detail}` của FastAPI); các màn giữ lớp lỗi cục bộ mỏng kế thừa/wrap `ApiRequestError`.
- State loading/empty/error/**stale** là hạng nhất ở mọi màn — khi poll lỗi, giữ dữ liệu cũ và gắn badge "Dữ liệu cũ", không nhảy trắng.

## Lưu ý vận hành

- Backend chỉ lắng nghe `127.0.0.1` — mở ra LAN phải thêm token trước (hợp đồng §0, §10.12).
- `AGENTS.md` / `CLAUDE.md` trong thư mục này do Next.js 16 sinh tự động, giữ nguyên.
- Đây là công cụ nghiên cứu — không phải lời khuyên tài chính.
