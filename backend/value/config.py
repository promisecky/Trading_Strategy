"""价值回归(白马恐慌折扣店)策略常量 — 严格对应《价值回归策略.MD》定稿版。"""

from __future__ import annotations

# ---- 选股: 七道安检门 ----
DIV_YEARS = 5  # 连续分红财年数
DIV_MIN_LAST_RATIO = 0.5  # 最新一年每股分红 ≥ 上年 50%
PE_MIN = 0.0  # PE(TTM) 必须 > 0(亏损拒)
PE_MAX = 20.0  # PE(TTM) ≤ 20
DY_MIN_PCT = 3.0  # 滚动一年股息率 > 3%
DY_LOOKBACK_DAYS = 365  # 滚动一年除权分红窗口
MCAP_MIN_YI = 300.0  # 总市值 ≥ 300 亿
ROE_YEARS = 5  # 最近 5 个已披露财年
ROE_MIN_PCT = 10.0  # 每年 ROE ≥ 10%
REAL_ESTATE_KEYWORDS = ("房地产", "地产")  # 行业黑名单关键词

# 池重建: 每季度首个交易日(1/4/7/10 月的第一个交易日)
POOL_MONTHS = (1, 4, 7, 10)

# ---- 买入 ----
BIAS_MAIN = 0.88  # 主信号: bias ≤ 0.88
BIAS_SIDE_LO = 0.88  # 侧门下限(不含)
BIAS_SIDE_HI = 0.90  # 侧门上限(含)
Z_SIDE = -2.0  # 侧门: z ≤ -2.0
PE_IND_MULT = 1.2  # PE < 所属行业中位数 × 1.2
IND_MIN_COUNT = 3  # 行业不足 3 只则跳过 PE 行业闸

# ---- 卖出(优先级 ①②③) ----
STOP_LOSS = 0.88  # ① 收盘 ≤ 成本×0.88
TAKE_PROFIT = 1.10  # ② 收盘 ≥ 成本×1.10
BIAS_EXIT = 1.08  # ③ bias ≥ 1.08
COOLDOWN_DAYS = 60  # ① 止损后该股冷却 60 个交易日

# ---- 仓位: 五条军规 ----
MAX_SLOTS = 5  # 最多 5 只, 等权, 单票 = 总资产 ÷ 5

# ---- 资金: 闲置现金场内货币化 ----
IDLE_YIELD_PCT = 2.0  # 闲置资金年化(%), 0 = 关闭

# ---- 指标窗口 ----
MA_WINDOW = 120  # bias = 收盘 ÷ 120 日均线
Z_WINDOW = 250  # z = (bias − 250日均值) ÷ 250日标准差

KLINE_START_YEAR = 2017  # K线拉取起始年(含 120+250 暖机, 覆盖 2018 回测)

TRADING_DAYS_PER_YEAR = 252
