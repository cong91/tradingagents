// M1 "P&L overview" screen: dashboard, KPI tiles, portfolio, performance, activity.

export const vi = {
  cannotReadData: (code: string) => `Không đọc được dữ liệu (${code})`,
  staleNote: (code: string, message: string) =>
    `Lần poll gần nhất lỗi (${code}) — đang hiển thị dữ liệu cũ: ${message}`,
  noDecisionsTitle: "Chưa có quyết định nào trong nhật ký",
  noDecisionsHint:
    "Lịch sử quyết định được ghi lại khi một phiên phân tích chạy xong. Hãy chạy phân tích để bắt đầu.",
  noDecisionsAction: "Chạy phân tích trong Hoạt động bot",
  noResolvedTitle: "Chưa có quyết định nào đã chốt để vẽ biểu đồ",
  noResolvedHint:
    "Một quyết định chỉ có alpha sau thời gian nắm giữ. Các quyết định đang chờ sẽ về đây khi được chốt.",
  noResolvedAction: "Xem quyết định đang chờ ở Lịch sử & Audit",
  computedOn: (shown: number, total: number) =>
    total > shown
      ? `Số liệu tính trên ${shown} quyết định gần nhất (tổng ${total}).`
      : `Số liệu tính trên ${shown} quyết định.`,
  skippedLines: (n: number) => `Cảnh báo: ${n} dòng log hỏng đã bị bỏ qua.`,
  noDataYet: "Chưa có dữ liệu",
  updatedAt: (time: string) => `Cập nhật lúc ${time}`,
  haltedToday: "Bot đã dừng đặt lệnh mới hôm nay (bộ bảo vệ rủi ro chặn).",
  viewAudit: "Xem audit",
  pendingApprovals: (n: number) => `${n} lệnh đang chờ duyệt.`,
  openApprovals: "Mở màn Duyệt lệnh",
  decisionPerformanceTitle: "Hiệu suất quyết định",
  decisionPerformanceDescription:
    "Alpha của các quyết định trong decision log, đơn vị điểm % — lãi luôn kèm dấu +, lỗ luôn kèm dấu −.",
  recentOrdersTitle: "Lệnh thực thi gần đây",
  recentOrdersDescription:
    "Spot mua/bán hiển thị ước tính dòng tiền; Futures hiển thị notional và margin, không phải tiền vào hoặc lãi/lỗ đã chốt.",
  outcome: { profit: "Lãi", loss: "Lỗ", flat: "Hoà" },
  kpi: {
    cumulativeAlpha: "Alpha tích luỹ",
    cumulativeAlphaAria: (value: string, outcome: string) =>
      `Alpha tích luỹ ${value} phần trăm — ${outcome}`,
    meanAlpha: "Alpha trung bình mỗi quyết định",
    onResolved: (n: number) => `trên ${n} quyết định đã chốt`,
    winRate: "Tỷ lệ thắng",
    directionalHits: (wins: number, total: number) =>
      `${wins}/${total} quyết định đi đúng hướng`,
    noDirectional: "chưa có quyết định có hướng để đánh giá",
    decisions: "Quyết định",
    pendingReview: (pending: number, review: number) => `${pending} chờ chốt · ${review} REVIEW`,
  },
  chartAria: "Biểu đồ alpha tích luỹ theo ngày, đơn vị phần trăm",
  chart: {
    legend: "Alpha tích luỹ (%)",
    tooltipDate: "Ngày",
    tooltipValue: "Giá trị",
    tooltipHint: "Di chuột trên biểu đồ để xem từng ngày",
  },
  orders: {
    emptyTitle: "Chưa có lệnh nào được thực thi",
    emptyHint:
      "Lệnh xuất hiện sau khi vòng quét tạo kế hoạch và kế hoạch đó được duyệt ở trang Duyệt lệnh.",
    time: "Thời gian",
    pair: "Cặp",
    order: "Lệnh",
    quantityPrice: "Khối lượng / Giá",
    flowNotional: "Spot dòng tiền / Futures notional",
    orderId: "Order ID",
  },
  unknownCurrency: "đơn vị chưa rõ",
  portfolio: {
    title: "Danh mục",
    description:
      "Snapshot danh mục khi khả dụng; P&L Spot là phép tính tham khảo từ audit log, chưa xác nhận khớp lệnh thực tế.",
    loading: "Đang tải danh mục…",
    missingTitle: "Chưa có dữ liệu danh mục",
    missingBody:
      "Chưa có dữ liệu danh mục — tính năng này đang được xây dựng phía máy chủ và sẽ tự hiển thị khi sẵn sàng. Đây không phải lỗi của bạn.",
    cannotRead: (code: string) => `Không đọc được danh mục (${code})`,
    unknownBasis: "Vị thế mở trước khi audit log bắt đầu — giá vốn không xác định",
    futuresPositions: "Vị thế Futures",
    futuresUnavailable:
      "Chưa đọc snapshot Futures. Snapshot chỉ được đọc khi chế độ giao dịch live đang bật.",
    futuresError: (error: string) =>
      `Không đọc được tài khoản Futures: ${error}. Dữ liệu này không phải danh mục rỗng.`,
    unknownErrorLower: "lỗi không xác định",
    accountUnified: "Tài khoản Unified (có gồm Futures)",
    accountTrading: "Tài khoản giao dịch OKX (equity quy đổi USD)",
    accountFutures: "Tài khoản Futures",
    walletBalance: "Số dư ví",
    equity: "Equity",
    available: "Khả dụng",
    unrealizedPnl: "Lãi/lỗ chưa chốt",
    futuresCaption: "Các vị thế Futures do sàn báo cáo",
    contract: "Hợp đồng",
    side: "Hướng",
    contractsQty: "Contracts / qty",
    entry: "Entry",
    mark: "Mark",
    notional: "Notional",
    margin: "Margin",
    leverage: "Đòn bẩy",
    heldFees: "Giữ / phí",
    noFuturesPositions: "Sàn không báo vị thế Futures đang mở.",
    futuresUnconfirmed: "Chưa thể xác nhận có vị thế Futures đang mở hay không.",
    futuresStale:
      "Dữ liệu Futures đã cũ hoặc chưa xác minh được thời điểm đọc; không thể xác nhận trạng thái Futures hiện tại.",
    futuresNoObservedAt:
      "Không xác định được thời điểm đọc vị thế Futures; không thể xác nhận dữ liệu hiện tại.",
    futuresIncomplete:
      "Danh sách vị thế Futures có thể không đầy đủ; không thể xác nhận toàn bộ vị thế đang mở.",
    heldFeesNote:
      "Cột “Giữ / phí” là chi phí ước tính để tham khảo — phí khứ hồi và funding không phải số tiền đã bị trừ khỏi số dư trong snapshot này.",
    snapshotStale: "Snapshot đã cũ; trạng thái Spot và Futures hiện tại chưa được xác nhận.",
    riskGuardUnknown: "Không xác định được trạng thái dừng giao dịch của bot.",
    spotUnavailable: "Không xác định được số dư và vị thế Spot.",
    spotStale: "Snapshot Spot đã cũ; không thể xác nhận trạng thái Spot hiện tại.",
    notionalNote:
      "Notional là giá trị danh nghĩa, margin là tài sản ký quỹ; cả hai không phải tiền lãi hay dòng tiền đã nhận.",
    noSpotTitle: "Chưa có dữ liệu Spot",
    noSpotBody:
      "Chưa có số dư Spot hoặc giao dịch Spot trong audit log. Phần Futures bên dưới được đọc độc lập từ sàn.",
    spotCash: "Tiền mặt Spot",
    spotEquity: "Giá trị Spot ước tính",
    spotRealizedTotal: "Spot audit đã chốt (tham khảo)",
    spotRealizedToday: "Spot audit hôm nay (tham khảo)",
    spotCaption: "Vị thế Spot đang mở theo từng mã",
    ticker: "Mã",
    quantity: "Khối lượng",
    costBasis: "Giá vốn audit",
    marketPrice: "Giá thị trường",
    value: "Giá trị",
    unrealizedEst: "Lãi/lỗ theo audit",
    closedTrades: (n: number) => `${n} lượt đóng được ghi nhận trong audit log`,
    unknownBasisCloses: (n: number) => ` · ${n} lượt đóng không rõ giá vốn (loại khỏi P&L)`,
    consecutiveLosses: (n: number) => ` · ${n} lỗ liên tiếp (cảnh báo bộ bảo vệ rủi ro)`,
    todayNote:
      "P&L Spot là phép tính từ audit log, không xác nhận khớp lệnh hoặc P&L thực tế của tài khoản; đơn vị tiền tệ chưa được xác minh. Hôm nay tính theo ngày UTC và khác con số bộ bảo vệ rủi ro dùng (cộng thêm lãi/lỗ tạm tính của vị thế mở trong ngày). Giá vốn Spot cũng chỉ là tham khảo từ audit log, không xác minh theo sàn.",
    footer: (time: string) =>
      `Cập nhật ${time} · thời điểm tạo phản hồi HTTP, không phải thời điểm dữ liệu tại sàn; tự làm mới mỗi 30 giây.`,
  },
  performance: {
    title: "Hiệu suất giao dịch",
    description: "Vị thế từ sàn, khớp lệnh Futures và nhật ký sự kiện.",
    closedStatsTitle: "Thống kê giao dịch đã đóng",
    closedStatsBody:
      "— Chưa đủ dữ liệu đối soát để xác nhận số lệnh đóng, tỷ lệ thắng và kỳ vọng.",
    historyLoading: "Đang tải lịch sử khớp lệnh…",
    historyError: (code: string, message: string) =>
      `Không đọc được lịch sử khớp lệnh (${code}): ${message}`,
    historyStale: (time: string) => `Lịch sử cũ; lần đọc gần nhất lúc ${time}.`,
    eventsLoading: "Đang tải nhật ký…",
    eventsError: (code: string, message: string) => `Không đọc được nhật ký (${code}): ${message}`,
    eventsStale: (time: string) => `Nhật ký cũ · ${time}`,
  },
  activity: {
    title: "Hoạt động hiện tại",
    description:
      "Theo dõi riêng vòng quét, job hằng ngày và phiên phân tích; không thay đổi trạng thái giao dịch.",
    status: {
      running: "Đang chạy",
      queued: "Đang chờ",
      completed: "Hoàn tất",
      failed: "Thất bại",
      cancelled: "Đã dừng",
      interrupted: "Bị gián đoạn",
      timeout: "Hết thời gian",
      idle: "Nhàn rỗi",
    } as Record<string, string>,
    ticker: (ticker: string) => `Mã ${ticker}`,
    agent: (node: string) => `Agent ${node}`,
    stage: (stage: string) => `Bước ${stage}`,
    executed: (done: number, total: number) => `${done}/${total} lệnh đã thực thi`,
    loading: "Đang tải hoạt động…",
    cannotRead: (message: string) => `Không đọc được trạng thái hoạt động: ${message}`,
    staleSince: (time: string | null, message: string) =>
      `Poll gần nhất lỗi; đang giữ dữ liệu${time ? ` từ ${time}` : " chưa xác nhận thời điểm cập nhật"}: ${message}`,
    dailyScan: "Quét hằng ngày",
    results: (done: number, total: number) => `${done}/${total} kết quả`,
    screened: (n: number) => `Đã sàng ${n} mã`,
    analysisRun: "Phiên phân tích",
    runReadError: (status: number) => `Không đọc được run đang hoạt động (HTTP ${status}).`,
    autoloopReadError: "Không đọc được trạng thái vòng tự động.",
    dailyReadError: "Không đọc được trạng thái job hằng ngày.",
    runFallbackError: "Không đọc được phiên phân tích đang hoạt động.",
    backendUnreachable: "Không kết nối được máy chủ TradingAgents.",
  },
};

export const en: typeof vi = {
  cannotReadData: (code) => `Could not read data (${code})`,
  staleNote: (code, message) => `Latest poll failed (${code}) — showing stale data: ${message}`,
  noDecisionsTitle: "No decisions in the log yet",
  noDecisionsHint:
    "Decision history is recorded when an analysis run finishes. Run an analysis to get started.",
  noDecisionsAction: "Run an analysis in Bot activity",
  noResolvedTitle: "No resolved decisions to chart yet",
  noResolvedHint:
    "A decision only has alpha after its holding period. Pending decisions will appear here once resolved.",
  noResolvedAction: "See pending decisions in History & Audit",
  computedOn: (shown, total) =>
    total > shown
      ? `Computed on the latest ${shown} decisions (${total} total).`
      : `Computed on ${shown} decisions.`,
  skippedLines: (n) => `Warning: ${n} corrupt log lines were skipped.`,
  noDataYet: "No data yet",
  updatedAt: (time) => `Updated at ${time}`,
  haltedToday: "The bot has stopped placing new orders today (blocked by its risk guard).",
  viewAudit: "View audit",
  pendingApprovals: (n) => `${n} ${n === 1 ? "order is" : "orders are"} awaiting approval.`,
  openApprovals: "Open Order approvals",
  decisionPerformanceTitle: "Decision performance",
  decisionPerformanceDescription:
    "Alpha of decisions in the decision log, in % points — profits always carry a +, losses always a −.",
  recentOrdersTitle: "Recent executed orders",
  recentOrdersDescription:
    "Spot buys/sells show estimated cash flow; Futures show notional and margin, not cash received or realized P&L.",
  outcome: { profit: "Profit", loss: "Loss", flat: "Flat" },
  kpi: {
    cumulativeAlpha: "Cumulative alpha",
    cumulativeAlphaAria: (value, outcome) => `Cumulative alpha ${value} percent — ${outcome}`,
    meanAlpha: "Mean alpha per decision",
    onResolved: (n) => `across ${n} resolved decisions`,
    winRate: "Win rate",
    directionalHits: (wins, total) => `${wins}/${total} decisions moved in the right direction`,
    noDirectional: "no directional decisions to evaluate yet",
    decisions: "Decisions",
    pendingReview: (pending, review) => `${pending} pending · ${review} REVIEW`,
  },
  chartAria: "Chart of cumulative alpha by day, in percent",
  chart: {
    legend: "Cumulative alpha (%)",
    tooltipDate: "Date",
    tooltipValue: "Value",
    tooltipHint: "Hover the chart to see each day",
  },
  orders: {
    emptyTitle: "No orders executed yet",
    emptyHint:
      "Orders appear once a scan produces a plan and that plan is approved on Approvals.",
    time: "Time",
    pair: "Pair",
    order: "Order",
    quantityPrice: "Quantity / Price",
    flowNotional: "Spot cash flow / Futures notional",
    orderId: "Order ID",
  },
  unknownCurrency: "unknown unit",
  portfolio: {
    title: "Portfolio",
    description:
      "Portfolio snapshot when available; Spot P&L is a reference calculation from the audit log, not confirmed against actual fills.",
    loading: "Loading portfolio…",
    missingTitle: "No portfolio data yet",
    missingBody:
      "No portfolio data yet — this feature is still being built on the server and will appear automatically when ready. This is not an error on your side.",
    cannotRead: (code) => `Could not read the portfolio (${code})`,
    unknownBasis: "Position opened before the audit log started — cost basis unknown",
    futuresPositions: "Futures positions",
    futuresUnavailable:
      "Futures snapshot not read. It is only read while live trading mode is enabled.",
    futuresError: (error) =>
      `Could not read the Futures account: ${error}. This does not mean the portfolio is empty.`,
    unknownErrorLower: "unknown error",
    accountUnified: "Unified account (includes Futures)",
    accountTrading: "OKX trading account (equity in USD)",
    accountFutures: "Futures account",
    walletBalance: "Wallet balance",
    equity: "Equity",
    available: "Available",
    unrealizedPnl: "Unrealized P&L",
    futuresCaption: "Futures positions reported by the exchange",
    contract: "Contract",
    side: "Side",
    contractsQty: "Contracts / qty",
    entry: "Entry",
    mark: "Mark",
    notional: "Notional",
    margin: "Margin",
    leverage: "Leverage",
    heldFees: "Held / fees",
    noFuturesPositions: "The exchange reports no open Futures positions.",
    futuresUnconfirmed: "Cannot confirm yet whether any Futures positions are open.",
    futuresStale:
      "Futures data is stale or its read time is unverified; the current Futures state cannot be confirmed.",
    futuresNoObservedAt:
      "The Futures positions read time is unknown; the current data cannot be confirmed.",
    futuresIncomplete:
      "The Futures position list may be incomplete; not every open position can be confirmed.",
    heldFeesNote:
      "The “Held / fees” column is an estimated cost for reference — round-trip fees and funding are not amounts already deducted from the balance in this snapshot.",
    snapshotStale: "Snapshot is stale; the current Spot and Futures state is unconfirmed.",
    riskGuardUnknown: "Could not determine the bot's trading-halt status.",
    spotUnavailable: "Spot balance and positions could not be determined.",
    spotStale: "Spot snapshot is stale; the current Spot state cannot be confirmed.",
    notionalNote:
      "Notional is face value and margin is posted collateral; neither is profit or cash received.",
    noSpotTitle: "No Spot data yet",
    noSpotBody:
      "No Spot balance or Spot trades in the audit log yet. The Futures section below is read independently from the exchange.",
    spotCash: "Spot cash",
    spotEquity: "Est. Spot value",
    spotRealizedTotal: "Spot audit realized (reference)",
    spotRealizedToday: "Spot audit today (reference)",
    spotCaption: "Open Spot positions by ticker",
    ticker: "Ticker",
    quantity: "Quantity",
    costBasis: "Audit cost basis",
    marketPrice: "Market price",
    value: "Value",
    unrealizedEst: "P&L per audit",
    closedTrades: (n) => `${n} ${n === 1 ? "close" : "closes"} recorded in the audit log`,
    unknownBasisCloses: (n) => ` · ${n} closes with unknown cost basis (excluded from P&L)`,
    consecutiveLosses: (n) => ` · ${n} consecutive losses (risk-guard warning)`,
    todayNote:
      "Spot P&L is computed from the audit log; it does not confirm fills or the account's actual P&L, and the currency unit is unverified. “Today” uses the UTC day and differs from the risk-guard figure, which also adds today's unrealized P&L of open positions. Spot cost basis is likewise a reference from the audit log, not verified against the exchange.",
    footer: (time) =>
      `Updated ${time} · time the HTTP response was built, not when the exchange data was read; refreshes every 30 seconds.`,
  },
  performance: {
    title: "Trading performance",
    description: "Exchange positions, Futures fills and the event log.",
    closedStatsTitle: "Closed-trade statistics",
    closedStatsBody:
      "— Not enough reconciled data yet to confirm closed trades, win rate and expectancy.",
    historyLoading: "Loading fill history…",
    historyError: (code, message) => `Could not read fill history (${code}): ${message}`,
    historyStale: (time) => `History is stale; last read at ${time}.`,
    eventsLoading: "Loading event log…",
    eventsError: (code, message) => `Could not read the event log (${code}): ${message}`,
    eventsStale: (time) => `Stale log · ${time}`,
  },
  activity: {
    title: "Current activity",
    description:
      "Tracks the scan loop, the daily job and analysis runs separately; does not change any trading state.",
    status: {
      running: "Running",
      queued: "Queued",
      completed: "Completed",
      failed: "Failed",
      cancelled: "Stopped",
      interrupted: "Interrupted",
      timeout: "Timed out",
      idle: "Idle",
    },
    ticker: (ticker) => `Ticker ${ticker}`,
    agent: (node) => `Agent ${node}`,
    stage: (stage) => `Stage ${stage}`,
    executed: (done, total) => `${done}/${total} orders executed`,
    loading: "Loading activity…",
    cannotRead: (message) => `Could not read activity status: ${message}`,
    staleSince: (time, message) =>
      `Latest poll failed; keeping data${time ? ` from ${time}` : " with an unconfirmed update time"}: ${message}`,
    dailyScan: "Daily scan",
    results: (done, total) => `${done}/${total} results`,
    screened: (n) => `Screened ${n} tickers`,
    analysisRun: "Analysis run",
    runReadError: (status) => `Could not read the active run (HTTP ${status}).`,
    autoloopReadError: "Could not read autoloop status.",
    dailyReadError: "Could not read daily job status.",
    runFallbackError: "Could not read the active analysis run.",
    backendUnreachable: "Cannot reach the TradingAgents server.",
  },
};
