import type { ValuePoolCard as PoolCard } from '../../api/types'

/** 恐慌程度徽章: bias 越低越恐慌(策略买点区 ≤0.88) */
function PanicBadge({ bias, z }: { bias?: number | null; z?: number | null }) {
  if (bias == null) return <span className="px-2 py-0.5 rounded text-xs font-medium bg-gray-500/20 text-gray-400">暖机中</span>
  const style =
    bias <= 0.88
      ? { bg: 'bg-red-500/20', text: 'text-red-400', label: '深坑买点区' }
      : bias <= 0.9
        ? { bg: 'bg-amber-500/20', text: 'text-amber-400', label: '侧门观察区' }
        : bias >= 1.08
          ? { bg: 'bg-cyan-500/20', text: 'text-cyan-400', label: '过热离场区' }
          : { bg: 'bg-green-500/20', text: 'text-green-400', label: '正常' }
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium ${style.bg} ${style.text}`}
      title={`bias=收盘÷MA120${z != null ? ` · z=${z}` : ''}`}>
      {style.label}
    </span>
  )
}

function MetricRow({ label, value, tone }: { label: string; value: string; tone?: 'red' | 'green' | 'gray' }) {
  const cls = tone === 'red' ? 'text-red-400' : tone === 'green' ? 'text-green-400' : 'text-gray-200'
  return (
    <div className="flex items-center justify-between">
      <span className="text-[11px] text-gray-500">{label}</span>
      <span className={`text-xs font-mono ${cls}`}>{value}</span>
    </div>
  )
}

export default function ValuePoolCardView({ card, onShowKline }: {
  card: PoolCard
  onShowKline: (code: string) => void
}) {
  const chg = card.change_pct
  const changeColor = chg != null && chg >= 0 ? 'text-red-400' : 'text-green-400'

  return (
    <div
      onClick={() => onShowKline(card.code)}
      className="bg-gray-900 border border-gray-800 rounded-lg p-4 cursor-pointer hover:border-gray-600 transition-colors"
      title="点击查看该股 K 线与买卖点"
    >
      <div className="flex items-center justify-between mb-2">
        <div className="min-w-0">
          <span className="font-medium text-sm text-white truncate">{card.name ?? card.code}</span>
          <span className="ml-2 text-xs text-gray-500 font-mono">{card.code}</span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {card.holding && (
            <span className="px-2 py-0.5 rounded text-xs font-medium bg-sky-500/20 text-sky-400">持仓中</span>
          )}
          <PanicBadge bias={card.bias} z={card.z} />
        </div>
      </div>

      <div className="flex items-baseline gap-3 mb-3">
        <span className="text-lg font-mono text-white">{card.price != null ? card.price.toFixed(2) : '-'}</span>
        {chg != null && (
          <span className={`text-sm font-mono ${changeColor}`}>{chg >= 0 ? '+' : ''}{chg.toFixed(2)}%</span>
        )}
        <span className="text-xs text-gray-500 truncate">{card.industry}</span>
      </div>

      <div className="space-y-1.5">
        <MetricRow label="PE(TTM)" value={card.pe_ttm != null ? card.pe_ttm.toFixed(1) : '-'} />
        <MetricRow label="滚动股息率" value={card.dy_pct != null ? `${card.dy_pct.toFixed(2)}%` : '-'}
          tone={card.dy_pct != null && card.dy_pct > 3 ? 'red' : undefined} />
        <MetricRow label="最新年报 ROE" value={card.roe_pct != null ? `${card.roe_pct.toFixed(1)}%` : '-'} />
        <MetricRow label="总市值" value={card.mcap_yi != null ? `${card.mcap_yi.toFixed(0)} 亿` : '-'} />
        <MetricRow label="bias / z" value={`${card.bias != null ? card.bias.toFixed(3) : '-'} / ${card.z != null ? card.z.toFixed(2) : '-'}`} />
      </div>

      <div className="mt-3 pt-2 border-t border-gray-800">
        <span className="flex gap-1 flex-wrap">
          {card.gates.map(g => (
            <span
              key={g.gate}
              title={`${g.gate}: ${g.reason}`}
              className={`px-1.5 py-0.5 rounded text-[10px] border ${
                g.passed ? 'text-green-400 border-green-500/30' : 'text-red-400 border-red-500/30'
              }`}
            >
              {g.passed ? '✓' : '✗'} {g.gate}
            </span>
          ))}
        </span>
      </div>
    </div>
  )
}
