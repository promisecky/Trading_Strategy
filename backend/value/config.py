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
# ---- 卖出三规则 v2(walk-forward 验证: 盲测 2023-2026 夏普 1.05 无衰减) ----
# ① 动态止损: 缩幅阴跌(洗盘)放宽、放幅急跌(出货)收紧
STOP_BASE = 0.92  # 动态止损基准线
STOP_WIDEN = 0.04  # 水位<1 时最大放宽幅度(quiet=1 时 s=0.88)
STOP_TIGHTEN = 0.05  # 水位>1.5 时收紧幅度(heavy=1 时 s=0.97)
# ② 移动止盈: +8% 激活, 自最高收盘回落 4% 卖, 上限 +30%
TP_ARM = 1.08
TP_TRAIL = 0.96
TP_CAP = 1.30
BIAS_EXIT = 1.08  # ③ bias ≥ 1.08
COOLDOWN_DAYS = 40  # ① 止损后该股冷却 40 个交易日
RULES_VERSION = "v2-dyn-trail"  # 卖出规则版本戳: 变更时使落盘回测状态失效

# ---- 仓位: 五条军规 ----
MAX_SLOTS = 5  # 最多 5 只, 等权, 单票 = 总资产 ÷ 5

# ---- 资金: 闲置现金场内货币化 ----
IDLE_YIELD_PCT = 2.0  # 闲置资金年化(%), 0 = 关闭

# ---- 指标窗口 ----
MA_WINDOW = 120  # bias = 收盘 ÷ 120 日均线
Z_WINDOW = 250  # z = (bias − 250日均值) ÷ 250日标准差

KLINE_START_YEAR = 2017  # K线拉取起始年(含 120+250 暖机, 覆盖 2018 回测)

TRADING_DAYS_PER_YEAR = 252

# ---- 闲钱货币化 ----
IDLE_FUND_CODE = "511880"  # 银华日利(场内货币ETF): 闲钱按其前复权日线收益计息
IDLE_FUND_NAME = "银华日利"
