import type { SignalResponse, EtfHistoryResponse, EtfInfo, RealtimeStatus, StatsResponse, SentimentOverview, SentimentRefreshResult, EtfRefreshResult, CalendarDays, CalendarRefreshResult, ResonanceOverview, ResonanceDayDetail, TradesResponse, DataStatus, DataSettings, JobState, StartJobRequest, StartJobResponse, PortfolioBacktestResponse, RealtimeTurnoverResponse, ScheduledTaskInfo, ValueOverview, ValueBacktest, ValuePoolResponse, ValueKlineResponse, ValueIndicators, ValueScheduledTask, ValueStockTrades } from './types'

const BASE = `${__APP_BASE__}/api`

// ETF 因子链(交易所官方成交额)稳定数据起点: 展示范围对齐该日期, 避免无信号空白区
export const ETF_DATA_START = '2022-01-01'

async function parseError(res: Response): Promise<Error> {
  let msg = `API error: ${res.status}`
  try {
    const body = await res.json()
    if (body && typeof body.detail === 'string') msg = body.detail
  } catch {
    /* 忽略非 JSON 响应 */
  }
  return new Error(msg)
}

// 请求超时兜底: 后端未就绪/接口慢时不让页面无限挂起, 由 React Query 重试恢复
const DEFAULT_TIMEOUT_MS = 20_000

async function request<T>(path: string, init?: RequestInit, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(`${BASE}${path}`, { ...init, signal: ctrl.signal })
    if (!res.ok) throw await parseError(res)
    return res.json()
  } finally {
    clearTimeout(timer)
  }
}

async function get<T>(path: string, timeoutMs?: number): Promise<T> {
  return request<T>(path, undefined, timeoutMs)
}

async function post<T>(path: string, body?: unknown, timeoutMs?: number): Promise<T> {
  const hasBody = body !== undefined
  return request<T>(
    path,
    {
      method: 'POST',
      headers: hasBody ? { 'Content-Type': 'application/json' } : undefined,
      body: hasBody ? JSON.stringify(body) : undefined,
    },
    timeoutMs,
  )
}

export function fetchSignalsToday(): Promise<SignalResponse> {
  return get('/signals/today')
}

export function fetchSignalsByDate(date: string): Promise<SignalResponse> {
  return get(`/signals/${date}`)
}

export function fetchEtfHistory(code: string, days = 60, since?: string): Promise<EtfHistoryResponse> {
  const q = new URLSearchParams({ days: String(days) })
  if (since) q.set('since', since)
  return get(`/etf/${code}/history?${q}`)
}

export function fetchEtfList(): Promise<EtfInfo[]> {
  return get('/etf/list')
}

export function refreshEtf(): Promise<EtfRefreshResult> {
  return post('/etf/refresh')
}

export function fetchRealtimeStatus(): Promise<RealtimeStatus> {
  return get('/realtime/status')
}

export function fetchRealtimeTurnover(): Promise<RealtimeTurnoverResponse> {
  return get('/realtime/turnover')
}

export function fetchStats(): Promise<StatsResponse> {
  return get('/stats')
}

export function fetchMarketSentiment(since?: string): Promise<SentimentOverview> {
  const q = new URLSearchParams()
  if (since) q.set('since', since)
  return get(q.size ? `/sentiment/overview?${q}` : '/sentiment/overview')
}

export function refreshSentiment(): Promise<SentimentRefreshResult> {
  return post('/sentiment/refresh')
}

export function fetchResonance(code = '510300', since?: string): Promise<ResonanceOverview> {
  const q = new URLSearchParams({ code })
  if (since) q.set('since', since)
  return get(`/resonance/overview?${q}`)
}

export function fetchResonanceDay(code: string, date: string): Promise<ResonanceDayDetail> {
  return get(`/resonance/day?code=${code}&date=${date}`)
}

export type AlgoVersion = 'stable' | 'beta' | 'band'

export function fetchResonanceTrades(code = '510300', version: AlgoVersion = 'stable'): Promise<TradesResponse> {
  return get(`/resonance/trades?code=${code}&version=${version}`)
}

export function fetchStrategyVersions(): Promise<Record<string, AlgoVersion[]>> {
  return get('/resonance/trades/versions')
}

export function fetchCalendarDays(year: number): Promise<CalendarDays> {
  return get(`/calendar/days?year=${year}`)
}

export function refreshCalendar(): Promise<CalendarRefreshResult> {
  return post('/calendar/refresh')
}

export function fetchDataStatus(): Promise<DataStatus> {
  return get('/data/status')
}

export function fetchScheduledTasks(): Promise<ScheduledTaskInfo[]> {
  return get('/data/scheduled')
}

export function fetchPortfolioBacktest(codes?: string[]): Promise<PortfolioBacktestResponse> {
  const qs = codes?.length ? `?codes=${codes.join(',')}` : ''
  return get(`/portfolio/backtest${qs}`, 60_000)
}

export function fetchDataJobs(): Promise<JobState[]> {
  return get('/data/jobs')
}

export function fetchDataSettings(): Promise<DataSettings> {
  return get('/data/settings')
}

export function updateDataSettings(body: DataSettings): Promise<DataSettings> {
  return request<DataSettings>('/data/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function startDataJob(req: StartJobRequest): Promise<StartJobResponse> {
  return post('/data/jobs', req)
}

// ---- 价值回归(个股策略, 与 ETF 体系隔离) ----
export function fetchValueOverview(): Promise<ValueOverview> {
  return get('/value/overview', 90_000)
}

export function fetchValueBacktest(start = '2018-01-01', end?: string): Promise<ValueBacktest> {
  const q = end ? `&end=${end}` : ''
  return get(`/value/backtest?start=${start}${q}`, 90_000)
}

export function fetchValuePool(): Promise<ValuePoolResponse> {
  return get('/value/pool', 90_000)
}

export function fetchValueKline(code: string): Promise<ValueKlineResponse> {
  return get(`/value/kline?code=${code}`, 60_000)
}

export function fetchValueIndicators(code: string): Promise<ValueIndicators> {
  return get(`/value/indicators?code=${code}`, 60_000)
}

export function fetchValueScheduled(): Promise<ValueScheduledTask[]> {
  return get('/value/scheduled', 30_000)
}

export function fetchValueStockTrades(code: string): Promise<ValueStockTrades> {
  return get(`/value/stock_trades?code=${code}`, 90_000)
}
