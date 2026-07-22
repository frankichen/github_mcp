# auto_gupiao

A 股自动选股与短线交易辅助系统，目标是围绕“1 万元本金、14:00 后生成买入候选、次交易日 10:00 左右卖出”的短线交易流程，沉淀一套可回测、可模拟、可审计、可逐步接入实盘的 Go 工程。

> 重要说明：本项目仅用于策略研究、工程开发和交易辅助，不构成任何投资建议，也不承诺收益。进入实盘前必须完成充分回测、模拟盘验证和风控检查。

## 核心能力

- A 股可交易股票池过滤：剔除停牌、ST、涨跌停不可交易、不可整手买入等标的。
- 资金约束：默认本金 10,000 元，按 100 股整手和交易成本估算可买数量。
- 规则评分：融合均线趋势、量能、换手率、RSI、短期涨幅、估值和基本面质量。
- 技术指标：自动计算 MA5、MA20、MA60、RSI6、5 日涨幅、20 日涨幅。
- 数据源抽象：策略层只依赖 `MarketDataProvider`，后续可替换 Tushare、券商行情或本地缓存。
- Tushare 接入：支持拉取股票基础信息、历史日线行情、每日基础指标、交易日历并合成选股快照。
- 历史回测：支持用日线 CSV 模拟当日收盘附近买入、次日开盘卖出，并统计扣费后收益。
- 分片缓存：支持按交易日缓存行情快照，按交易所和日期范围缓存交易日历。
- 风控优先：数据异常、流动性不足、涨跌停、收益/风险不达标时不交易。
- CLI 原型：支持读取 CSV、Tushare 行情快照或历史 K 线并输出 JSON。

## 快速开始

使用示例 CSV 选股：

```bash
go test ./...
go run ./cmd/autogupiao -mode select -source csv -input examples/sample_quotes.csv -top 5 -cash 10000 -min-score 60
```

使用 Tushare 选股：

```bash
export TUSHARE_TOKEN=你的token
go run ./cmd/autogupiao -mode select -source tushare -trade-date 20260518 -top 5 -cash 10000 -min-score 60
```

强制刷新行情快照和交易日历缓存：

```bash
go run ./cmd/autogupiao -mode select -source tushare -trade-date 20260518 -refresh-cache -refresh-calendar
```

运行历史回测：

```bash
go run ./cmd/autogupiao -mode backtest -input examples/sample_daily_bars.csv -cash 10000 -top 1 -min-score 60
```

输出示例为候选股数组或回测报告 JSON。

## 当前目录结构

```text
.
├── README.md
├── 需求文档.md
├── configs/
│   └── example.yaml
├── docs/
│   ├── 技术架构设计.md
│   ├── 策略与风控设计.md
│   └── 开发路线图.md
├── examples/
│   ├── sample_daily_bars.csv
│   └── sample_quotes.csv
├── cmd/autogupiao/
│   └── main.go
└── internal/
    ├── backtest/
    ├── data/
    ├── domain/
    ├── indicator/
    ├── marketdata/
    │   └── tushare/
    ├── selector/
    └── storage/
```

## 数据源设计

核心接口：

```go
type Provider interface {
    ListStocks(ctx context.Context) ([]domain.StockBasic, error)
    DailyBars(ctx context.Context, code string, startDate string, endDate string) ([]domain.DailyBar, error)
    DailyBasics(ctx context.Context, tradeDate string) ([]domain.Fundamental, error)
    TradeCalendar(ctx context.Context, exchange string, startDate string, endDate string) ([]domain.TradingDay, error)
    ListSnapshots(ctx context.Context, tradeDate string) ([]domain.StockSnapshot, error)
}
```

当前已实现：

- `internal/marketdata/tushare`：Tushare HTTP 客户端。
- `internal/indicator`：MA、RSI、区间涨幅等技术指标计算。
- `internal/backtest`：历史回测引擎。
- `internal/storage`：本地 JSON 快照缓存和交易日历缓存。
- `internal/data`：CSV 快照和历史日线读取。

## 技术指标计算

Tushare 数据源会默认拉取目标交易日前约 140 个自然日的日线数据，用于覆盖至少 60 个交易日窗口，并自动填充：

- `ma5`
- `ma20`
- `ma60`
- `rsi6`
- `five_day_pct`
- `twenty_day_pct`

这些字段会进入现有规则评分器，用来判断趋势、动量和过热风险。

## 回测设计

当前回测是 MVP 版本，使用日线数据近似短线交易流程：

- 买入价：当日收盘价，近似 14:00 后买入价格。
- 卖出价：次交易日开盘价，近似次日 10:00 附近卖出价格。
- 交易成本：包含佣金、最低佣金、过户费、印花税、滑点。
- 输出指标：总收益率、胜率、平均收益、最大回撤、盈亏因子、最大连续亏损等。

## 缓存设计

默认缓存路径：

```text
data/cache/snapshots/20260518.json
data/cache/calendar/SSE_20260418_20260525.json
```

如果 `-cache` 或 `-calendar-cache` 传入 `.json` 结尾路径，系统会兼容旧的单文件缓存；如果传入目录路径，系统会自动生成分片缓存文件。

## CSV 输入字段

选股快照 CSV 字段见 `examples/sample_quotes.csv`。历史回测日线 CSV 字段见 `examples/sample_daily_bars.csv`。

快照核心字段包括：

- `code`, `name`, `market`, `close`
- `change_pct`, `turnover_rate`, `volume_ratio`
- `ma5`, `ma20`, `ma60`, `rsi6`
- `five_day_pct`, `twenty_day_pct`
- `roe`, `revenue_growth`, `net_profit_growth`, `debt_asset_ratio`, `pb`
- `limit_up`, `limit_down`, `suspended`, `st`

日线核心字段包括：

- `date`, `code`, `open`, `high`, `low`, `close`
- `prev_close`, `change`, `change_pct`, `volume`, `amount`

## 后续开发优先级

1. 完成模拟盘撮合、持仓和资金账户。
2. 接入任务调度：14:00 后选股、次日 10:00 卖出。
3. 增加数据质量校验和异常熔断。
4. 将回测卖出价从次日开盘扩展为可配置的分钟线/目标时间价格。
5. 在模拟盘稳定后，再评估是否接入真实交易接口。

## 风险原则

- 不追求“每天必交易”，宁可空仓也不做低胜率交易。
- 不以涨停、热点、消息面作为唯一依据，必须通过量价、流动性和风控验证。
- 所有策略参数必须能通过回测与模拟盘复核。
- 实盘必须保留人工确认或熔断机制。
