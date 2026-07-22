package serverrunner

import (
	"context"
	"fmt"
	"net/url"
	"path/filepath"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/appconfig"
	"github.com/frankichen/auto_gupiao/internal/notify/dingtalk"
	"github.com/frankichen/auto_gupiao/internal/notify/webhook"
	"github.com/frankichen/auto_gupiao/internal/report"
)

const dailyReportEvent = "daily_report.generated"

func notifyDingTalk(ctx context.Context, cfg appconfig.Config, result Result) error {
	client := dingtalk.NewClient(cfg.Notify.DingTalk.Webhook, cfg.Notify.DingTalk.Secret)
	return client.SendMarkdown(ctx, dingtalk.MarkdownMessage{
		Title: "A股观察盘日报",
		Text:  BuildDingTalkMarkdown(result, cfg),
	})
}

func notifyWebhook(ctx context.Context, cfg appconfig.Config, result Result, generatedAt time.Time) error {
	client := webhook.NewClient(cfg.Notify.Webhook.URL, cfg.Notify.Webhook.Secret)
	_, err := client.Send(ctx, BuildWebhookPayload(result, cfg, generatedAt))
	return err
}

func BuildWebhookPayload(result Result, cfg appconfig.Config, generatedAt time.Time) webhook.Payload {
	paperResult := result.Daily.Paper
	insights := report.BuildPaperInsights(paperResult)
	idempotencyKey := fmt.Sprintf("auto_gupiao:%s:%s:%d", dailyReportEvent, cfg.Report.TradeDate, result.RunID)
	return webhook.Payload{
		Source:         "auto_gupiao",
		Event:          dailyReportEvent,
		IdempotencyKey: idempotencyKey,
		RunID:          result.RunID,
		TradeDate:      cfg.Report.TradeDate,
		GeneratedAt:    generatedAt.Format(time.RFC3339),
		Bars: webhook.BarsPayload{
			StartDate: result.Bars.StartDate,
			EndDate:   result.Bars.EndDate,
			Codes:     result.Bars.Codes,
			Rows:      result.Bars.Rows,
		},
		Summary: webhook.SummaryPayload{
			RiskLevel:          insights.RiskLevel,
			Conclusion:         insights.Conclusion,
			InitialCash:        paperResult.InitialCash,
			FinalEquity:        paperResult.FinalEquity,
			TotalReturnPct:     paperResult.TotalReturnPct,
			MaxDrawdownPct:     paperResult.MaxDrawdownPct,
			Trades:             paperResult.Trades,
			WinRatePct:         paperResult.WinRatePct,
			ProfitFactor:       paperResult.ProfitFactor,
			MaxConsecutiveLoss: paperResult.MaxConsecutiveLoss,
		},
		Reports: webhook.ReportsPayload{
			MarkdownURL: publicReportPath(cfg, result.Daily.Report.MarkdownPath),
			TradesURL:   publicReportPath(cfg, result.Daily.Report.TradesCSV),
			EquityURL:   publicReportPath(cfg, result.Daily.Report.EquityCSV),
		},
	}
}

func BuildDingTalkMarkdown(result Result, cfg appconfig.Config) string {
	paperResult := result.Daily.Paper
	reportManifest := result.Daily.Report
	insights := report.BuildPaperInsights(paperResult)
	profitFactor := "未定义"
	if paperResult.ProfitFactor != nil {
		profitFactor = fmt.Sprintf("%.4f", *paperResult.ProfitFactor)
	}
	codes := strings.Join(result.Bars.Codes, ", ")
	if codes == "" {
		codes = "无"
	}
	markdownPath := publicReportPath(cfg, reportManifest.MarkdownPath)
	tradesPath := publicReportPath(cfg, reportManifest.TradesCSV)
	equityPath := publicReportPath(cfg, reportManifest.EquityCSV)
	return fmt.Sprintf(`# A股观察盘日报

**说明**：这是历史日线回放模拟结果，用于观察策略，不是当日成交流水。

**交易日**：%s

**数据状态**：%s

**数据区间**：%s ~ %s

**股票池**：%s

**数据行数**：%d

## 观察结论

- 风险等级：%s
- 结论：%s

## 模拟盘结果

- 初始资金：%.2f
- 最终权益：%.2f
- 总收益率：%.4f%%
- 最大回撤：%.4f%%
- 交易次数：%d
- 胜率：%.4f%%
- 盈利因子：%s
- 最大连续亏损：%d
- 最近一笔模拟卖出日：%s

## 报告文件

- Markdown：%s
- 交易明细：%s
- 权益曲线：%s
`, cfg.Report.TradeDate, dataStatusText(result), result.Bars.StartDate, result.Bars.EndDate, codes, result.Bars.Rows, insights.RiskLevel, insights.Conclusion, paperResult.InitialCash, paperResult.FinalEquity, paperResult.TotalReturnPct, paperResult.MaxDrawdownPct, paperResult.Trades, paperResult.WinRatePct, profitFactor, paperResult.MaxConsecutiveLoss, emptyAsNone(insights.LastTradeSellDate), markdownPath, tradesPath, equityPath)
}

func dataStatusText(result Result) string {
	status := result.DataStatus
	if strings.TrimSpace(status) == "" {
		status = "fresh"
	}
	if status == "cached_fallback" {
		if result.DataStatusReason != "" {
			return "缓存兜底（" + result.DataStatusReason + "）"
		}
		return "缓存兜底"
	}
	return "新拉取"
}

func publicReportPath(cfg appconfig.Config, reportPath string) string {
	baseURL := strings.TrimSpace(cfg.Report.PublicBaseURL)
	if baseURL == "" || strings.TrimSpace(reportPath) == "" {
		return reportPath
	}
	relativePath := relativeReportPath(cfg.Report.ReportDir, reportPath)
	parts := strings.Split(relativePath, "/")
	for i := range parts {
		parts[i] = url.PathEscape(parts[i])
	}
	return strings.TrimRight(baseURL, "/") + "/" + strings.Join(parts, "/")
}

func relativeReportPath(reportDir, reportPath string) string {
	relativePath, err := filepath.Rel(reportDir, reportPath)
	if err != nil || relativePath == "" || relativePath == "." || relativePath == ".." || strings.HasPrefix(relativePath, ".."+string(filepath.Separator)) {
		relativePath = filepath.Base(reportPath)
	}
	return filepath.ToSlash(relativePath)
}

func emptyAsNone(value string) string {
	if strings.TrimSpace(value) == "" {
		return "无"
	}
	return value
}
