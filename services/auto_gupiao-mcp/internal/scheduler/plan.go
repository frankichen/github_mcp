package scheduler

import (
	"strings"
	"time"
)

type Task struct {
	Name        string `json:"name"`
	Action      string `json:"action"`
	Time        string `json:"time"`
	Description string `json:"description"`
}

type Plan struct {
	TradeDate     string `json:"trade_date"`
	NextTradeDate string `json:"next_trade_date"`
	Timezone      string `json:"timezone"`
	Tasks         []Task `json:"tasks"`
}

type Config struct {
	TradeDate     string
	NextTradeDate string
	Timezone      string
	SelectTime    string
	BuyTime       string
	SellTime      string
	ReportTime    string
}

func BuildPlan(cfg Config) Plan {
	tradeDate := normalizeDate(cfg.TradeDate)
	if tradeDate == "" {
		tradeDate = time.Now().Format("20060102")
	}
	next := normalizeDate(cfg.NextTradeDate)
	if next == "" {
		next = nextCalendarDay(tradeDate)
	}
	tz := cfg.Timezone
	if tz == "" {
		tz = "Asia/Shanghai"
	}
	selectTime := defaultString(cfg.SelectTime, "14:00")
	buyTime := defaultString(cfg.BuyTime, "14:05")
	sellTime := defaultString(cfg.SellTime, "10:00")
	reportTime := defaultString(cfg.ReportTime, "15:30")
	return Plan{
		TradeDate:     tradeDate,
		NextTradeDate: next,
		Timezone:      tz,
		Tasks: []Task{
			{Name: "盘中选股", Action: "select", Time: tradeDate + " " + selectTime, Description: "拉取行情快照并生成候选股"},
			{Name: "模拟买入", Action: "simulate_buy", Time: tradeDate + " " + buyTime, Description: "按候选股和仓位规则执行模拟买入"},
			{Name: "模拟卖出", Action: "simulate_sell", Time: next + " " + sellTime, Description: "按次交易日价格执行模拟卖出"},
			{Name: "生成报告", Action: "report", Time: next + " " + reportTime, Description: "汇总成交、持仓、收益和风控信息"},
		},
	}
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

func nextCalendarDay(date string) string {
	parsed, err := time.Parse("20060102", normalizeDate(date))
	if err != nil {
		return normalizeDate(date)
	}
	return parsed.AddDate(0, 0, 1).Format("20060102")
}

func defaultString(value string, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return strings.TrimSpace(value)
}
