package report

import (
	"bytes"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/paper"
)

type Options struct {
	ReportDir string
	Prefix    string
	Date      string
}

type Manifest struct {
	MarkdownPath string `json:"markdown_path"`
	TradesCSV    string `json:"trades_csv"`
	EquityCSV    string `json:"equity_csv"`
}

type PaperInsights struct {
	DataStart            string   `json:"data_start"`
	DataEnd              string   `json:"data_end"`
	LastTradeSellDate    string   `json:"last_trade_sell_date"`
	CurrentOpenPositions int      `json:"current_open_positions"`
	RiskLevel            string   `json:"risk_level"`
	Conclusion           string   `json:"conclusion"`
	Warnings             []string `json:"warnings"`
	AvgTradeAmount       float64  `json:"avg_trade_amount"`
	AvgNetReturnPct      float64  `json:"avg_net_return_pct"`
	AvgHoldingDays       float64  `json:"avg_holding_days"`
}

type AttributionRow struct {
	Key            string  `json:"key"`
	Trades         int     `json:"trades"`
	Wins           int     `json:"wins"`
	WinRatePct     float64 `json:"win_rate_pct"`
	NetProfit      float64 `json:"net_profit"`
	AvgReturnPct   float64 `json:"avg_return_pct"`
	MaxProfit      float64 `json:"max_profit"`
	MaxLoss        float64 `json:"max_loss"`
	AvgHoldingDays float64 `json:"avg_holding_days"`
}

func LoadPaperResult(r io.Reader) (paper.Result, error) {
	var result paper.Result
	if err := json.NewDecoder(r).Decode(&result); err != nil {
		return paper.Result{}, fmt.Errorf("decode paper result: %w", err)
	}
	return result, nil
}

func WritePaperReport(result paper.Result, opts Options) (Manifest, error) {
	opts = normalizeOptions(opts)
	if err := os.MkdirAll(opts.ReportDir, 0755); err != nil {
		return Manifest{}, fmt.Errorf("create report dir: %w", err)
	}
	manifest := Manifest{
		MarkdownPath: filepath.Join(opts.ReportDir, opts.Prefix+".md"),
		TradesCSV:    filepath.Join(opts.ReportDir, opts.Prefix+"_trades.csv"),
		EquityCSV:    filepath.Join(opts.ReportDir, opts.Prefix+"_equity.csv"),
	}
	if err := os.WriteFile(manifest.MarkdownPath, []byte(RenderPaperMarkdown(result, opts.Date)), 0644); err != nil {
		return Manifest{}, fmt.Errorf("write markdown report: %w", err)
	}
	if err := os.WriteFile(manifest.TradesCSV, []byte(RenderTradesCSV(result)), 0644); err != nil {
		return Manifest{}, fmt.Errorf("write trades csv: %w", err)
	}
	if err := os.WriteFile(manifest.EquityCSV, []byte(RenderEquityCSV(result)), 0644); err != nil {
		return Manifest{}, fmt.Errorf("write equity csv: %w", err)
	}
	return manifest, nil
}

func RenderPaperMarkdown(result paper.Result, reportDate string) string {
	if reportDate == "" {
		reportDate = time.Now().Format("20060102")
	}
	insights := BuildPaperInsights(result)
	var b strings.Builder
	b.WriteString("# 历史回放连续模拟盘报告\n\n")
	b.WriteString(fmt.Sprintf("生成日期：%s\n\n", normalizeDate(reportDate)))
	b.WriteString("> 说明：本报告是基于历史日线数据的回放模拟，用于观察策略表现；不是今天真实发生的交易流水，也不代表已经实盘下单。\n\n")

	b.WriteString("## 观察结论\n\n")
	b.WriteString(fmt.Sprintf("**风险等级：%s**\n\n", insights.RiskLevel))
	b.WriteString(insights.Conclusion + "\n\n")
	if len(insights.Warnings) > 0 {
		b.WriteString("### 主要风险信号\n\n")
		for _, warning := range insights.Warnings {
			b.WriteString("- " + warning + "\n")
		}
		b.WriteString("\n")
	}

	b.WriteString("## 数据与模拟范围\n\n")
	b.WriteString("| 项目 | 数值 |\n")
	b.WriteString("| --- | ---: |\n")
	b.WriteString(fmt.Sprintf("| 数据开始日 | %s |\n", emptyAsDash(insights.DataStart)))
	b.WriteString(fmt.Sprintf("| 数据结束日 / 最新模拟日 | %s |\n", emptyAsDash(insights.DataEnd)))
	b.WriteString(fmt.Sprintf("| 最近一笔模拟卖出日 | %s |\n", emptyAsDash(insights.LastTradeSellDate)))
	b.WriteString(fmt.Sprintf("| 最新持仓数 | %d |\n", insights.CurrentOpenPositions))
	b.WriteString(fmt.Sprintf("| 平均单笔买入成本 | %.2f |\n", insights.AvgTradeAmount))
	b.WriteString(fmt.Sprintf("| 平均单笔收益率 | %.4f%% |\n", insights.AvgNetReturnPct))
	b.WriteString(fmt.Sprintf("| 平均持有天数 | %.2f |\n", insights.AvgHoldingDays))
	b.WriteString("\n")

	b.WriteString("## 概览\n\n")
	b.WriteString("| 指标 | 数值 |\n")
	b.WriteString("| --- | ---: |\n")
	b.WriteString(fmt.Sprintf("| 初始资金 | %.2f |\n", result.InitialCash))
	b.WriteString(fmt.Sprintf("| 最终权益 | %.2f |\n", result.FinalEquity))
	b.WriteString(fmt.Sprintf("| 总收益率 | %.4f%% |\n", result.TotalReturnPct))
	b.WriteString(fmt.Sprintf("| 最大回撤 | %.4f%% |\n", result.MaxDrawdownPct))
	b.WriteString(fmt.Sprintf("| 交易天数 | %d |\n", result.Days))
	b.WriteString(fmt.Sprintf("| 交易次数 | %d |\n", result.Trades))
	b.WriteString(fmt.Sprintf("| 胜率 | %.4f%% |\n", result.WinRatePct))
	if result.ProfitFactor == nil {
		b.WriteString("| 盈亏因子 | 无亏损交易 / ∞ |\n")
	} else {
		b.WriteString(fmt.Sprintf("| 盈亏因子 | %.4f |\n", *result.ProfitFactor))
	}
	b.WriteString(fmt.Sprintf("| 最大连续亏损 | %d |\n", result.MaxConsecutiveLoss))
	b.WriteString("\n")

	writeOpenPositionsSection(&b, result.OpenPositions)
	writeFilterStatsSection(&b, result.FilterStats, 20)
	writeAttributionSection(&b, "按股票归因", BuildTradeAttribution(result.TradesList, func(t paper.Trade) string { return t.Code }), 12)
	writeAttributionSection(&b, "按卖出原因归因", BuildTradeAttribution(result.TradesList, func(t paper.Trade) string { return t.ExitReason }), 12)
	writeLossTradesSection(&b, result.TradesList, 10)

	b.WriteString("## 最近权益\n\n")
	b.WriteString("| 日期 | 现金 | 持仓市值 | 总权益 | 日收益率 | 持仓数 |\n")
	b.WriteString("| --- | ---: | ---: | ---: | ---: | ---: |\n")
	for _, item := range tailEquity(result.DailyEquity, 10) {
		b.WriteString(fmt.Sprintf("| %s | %.2f | %.2f | %.2f | %.4f%% | %d |\n", item.Date, item.Cash, item.MarketValue, item.Equity, item.DailyReturnPct, item.OpenPositions))
	}
	b.WriteString("\n")

	b.WriteString("## 最近已平仓历史模拟交易\n\n")
	b.WriteString("> 这里展示的是历史回放中最近完成平仓的模拟交易，不是今天真实买卖。\n\n")
	b.WriteString("| 买入日 | 卖出日 | 代码 | 股数 | 买入价 | 卖出价 | 持有天数 | 卖出原因 | 净利润 | 收益率 | 风险 |\n")
	b.WriteString("| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |\n")
	for _, trade := range tailTrades(result.TradesList, 20) {
		b.WriteString(fmt.Sprintf("| %s | %s | %s | %d | %.4f | %.4f | %d | %s | %.2f | %.4f%% | %s |\n", trade.BuyDate, trade.SellDate, trade.Code, trade.Shares, trade.BuyPrice, trade.SellPrice, trade.HoldingDays, trade.ExitReason, trade.NetProfit, trade.NetReturnPct, trade.RiskLevel))
	}
	b.WriteString("\n")
	b.WriteString("## 风险提示\n\n")
	b.WriteString("本报告基于历史日线近似模拟生成，不构成任何投资建议。实盘前必须继续验证数据质量、撮合偏差、滑点、交易费用、最低佣金影响和风控规则。\n")
	return b.String()
}

func BuildTradeAttribution(trades []paper.Trade, keyFunc func(paper.Trade) string) []AttributionRow {
	type acc struct {
		row         AttributionRow
		returnSum   float64
		holdingSum  float64
		initialized bool
	}
	items := map[string]*acc{}
	for _, trade := range trades {
		key := strings.TrimSpace(keyFunc(trade))
		if key == "" {
			key = "unknown"
		}
		item := items[key]
		if item == nil {
			item = &acc{row: AttributionRow{Key: key}, initialized: true}
			items[key] = item
		}
		item.row.Trades++
		if trade.NetProfit >= 0 {
			item.row.Wins++
		}
		item.row.NetProfit += trade.NetProfit
		item.returnSum += trade.NetReturnPct
		item.holdingSum += float64(trade.HoldingDays)
		if item.row.Trades == 1 || trade.NetProfit > item.row.MaxProfit {
			item.row.MaxProfit = trade.NetProfit
		}
		if item.row.Trades == 1 || trade.NetProfit < item.row.MaxLoss {
			item.row.MaxLoss = trade.NetProfit
		}
	}
	rows := make([]AttributionRow, 0, len(items))
	for _, item := range items {
		if item.row.Trades > 0 {
			item.row.WinRatePct = round(float64(item.row.Wins)/float64(item.row.Trades)*100, 4)
			item.row.AvgReturnPct = round(item.returnSum/float64(item.row.Trades), 4)
			item.row.AvgHoldingDays = round(item.holdingSum/float64(item.row.Trades), 2)
			item.row.NetProfit = round(item.row.NetProfit, 2)
			item.row.MaxProfit = round(item.row.MaxProfit, 2)
			item.row.MaxLoss = round(item.row.MaxLoss, 2)
		}
		rows = append(rows, item.row)
	}
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].NetProfit < rows[j].NetProfit })
	return rows
}

func BuildPaperInsights(result paper.Result) PaperInsights {
	insights := PaperInsights{RiskLevel: "低", Conclusion: "当前历史回放结果暂未触发明显风险阈值，可继续观察，但仍不构成实盘建议。"}
	if len(result.DailyEquity) > 0 {
		insights.DataStart = result.DailyEquity[0].Date
		last := result.DailyEquity[len(result.DailyEquity)-1]
		insights.DataEnd = last.Date
		insights.CurrentOpenPositions = last.OpenPositions
	}
	if len(result.TradesList) > 0 {
		lastTrade := result.TradesList[len(result.TradesList)-1]
		insights.LastTradeSellDate = lastTrade.SellDate
		totalCost := 0.0
		totalReturn := 0.0
		totalHoldingDays := 0.0
		for _, trade := range result.TradesList {
			totalCost += trade.BuyCost
			totalReturn += trade.NetReturnPct
			if trade.HoldingDays > 0 {
				totalHoldingDays += float64(trade.HoldingDays)
			} else {
				totalHoldingDays += holdingDays(trade.BuyDate, trade.SellDate)
			}
		}
		count := float64(len(result.TradesList))
		insights.AvgTradeAmount = round(totalCost/count, 2)
		insights.AvgNetReturnPct = round(totalReturn/count, 4)
		insights.AvgHoldingDays = round(totalHoldingDays/count, 2)
	}
	warnings := make([]string, 0)
	if result.TotalReturnPct <= -10 {
		warnings = append(warnings, fmt.Sprintf("历史回放总收益率为 %.4f%%，策略当前处于明显亏损。", result.TotalReturnPct))
	}
	if result.MaxDrawdownPct >= 20 {
		warnings = append(warnings, fmt.Sprintf("最大回撤达到 %.4f%%，超过观察盘常用风控阈值。", result.MaxDrawdownPct))
	}
	if result.Trades > 0 && result.WinRatePct < 35 {
		warnings = append(warnings, fmt.Sprintf("胜率仅 %.4f%%，信号质量偏弱。", result.WinRatePct))
	}
	if result.MaxConsecutiveLoss >= 10 {
		warnings = append(warnings, fmt.Sprintf("最大连续亏损 %d 次，连续止损风险很高。", result.MaxConsecutiveLoss))
	}
	if result.Days > 0 && float64(result.Trades)/float64(result.Days) > 1.0 {
		warnings = append(warnings, "交易频率过高，当前一日一卖的回放机制可能导致过度交易。")
	}
	if insights.AvgTradeAmount > 0 && insights.AvgTradeAmount < 3000 {
		warnings = append(warnings, fmt.Sprintf("平均单笔买入成本约 %.2f，金额偏小，A股最低佣金和滑点会显著侵蚀收益。", insights.AvgTradeAmount))
	}
	if result.FinalEquity > 0 && result.InitialCash > 0 && result.FinalEquity < result.InitialCash*0.5 {
		warnings = append(warnings, "期末权益已低于初始资金的一半，不适合进入实盘。")
	}
	insights.Warnings = warnings
	if len(warnings) >= 4 || result.TotalReturnPct <= -30 || result.MaxDrawdownPct >= 30 {
		insights.RiskLevel = "高"
		insights.Conclusion = "当前历史回放表现很差，建议暂停实盘，只保留观察；下一步应先优化持仓周期、最低交易金额、止损/止盈和选股阈值。"
	} else if len(warnings) > 0 {
		insights.RiskLevel = "中"
		insights.Conclusion = "当前历史回放存在风险信号，建议继续观察并调参，暂不建议实盘。"
	}
	return insights
}

func writeOpenPositionsSection(b *strings.Builder, positions []paper.OpenPosition) {
	b.WriteString("## 当前未平仓持仓\n\n")
	if len(positions) == 0 {
		b.WriteString("当前历史回放末尾无未平仓持仓。\n\n")
		return
	}
	b.WriteString("> 这里展示的是历史回放末尾仍未平仓的模拟持仓，不是今天真实持仓。\n\n")
	b.WriteString("| 代码 | 买入日 | 当前日 | 股数 | 买入价 | 当前价 | 买入成本 | 估算净值 | 浮动盈亏 | 浮动收益率 | 持有天数 | 风险 |\n")
	b.WriteString("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |\n")
	for _, pos := range positions {
		b.WriteString(fmt.Sprintf("| %s | %s | %s | %d | %.4f | %.4f | %.2f | %.2f | %.2f | %.4f%% | %d | %s |\n", pos.Code, pos.BuyDate, pos.CurrentDate, pos.Shares, pos.BuyPrice, pos.CurrentPrice, pos.BuyCost, pos.EstimatedNetValue, pos.UnrealizedProfit, pos.UnrealizedReturnPct, pos.HoldingDays, pos.RiskLevel))
	}
	b.WriteString("\n")
}

func writeFilterStatsSection(b *strings.Builder, stats []paper.FilterStat, limit int) {
	b.WriteString("## 过滤生效统计\n\n")
	if len(stats) == 0 {
		b.WriteString("本次历史回放未触发过滤规则。\n\n")
		return
	}
	b.WriteString("| 代码 | 过滤原因 | 次数 | 最近日期 |\n")
	b.WriteString("| --- | --- | ---: | --- |\n")
	if limit > 0 && len(stats) > limit {
		stats = stats[:limit]
	}
	for _, stat := range stats {
		b.WriteString(fmt.Sprintf("| %s | %s | %d | %s |\n", stat.Code, filterReasonLabel(stat.Reason), stat.Count, stat.LastDate))
	}
	b.WriteString("\n")
}

func filterReasonLabel(reason string) string {
	switch reason {
	case "single_large_stop_loss":
		return "单笔大亏过滤"
	case "recent_stop_loss_cooldown":
		return "止损后冷却"
	case "poor_performer":
		return "累计亏损过滤"
	case "loss_cooldown":
		return "亏损后冷却"
	default:
		return reason
	}
}

func writeAttributionSection(b *strings.Builder, title string, rows []AttributionRow, limit int) {
	b.WriteString("## " + title + "\n\n")
	if len(rows) == 0 {
		b.WriteString("暂无交易。\n\n")
		return
	}
	b.WriteString("| 项目 | 笔数 | 胜率 | 净利润 | 平均收益率 | 最大盈利 | 最大亏损 | 平均持有天数 |\n")
	b.WriteString("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n")
	if limit > 0 && len(rows) > limit {
		rows = rows[:limit]
	}
	for _, row := range rows {
		b.WriteString(fmt.Sprintf("| %s | %d | %.4f%% | %.2f | %.4f%% | %.2f | %.2f | %.2f |\n", row.Key, row.Trades, row.WinRatePct, row.NetProfit, row.AvgReturnPct, row.MaxProfit, row.MaxLoss, row.AvgHoldingDays))
	}
	b.WriteString("\n")
}

func writeLossTradesSection(b *strings.Builder, trades []paper.Trade, limit int) {
	losses := make([]paper.Trade, 0)
	for _, trade := range trades {
		if trade.NetProfit < 0 {
			losses = append(losses, trade)
		}
	}
	sort.SliceStable(losses, func(i, j int) bool { return losses[i].NetProfit < losses[j].NetProfit })
	b.WriteString("## 最大亏损交易 Top 10\n\n")
	if len(losses) == 0 {
		b.WriteString("暂无亏损交易。\n\n")
		return
	}
	b.WriteString("| 买入日 | 卖出日 | 代码 | 卖出原因 | 持有天数 | 净利润 | 收益率 | 风险 |\n")
	b.WriteString("| --- | --- | --- | --- | ---: | ---: | ---: | --- |\n")
	if limit > 0 && len(losses) > limit {
		losses = losses[:limit]
	}
	for _, trade := range losses {
		b.WriteString(fmt.Sprintf("| %s | %s | %s | %s | %d | %.2f | %.4f%% | %s |\n", trade.BuyDate, trade.SellDate, trade.Code, trade.ExitReason, trade.HoldingDays, trade.NetProfit, trade.NetReturnPct, trade.RiskLevel))
	}
	b.WriteString("\n")
}

func RenderTradesCSV(result paper.Result) string {
	var buf bytes.Buffer
	writer := csv.NewWriter(&buf)
	_ = writer.Write([]string{"buy_date", "sell_date", "code", "shares", "buy_price", "sell_price", "holding_days", "exit_reason", "buy_cost", "sell_proceeds", "net_profit", "net_return_pct", "score", "risk_level", "reasons"})
	for _, trade := range result.TradesList {
		_ = writer.Write([]string{
			trade.BuyDate,
			trade.SellDate,
			trade.Code,
			fmt.Sprintf("%d", trade.Shares),
			fmt.Sprintf("%.4f", trade.BuyPrice),
			fmt.Sprintf("%.4f", trade.SellPrice),
			fmt.Sprintf("%d", trade.HoldingDays),
			trade.ExitReason,
			fmt.Sprintf("%.2f", trade.BuyCost),
			fmt.Sprintf("%.2f", trade.SellProceeds),
			fmt.Sprintf("%.2f", trade.NetProfit),
			fmt.Sprintf("%.4f", trade.NetReturnPct),
			fmt.Sprintf("%.4f", trade.Score),
			trade.RiskLevel,
			strings.Join(trade.Reasons, ";"),
		})
	}
	writer.Flush()
	return buf.String()
}

func RenderEquityCSV(result paper.Result) string {
	var buf bytes.Buffer
	writer := csv.NewWriter(&buf)
	_ = writer.Write([]string{"date", "cash", "market_value", "equity", "daily_return_pct", "open_positions"})
	for _, item := range result.DailyEquity {
		_ = writer.Write([]string{
			item.Date,
			fmt.Sprintf("%.2f", item.Cash),
			fmt.Sprintf("%.2f", item.MarketValue),
			fmt.Sprintf("%.2f", item.Equity),
			fmt.Sprintf("%.4f", item.DailyReturnPct),
			fmt.Sprintf("%d", item.OpenPositions),
		})
	}
	writer.Flush()
	return buf.String()
}

func normalizeOptions(opts Options) Options {
	if opts.ReportDir == "" {
		opts.ReportDir = "reports"
	}
	if opts.Date == "" {
		opts.Date = time.Now().Format("20060102")
	}
	if opts.Prefix == "" {
		opts.Prefix = "paper_" + normalizeDate(opts.Date)
	}
	return opts
}

func tailEquity(items []paper.DailyEquity, n int) []paper.DailyEquity {
	if len(items) <= n {
		return items
	}
	return items[len(items)-n:]
}

func tailTrades(items []paper.Trade, n int) []paper.Trade {
	if len(items) <= n {
		return items
	}
	return items[len(items)-n:]
}

func normalizeDate(date string) string {
	var b strings.Builder
	for _, r := range strings.TrimSpace(date) {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

func emptyAsDash(value string) string {
	if strings.TrimSpace(value) == "" {
		return "-"
	}
	return value
}

func holdingDays(buyDate string, sellDate string) float64 {
	buy, err1 := time.Parse("20060102", normalizeDate(buyDate))
	sell, err2 := time.Parse("20060102", normalizeDate(sellDate))
	if err1 != nil || err2 != nil || sell.Before(buy) {
		return 0
	}
	days := sell.Sub(buy).Hours() / 24
	if days < 1 {
		return 1
	}
	return days
}

func round(v float64, places int) float64 {
	factor := 1.0
	for i := 0; i < places; i++ {
		factor *= 10
	}
	if v >= 0 {
		return float64(int(v*factor+0.5)) / factor
	}
	return float64(int(v*factor-0.5)) / factor
}
