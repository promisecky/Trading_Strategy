import { useMemo, useState } from 'react'
import { useQuery, useQueries } from '@tanstack/react-query'
import {
  fetchValueOverview, fetchValueBacktest, fetchValuePool, fetchValueKline, fetchValueIndicators,
  fetchValueScheduled, fetchValueStockTrades, fetchDataJobs, startDataJob,
} from '../api/client'
import type { ValueScheduledTask } from '../api/types'
import ValueEquityChart from '../components/value/ValueEquityChart'
import ValuePoolCardView from '../components/value/ValuePoolCard'
import ValueStockKline from '../components/value/ValueStockKline'
import ValueIndicatorsChart from '../components/value/ValueIndicatorsChart'

const RULE_LABEL: Record<number, string> = { 1: '①止损-12%', 2: '②止盈+10%', 3: '③bias≥1.08' }

/** 数据管理栏: 手动任务定义(与后端 job_registry 对应) */
const MANUAL_JOBS = [
  { task: 'value_fetch_data', label: '拉取个股数据', desc: 'K线/分红/ROE/PE 全量与增量, 断点续传; 每日 16:30 自动增量' },
  { task: 'value_rebuild_pool', label: '重建股票池', desc: '以最新数据重跑七道门; 每季度首个交易日 17:00 自动执行' },
  { task: 'value_upgrade_kline', label: '升级K线(补高低价)', desc: '一次性任务: 为存量个股重拉含高低价的蜡烛图数据' },
]

const STEP_BADGE: Record<string, { label: string; cls: string }> = {
  fetch: { label: '拉取', cls: 'text-cyan-400 bg-cyan-400/10' },
  derive: { label: '加工', cls: 'text-violet-400 bg-violet-400/10' },
  write: { label: '写库', cls: 'text-emerald-400 bg-emerald-400/10' },
  read: { label: '读库', cls: 'text-blue-400 bg-blue-400/10' },
}

function fmtCountdown(ms: number): string {
  if (ms <= 0) return '即将运行'
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
}

function StatCard({ label, value, sub, tone }: {
  label: string
  value: string
  sub?: string
  tone?: 'green' | 'red' | 'gray'
}) {
  const color = tone === 'green' ? 'text-green-400' : tone === 'red' ? 'text-red-400' : 'text-white'
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      <div className={`text-lg font-bold ${color}`}>{value}</div>
      {sub && <div className="text-[11px] text-gray-500 mt-0.5">{sub}</div>}
    </div>
  )
}

export default function Value() {
  const [tab, setTab] = useState<'pool' | 'backtest' | 'kline' | 'admin'>('pool')
  const [klineCodes, setKlineCodes] = useState<string[]>([])
  const [range, setRange] = useState<{ start?: string; end?: string }>({})
  const btStart = range.start && range.start >= '2018-01-01' ? range.start : '2018-01-01'
  const overview = useQuery({ queryKey: ['valueOverview'], queryFn: fetchValueOverview, staleTime: 5 * 60_000, refetchOnWindowFocus: false })
  const backtest = useQuery({
    queryKey: ['valueBacktest', btStart, range.end ?? ''],
    queryFn: () => fetchValueBacktest(btStart, range.end),
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  })
  const poolQ = useQuery({ queryKey: ['valuePool'], queryFn: fetchValuePool, staleTime: 5 * 60_000, refetchOnWindowFocus: false })
  const jobsQ = useQuery({ queryKey: ['dataJobs'], queryFn: fetchDataJobs, refetchInterval: tab === 'admin' ? 5000 : false })
  const scheduledQ = useQuery({
    queryKey: ['valueScheduled'],
    queryFn: fetchValueScheduled,
    staleTime: 60_000,
    refetchInterval: tab === 'admin' ? 30_000 : false,
  })

  // 时间范围预设: 近 N 年(以今日为终点基准)
  const applyPreset = (years: number | null) => {
    if (!years) { setRange({}); return }
    const end = new Date()
    const start = new Date()
    start.setFullYear(end.getFullYear() - years)
    setRange({ start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) })
  }

  const ov = overview.data
  const runJob = (task: string) => startDataJob({ task })

  const cards = poolQ.data?.cards ?? []
  const heldCodes = Object.keys(ov?.positions ?? {})
  // 代码→名称映射(池卡片 + 持仓增强), 供勾选栏/持仓条展示
  const nameOf = (code: string) =>
    cards.find(c => c.code === code)?.name ?? (ov?.positions ?? {})[code]?.name ?? ''
  // 信号日净值映射: 交易仓位占比 = 归一金额 ÷ 信号日净值
  const equityByDate = useMemo(() => {
    const m = new Map<string, number>()
    for (const h of backtest.data?.history ?? []) m.set(h.date, h.equity)
    return m
  }, [backtest.data])
  // K线栏候选 = 股票池 ∪ 当前持仓
  const klineCandidates = [...new Set([...cards.map(c => c.code), ...heldCodes])]

  const klineResults = useQueries({
    queries: klineCodes.map(code => ({
      queryKey: ['valueKline', code],
      queryFn: async () => {
        const [kline, ind, st] = await Promise.all([
          fetchValueKline(code),
          fetchValueIndicators(code),
          fetchValueStockTrades(code),
        ])
        return { kline, ind, st }
      },
      staleTime: 5 * 60_000,
      refetchOnWindowFocus: false,
    })),
  })

  const showKline = (code: string) => {
    setKlineCodes(prev => (prev.includes(code) ? prev : [...prev, code]))
    setTab('kline')
  }
  const toggleKline = (code: string) => {
    setKlineCodes(prev => (prev.includes(code) ? prev.filter(c => c !== code) : [...prev, code]))
  }

  if (overview.isLoading) return <div className="text-gray-400 text-center py-20">价值回归数据加载中...</div>

  if (!ov?.ready) {
    return (
      <div className="space-y-4">
        <h2 className="text-lg font-bold text-white">价值回归 · 白马恐慌折扣店</h2>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center text-sm text-gray-400 space-y-4">
          <p>{ov?.hint ?? '暂无数据'}</p>
          <div className="flex justify-center gap-3">
            <button onClick={() => runJob('value_fetch_data')}
              className="px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-sm">
              拉取个股数据(后台)
            </button>
            <button onClick={() => runJob('value_rebuild_pool')}
              className="px-4 py-2 rounded bg-gray-800 hover:bg-gray-700 text-gray-200 text-sm border border-gray-700">
              重建股票池
            </button>
          </div>
          <p className="text-[11px] text-gray-600">
            策略: 季度首日七道门选白马 → 恐慌深坑(bias≤0.88 且企稳)买入 → +10%/-12%/bias≥1.08 卖出 → 5 槽等权, 闲钱货币化
          </p>
        </div>
      </div>
    )
  }

  const sig = ov.signals!
  const positions = Object.entries(ov.positions ?? {})

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <div>
          <h2 className="text-lg font-bold text-white">价值回归 · 白马恐慌折扣店</h2>
          <p className="text-xs text-gray-500 mt-1">
            独立个股策略, 与 ETF 体系完全隔离 · 数据截至 {ov.last_date} · 池重建 {ov.last_rebuild?.date ?? '-'}
          </p>
        </div>
        <div className="ml-auto flex gap-2">
          <button onClick={() => runJob('value_fetch_data')}
            className="px-3 py-1.5 rounded text-sm bg-gray-800 text-gray-200 border border-gray-700 hover:border-gray-500">
            拉取数据
          </button>
          <button onClick={() => runJob('value_rebuild_pool')}
            className="px-3 py-1.5 rounded text-sm bg-gray-800 text-gray-200 border border-gray-700 hover:border-gray-500">
            重建池
          </button>
        </div>
      </div>

      <div className="flex gap-1 border-b border-gray-800">
        {(
          [
            ['pool', `股票池 (${cards.length || ov.pool_size || 0})`],
            ['backtest', '组合回测'],
            ['kline', `个股K线${klineCodes.length ? ` (${klineCodes.length})` : ''}`],
            ['admin', '数据管理与定时'],
          ] as const
        ).map(([k, label]) => (
          <button key={k} onClick={() => setTab(k)}
            className={`px-4 py-2 text-sm transition-colors ${
              tab === k ? 'text-white border-b-2 border-sky-500' : 'text-gray-500 hover:text-gray-300'
            }`}>
            {label}
          </button>
        ))}
        <div className="ml-auto self-center text-[11px] text-gray-600 pr-1">
          今日买 {sig.buys.length} · 卖 {sig.sells.length} · 槽位 {ov.slots_used}/{ov.max_slots}
        </div>
      </div>

      {/* ---------------- 时间范围设置栏(作用于回测区间 + K线/指标图缩放) ---------------- */}
      {(tab === 'backtest' || tab === 'kline') && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg px-3 py-2 flex items-center gap-3 flex-wrap text-xs">
          <span className="text-gray-500">时间范围:</span>
          {[['1年', 1], ['2年', 2], ['3年', 3], ['5年', 5]].map(([label, y]) => (
            <button key={label} onClick={() => applyPreset(y as number)}
              className="px-2 py-1 rounded bg-gray-800 text-gray-300 border border-gray-700 hover:border-gray-500">
              近{label}
            </button>
          ))}
          <button onClick={() => applyPreset(null)}
            className="px-2 py-1 rounded bg-gray-800 text-gray-300 border border-gray-700 hover:border-gray-500">
            全部
          </button>
          <span className="text-gray-600">|</span>
          <input type="date" value={range.start ?? ''} min="2018-01-01"
            onChange={e => setRange(r => ({ ...r, start: e.target.value || undefined }))}
            className="bg-gray-800 border border-gray-700 rounded px-1.5 py-0.5 text-gray-300 [color-scheme:dark]" />
          <span className="text-gray-600">→</span>
          <input type="date" value={range.end ?? ''}
            onChange={e => setRange(r => ({ ...r, end: e.target.value || undefined }))}
            className="bg-gray-800 border border-gray-700 rounded px-1.5 py-0.5 text-gray-300 [color-scheme:dark]" />
          <span className="text-[11px] text-gray-600 ml-auto">
            回测区间 {btStart} → {range.end ?? '最新'} · 指标口径始终为全量回测, 曲线/交易按区间展示 · 最早 2018(估值数据起点)
          </span>
        </div>
      )}

      {/* ---------------- 栏一: 股票池卡片(对标 ETF 流向分析) ---------------- */}
      {tab === 'pool' && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap text-xs text-gray-500">
            <span>季度首日(1/4/7/10月)七道门重建 · 点击卡片查看该股 K 线与买卖点</span>
            {!poolQ.data?.hl_ready && (
              <span className="text-amber-400/80">K线高低价升级抓取中, K线栏暂以折线显示</span>
            )}
          </div>
          {poolQ.isLoading ? (
            <div className="text-gray-500 text-center py-16">股票池指标加载中(首次约 30 秒)...</div>
          ) : cards.length === 0 ? (
            <div className="text-gray-500 text-center py-16">当前无入池标的 — 可点「重建池」刷新</div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-3">
              {cards.map(c => <ValuePoolCardView key={c.code} card={c} onShowKline={showKline} />)}
            </div>
          )}
        </div>
      )}

      {/* ---------------- 栏二: 组合回测(对标 ETF 组合回测) ---------------- */}
      {tab === 'backtest' && (
        backtest.isLoading ? (
          <div className="text-gray-500 text-center py-16">回测计算中(首次约 30 秒)...</div>
        ) : backtest.data ? (
          <div className="space-y-4">
            <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-3">
              <StatCard label="累计收益" value={`${backtest.data.metrics.total_return_pct > 0 ? '+' : ''}${backtest.data.metrics.total_return_pct}%`}
                tone={backtest.data.metrics.total_return_pct >= 0 ? 'green' : 'red'} sub={`${backtest.data.start} 起`} />
              <StatCard label="年化收益" value={`${backtest.data.metrics.annual_return_pct}%`}
                tone={backtest.data.metrics.annual_return_pct >= 0 ? 'green' : 'red'} />
              <StatCard label="最大回撤" value={`${backtest.data.metrics.max_drawdown_pct}%`} tone="red"
                sub={`夏普 ${backtest.data.metrics.sharpe} · 卡玛 ${backtest.data.metrics.calmar}`} />
              <StatCard label="胜率" value={`${backtest.data.metrics.win_rate_pct}%`}
                sub={`${backtest.data.metrics.round_count} 次往返`} />
              <StatCard label="资金利用率" value={`${backtest.data.metrics.utilization_pct}%`}
                sub={`${backtest.data.metrics.trade_count} 笔 · 年均 ${backtest.data.metrics.trades_per_year}`} />
            </div>

            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <div className="text-xs text-gray-500 mb-2">
                净值曲线({btStart} → {range.end ?? '最新'}) + 买卖点(▲绿买 ▼红卖, 点击三角看明细) ·{' '}
                {backtest.data.idle_fund?.mode === 'fund'
                  ? `闲钱 = ${backtest.data.idle_fund.name}(${backtest.data.idle_fund.code}) 实际日线收益, 缺失日按年化 ${backtest.data.idle_yield_pct}% 兜底`
                  : `闲钱年化 ${backtest.data.idle_yield_pct}%`}
                {' '}· 次日开盘成交 · 无前视
              </div>
              <ValueEquityChart history={backtest.data.history} trades={backtest.data.trades} />
            </div>

            {positions.length > 0 && (
              <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
                <div className="text-xs text-gray-500 mb-2">当前持仓 ({positions.length}/{ov.max_slots})</div>
                <div className="flex flex-wrap gap-2">
                  {positions.map(([code, p]) => {
                    const inPool = (ov.pool ?? []).some(pp => pp.code === code)
                    return (
                      <span key={code} className="px-2 py-1 rounded text-xs bg-gray-800 text-gray-300 border border-gray-700 cursor-pointer hover:border-gray-500"
                        onClick={() => showKline(code)}>
                        {code} {p.name ?? ''}
                        <span className="text-gray-500 ml-1">成本 {p.cost.toFixed(2)}</span>
                        {p.price != null && <span className="text-gray-500 ml-1">现 {p.price.toFixed(2)}</span>}
                        {p.pnl_pct != null && (
                          <span className={`ml-1 font-mono ${p.pnl_pct >= 0 ? 'text-red-400' : 'text-green-400'}`}>
                            {p.pnl_pct >= 0 ? '+' : ''}{p.pnl_pct}%
                          </span>
                        )}
                        {p.weight_pct != null && <span className="text-sky-400 ml-1">仓位 {p.weight_pct}%</span>}
                        <span className="text-gray-600 ml-1">@{p.buy_date} 起</span>
                        {!inPool && (
                          <span className="text-amber-400 ml-1" title="季度重建后已不在新池: 不强制卖, 继续按①止损/止盈/③bias≥1.08 三规则持有; 卖出后不再买回">
                            出池·持有至卖出规则
                          </span>
                        )}
                      </span>
                    )
                  })}
                </div>
              </div>
            )}

            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <div className="flex items-center justify-between mb-2">
                <div className="text-xs text-gray-500">交易记录留存（共 {backtest.data.trades.length} 条）</div>
                <div className="text-[11px] text-gray-600" title="回测以期初净值=1 归一化: 单票金额=信号日总资产÷5, 故仓位占比恒≈20%; 实际下单按自有等比例放大即可">
                  归一化回测(期初=1): 单票=信号日总资产÷5 · 仓位占比≈20%/笔
                </div>
              </div>
              <div className="overflow-x-auto max-h-[480px] overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-gray-900">
                    <tr className="text-gray-500 border-b border-gray-800">
                      <th className="text-left py-1.5 pr-3">成交日</th>
                      <th className="text-left py-1.5 pr-3">信号日</th>
                      <th className="text-left py-1.5 pr-3">标的</th>
                      <th className="text-left py-1.5 pr-3">操作</th>
                      <th className="text-right py-1.5 pr-3">价格</th>
                      <th className="text-right py-1.5 pr-3">仓位占比</th>
                      <th className="text-right py-1.5 pr-3">盈亏%</th>
                      <th className="text-left py-1.5">理由</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...backtest.data.trades].reverse().map((t, i) => {
                      const eq = equityByDate.get(t.date)
                      const weight = eq ? ((t.amount / eq) * 100).toFixed(1) : '-'
                      return (
                        <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                          <td className="py-1.5 pr-3 text-gray-400 font-mono">{t.exec_date}</td>
                          <td className="py-1.5 pr-3 text-gray-600 font-mono">{t.date}</td>
                          <td className="py-1.5 pr-3 text-gray-300">
                            <button className="hover:text-sky-400" onClick={() => showKline(t.code)}>
                              {t.code} {t.name ?? ''}
                            </button>
                          </td>
                          <td className={`py-1.5 pr-3 ${t.action === 'BUY' ? 'text-green-400' : 'text-red-400'}`}>
                            {t.action === 'BUY' ? (t.kind === 'side' ? '买·侧门' : '买·主信号') : `卖·${RULE_LABEL[t.rule ?? 0] ?? t.rule}`}
                          </td>
                          <td className="py-1.5 pr-3 text-right text-gray-400 font-mono">{t.price.toFixed(2)}</td>
                          <td className="py-1.5 pr-3 text-right text-gray-400 font-mono">{weight}%</td>
                          <td className={`py-1.5 pr-3 text-right font-mono ${t.pnl_pct != null ? (t.pnl_pct >= 0 ? 'text-red-400' : 'text-green-400') : 'text-gray-600'}`}>
                            {t.pnl_pct != null ? `${t.pnl_pct > 0 ? '+' : ''}${t.pnl_pct}` : '-'}
                          </td>
                          <td className="py-1.5 text-gray-500">{t.reason}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        ) : (
          <div className="text-red-400 text-center py-16">回测数据加载失败</div>
        )
      )}

      {/* ---------------- 栏三: 个股K线(对标 ETF 走势对比) ---------------- */}
      {tab === 'kline' && (
        <div className="space-y-4">
          <div className="flex items-center gap-3 flex-wrap text-xs">
            <button
              onClick={() => setKlineCodes(prev => [...new Set([...prev, ...heldCodes])])}
              disabled={heldCodes.length === 0}
              className="shrink-0 px-2.5 py-1 rounded bg-sky-600/20 text-sky-300 border border-sky-500/40 hover:bg-sky-600/30 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              一键选中持仓({heldCodes.length})
            </button>
            <span className="text-gray-500">勾选查看(股票池 ∪ 持仓):</span>
            {klineCandidates.map(code => {
              return (
                <label key={code} className="flex items-center gap-1.5 cursor-pointer select-none">
                  <input type="checkbox" checked={klineCodes.includes(code)} onChange={() => toggleKline(code)}
                    className="accent-sky-500 w-3.5 h-3.5" />
                  <span className={klineCodes.includes(code) ? 'text-gray-300' : 'text-gray-600'}>
                    {code} {nameOf(code)}
                  </span>
                </label>
              )
            })}
          </div>

          {klineCodes.length === 0 ? (
            <div className="text-gray-500 text-center py-16">未选择任何股票 — 在上方勾选, 或到股票池点卡片</div>
          ) : (
            <div className="space-y-6">
              {klineResults.map((r, i) => {
                const code = klineCodes[i]
                if (r.isLoading) return <div key={code} className="bg-gray-900 border border-gray-800 rounded-lg p-6 text-gray-500 text-sm">{code} K线与指标加载中...</div>
                if (r.error || !r.data) return <div key={code} className="bg-gray-900 border border-gray-800 rounded-lg p-6 text-red-400 text-sm">{code} 加载失败</div>
                const d = r.data.kline
                const st = r.data.st
                return (
                  <div key={code} className="bg-gray-900 border border-gray-800 rounded-lg p-3">
                    <div className="flex items-center gap-3 mb-1 flex-wrap">
                      <h3 className="text-sm font-semibold text-white">
                        {d.code} {d.name}
                        <button onClick={() => toggleKline(code)} className="ml-2 text-[11px] text-gray-600 hover:text-red-400">移除</button>
                      </h3>
                      <span className="text-[11px] text-gray-400">
                        独立策略视角(全仓只做这一只): {st.round_count} 轮
                        {st.round_count > 0 && (
                          <>
                            <span className="ml-1.5">胜率 {st.win_rate_pct}%</span>
                            <span className={`ml-1.5 ${st.total_return_pct >= 0 ? 'text-red-400' : 'text-green-400'}`}>
                              累计 {st.total_return_pct >= 0 ? '+' : ''}{st.total_return_pct}%
                            </span>
                          </>
                        )}
                        {st.holding && <span className="text-amber-400 ml-1.5">独立视角持仓中</span>}
                        {positions.some(([c]) => c === code) && <span className="text-sky-400 ml-1.5">组合持仓中</span>}
                      </span>
                    </div>
                    <ValueStockKline data={{ ...d, trades: st.trades }} group={`valueKline-${code}`} range={range} />
                    <div className="mt-1 text-[11px] text-gray-600">选股指标: PE上限20 · 股息率门槛3% · bias 0.88主买/0.90侧门/1.08离场 · 与上方K线联动缩放</div>
                    <ValueIndicatorsChart ind={r.data.ind} group={`valueKline-${code}`} range={range} />
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* ---------------- 栏四: 数据管理与定时任务(本页专属, 不进 ETF 数据管理页) ---------------- */}
      {tab === 'admin' && (
        <div className="space-y-6">
          <div>
            <h3 className="text-sm font-semibold text-white mb-2">手动任务(后台异步, 断点续传)</h3>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {MANUAL_JOBS.map(j => {
                const job = (jobsQ.data ?? [])
                  .filter(x => x.task === j.task)
                  .sort((a, b) => (b.started_at ?? '').localeCompare(a.started_at ?? ''))[0]
                const running = job?.status === 'running'
                return (
                  <div key={j.task} className="bg-gray-900 border border-gray-800 rounded-lg p-4 flex flex-col gap-2">
                    <div className="flex items-center justify-between gap-2">
                      <h4 className="text-sm font-medium text-white">{j.label}</h4>
                      <button
                        onClick={() => runJob(j.task)}
                        disabled={running}
                        className={`px-2.5 py-1 rounded text-xs border ${
                          running
                            ? 'bg-gray-800 text-gray-500 border-gray-700 cursor-not-allowed'
                            : 'bg-sky-600/20 text-sky-300 border-sky-500/40 hover:bg-sky-600/30'
                        }`}
                      >
                        {running ? '运行中...' : '执行'}
                      </button>
                    </div>
                    <p className="text-[11px] text-gray-500 leading-relaxed">{j.desc}</p>
                    {job && (
                      <div className="mt-auto pt-2 border-t border-gray-800 text-[11px] space-y-1">
                        <div className="flex justify-between">
                          <span className={job.status === 'success' ? 'text-green-400' : job.status === 'failed' ? 'text-red-400' : 'text-amber-400'}>
                            {job.status === 'running' ? '运行中' : job.status === 'success' ? '✓ 完成' : '✗ 失败'}
                          </span>
                          <span className="text-gray-600 font-mono">{(job.started_at ?? '').replace('T', ' ').slice(5, 16)}</span>
                        </div>
                        {running && job.total > 0 && (
                          <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
                            <div className="h-full bg-sky-500 rounded-full transition-[width]" style={{ width: `${Math.min(100, (job.current / job.total) * 100)}%` }} />
                          </div>
                        )}
                        <div className="text-gray-500 truncate" title={job.message}>{job.message || '-'}</div>
                        {job.error && <div className="text-red-400 truncate" title={job.error}>{job.error}</div>}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-white mb-2">定时任务(自动运行, 服务重启后自动恢复)</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {(scheduledQ.data ?? []).map((t: ValueScheduledTask) => {
                const nextMs = t.next_run ? new Date(t.next_run).getTime() : null
                return (
                  <div key={t.id} className="bg-gray-900 border border-gray-800 rounded-lg p-4 flex flex-col gap-2">
                    <div className="flex items-center justify-between gap-2">
                      <h4 className="text-sm font-medium text-white">{t.label}</h4>
                      <span className="shrink-0 text-[11px] text-gray-400 bg-gray-800 rounded px-2 py-0.5">{t.schedule}</span>
                    </div>
                    {nextMs !== null ? (
                      <div className="flex items-baseline justify-between">
                        <span className="font-mono text-lg text-gray-100">{fmtCountdown(nextMs - Date.now())}</span>
                        <span className="text-[11px] text-gray-500 font-mono">下次 {t.next_run?.replace('T', ' ').split('+')[0].slice(5, 19)}</span>
                      </div>
                    ) : (
                      <span className="text-xs text-gray-600">调度器未注册</span>
                    )}
                    <p className="text-[11px] text-gray-400 leading-relaxed">{t.purpose}</p>
                    <div className="mt-auto pt-2 border-t border-gray-800 space-y-1.5">
                      {t.data_flow.map((f, i) => {
                        const badge = STEP_BADGE[f.step] ?? { label: f.step, cls: 'text-gray-400 bg-gray-400/10' }
                        return (
                          <div key={i} className="flex items-start gap-2 text-[11px]">
                            <span className={`shrink-0 rounded px-1.5 py-0.5 font-medium ${badge.cls}`}>{badge.label}</span>
                            <span className="text-gray-400 leading-relaxed">{f.text}</span>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )
              })}
              {scheduledQ.data?.length === 0 && <div className="text-gray-500 text-sm py-6">调度器未就绪, 请稍后刷新</div>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
