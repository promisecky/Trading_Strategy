export interface ValueGate {
  gate: string
  passed: boolean
  reason: string
}

export interface ValuePoolCard {
  code: string
  name?: string
  industry?: string
  gates: ValueGate[]
  price?: number | null
  prev_close?: number | null
  change_pct?: number | null
  pe_ttm?: number | null
  dy_pct?: number | null
  mcap_yi?: number | null
  roe_pct?: number | null
  bias?: number | null
  z?: number | null
  holding: boolean
  in_active_pool: boolean
}

export interface ValuePosition {
  shares: number
  cost: number
  buy_date: string
}

export interface ValueSellSignal {
  code: string
  rule: number
  reason: string
}

export interface ValueBuySignal {
  code: string
  kind: 'main' | 'side'
  bias: number
  reason: string
}

export interface ValueSignals {
  sells: ValueSellSignal[]
  buys: ValueBuySignal[]
}

export interface ValueMetrics {
  total_return_pct: number
  annual_return_pct: number
  sharpe: number
  max_drawdown_pct: number
  calmar: number
  win_rate_pct: number
  round_count: number
  trade_count: number
  trades_per_year: number
  utilization_pct: number
}

export interface ValueOverview {
  ready: boolean
  hint?: string
  last_date?: string
  pool_size?: number
  pool?: ValuePoolCard[]
  positions?: Record<string, ValuePosition>
  slots_used?: number
  max_slots?: number
  cooldown?: { code: string; until_idx: number }[]
  signals?: ValueSignals
  metrics?: ValueMetrics
  last_rebuild?: { date: string; codes: string[] } | null
}

export interface ValueTrade {
  date: string
  exec_date: string
  code: string
  name?: string
  action: string
  rule?: number
  kind?: string
  reason: string
  price: number
  shares: number
  amount: number
  pnl_pct?: number
}

export interface ValueHistoryPoint {
  date: string
  equity: number
  position_pct: number
  cash: number
}

export interface ValueBacktest {
  start: string
  end?: string | null
  idle_yield_pct: number
  metrics: ValueMetrics
  history: ValueHistoryPoint[]
  trades: ValueTrade[]
  pool_rebuilds: { date: string; codes: string[] }[]
}

export interface ValueIndicators {
  code: string
  dates: string[]
  pe: (number | null)[]
  dy: (number | null)[]
  bias: (number | null)[]
}

export interface ValuePoolResponse {
  rebuild_date: string | null
  cards: ValuePoolCard[]
  hl_ready: boolean
}

export interface ValueDataFlowStep {
  step: string
  text: string
}

export interface ValueScheduledTask {
  id: string
  label: string
  schedule: string
  purpose: string
  data_flow: ValueDataFlowStep[]
  next_run: string | null
  prev_run: string | null
}

export interface ValueStockTrades {
  code: string
  name?: string
  trades: ValueTrade[]
  round_count: number
  win_rate_pct: number
  total_return_pct: number
  holding: boolean
  equity: number
}

export interface ValueKlinePoint {
  date: string
  open: number
  close: number
  high?: number | null
  low?: number | null
}

export interface ValueKlineResponse {
  code: string
  name?: string
  kline: ValueKlinePoint[]
  ma120: (number | null)[]
  trades: ValueTrade[]
}
