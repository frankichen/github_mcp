package report

import (
	"strings"
	"testing"

	"github.com/frankichen/auto_gupiao/internal/paper"
)

func TestRenderPaperMarkdown(t *testing.T) {
	pf := 2.5
	result := paper.Result{
		InitialCash:        10000,
		FinalEquity:        10500,
		TotalReturnPct:     5,
		MaxDrawdownPct:     1.2,
		Days:               3,
		Trades:             1,
		Wins:               1,
		WinRatePct:         100,
		ProfitFactor:       &pf,
		MaxConsecutiveLoss: 0,
		DailyEquity: []paper.DailyEquity{
			{Date: "20260519", Cash: 7000, MarketValue: 3000, Equity: 10000, DailyReturnPct: 0, OpenPositions: 1},
			{Date: "20260520", Cash: 10500, MarketValue: 0, Equity: 10500, DailyReturnPct: 5, OpenPositions: 0},
		},
		TradesList: []paper.Trade{
			{Code: "000001", BuyDate: "20260519", SellDate: "20260520", Shares: 100, BuyPrice: 10, BuyCost: 1000, SellPrice: 10.5, NetProfit: 45, NetReturnPct: 1.5, RiskLevel: "medium"},
		},
		OpenPositions: []paper.OpenPosition{
			{Code: "600001", BuyDate: "20260520", CurrentDate: "20260521", Shares: 100, BuyPrice: 10, CurrentPrice: 10.5, BuyCost: 1005, EstimatedNetValue: 1040, UnrealizedProfit: 35, UnrealizedReturnPct: 3.5, HoldingDays: 1, RiskLevel: "low"},
		},
		FilterStats: []paper.FilterStat{
			{Code: "002050", Reason: "single_large_stop_loss", Count: 2, LastDate: "20260522"},
		},
	}
	md := RenderPaperMarkdown(result, "20260520")
	if !strings.Contains(md, "历史回放连续模拟盘报告") || !strings.Contains(md, "不是今天真实发生的交易流水") || !strings.Contains(md, "数据与模拟范围") || !strings.Contains(md, "000001") {
		t.Fatalf("unexpected markdown: %s", md)
	}
	if !strings.Contains(md, "当前未平仓持仓") || !strings.Contains(md, "600001") || !strings.Contains(md, "浮动盈亏") {
		t.Fatalf("expected open position section, got: %s", md)
	}
	if !strings.Contains(md, "过滤生效统计") || !strings.Contains(md, "002050") || !strings.Contains(md, "单笔大亏过滤") {
		t.Fatalf("expected filter stats section, got: %s", md)
	}
}

func TestBuildPaperInsightsHighRisk(t *testing.T) {
	result := paper.Result{
		InitialCash:        10000,
		FinalEquity:        2127.71,
		TotalReturnPct:     -78.7229,
		MaxDrawdownPct:     78.7229,
		Days:               330,
		Trades:             545,
		WinRatePct:         1.4679,
		MaxConsecutiveLoss: 310,
		DailyEquity: []paper.DailyEquity{
			{Date: "20250102", Equity: 10000, OpenPositions: 1},
			{Date: "20260519", Equity: 2127.71, OpenPositions: 0},
		},
		TradesList: []paper.Trade{
			{Code: "601288", BuyDate: "20260327", SellDate: "20260330", BuyCost: 650, NetReturnPct: -2.2},
		},
	}
	insights := BuildPaperInsights(result)
	if insights.RiskLevel != "高" || insights.LastTradeSellDate != "20260330" || len(insights.Warnings) < 4 {
		t.Fatalf("unexpected insights: %+v", insights)
	}
	if !strings.Contains(insights.Conclusion, "暂停实盘") {
		t.Fatalf("unexpected conclusion: %s", insights.Conclusion)
	}
}

func TestRenderCSV(t *testing.T) {
	result := paper.Result{
		DailyEquity: []paper.DailyEquity{{Date: "20260519", Cash: 10000, Equity: 10000}},
		TradesList:  []paper.Trade{{Code: "000001", BuyDate: "20260519", SellDate: "20260520", Shares: 100}},
	}
	trades := RenderTradesCSV(result)
	equity := RenderEquityCSV(result)
	if !strings.Contains(trades, "buy_date") || !strings.Contains(trades, "000001") {
		t.Fatalf("unexpected trades csv: %s", trades)
	}
	if !strings.Contains(equity, "date") || !strings.Contains(equity, "20260519") {
		t.Fatalf("unexpected equity csv: %s", equity)
	}
}
