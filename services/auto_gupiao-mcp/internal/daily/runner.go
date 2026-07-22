package daily

import (
	"fmt"

	"github.com/frankichen/auto_gupiao/internal/domain"
	"github.com/frankichen/auto_gupiao/internal/paper"
	"github.com/frankichen/auto_gupiao/internal/report"
)

type Config struct {
	InitialCash               float64
	TopN                      int
	MinScore                  float64
	MaxPositionRatio          float64
	AllowMultipleBuys         bool
	StrategyProfile           string
	StrictEntry               bool
	SellPriceMode             string
	MinTradeAmount            float64
	MinHoldDays               int
	MaxHoldDays               int
	StopLossPct               float64
	TakeProfitPct             float64
	CooldownDays              int
	StopLossCooldownDays      int
	PoorPerformerFilter       bool
	RepeatedStopLossFilter    bool
	PoorPerformerMinTrades    int
	PoorPerformerMaxNetProfit float64
	SingleLossFilter          bool
	SingleLossMaxNetProfit    float64
	SingleLossMaxReturnPct    float64
	LargeLossFilter           bool
	LargeLossMaxReturnPct     float64
	LargeLossCooldownDays     int
	LossStreakPause           bool
	LossStreakThreshold       int
	LossStreakPauseDays       int
	ReportDir                 string
	ReportPrefix              string
	ReportDate                string
	Cost                      domain.CostModel
}

type Result struct {
	Paper  paper.Result    `json:"paper"`
	Report report.Manifest `json:"report"`
}

func RunPaperReport(bars []domain.DailyBar, cfg Config) (Result, error) {
	paperResult := paper.Run(bars, paper.Config{
		InitialCash:               cfg.InitialCash,
		TopN:                      cfg.TopN,
		MinScore:                  cfg.MinScore,
		MaxPositionRatio:          cfg.MaxPositionRatio,
		AllowMultipleBuys:         cfg.AllowMultipleBuys,
		StrategyProfile:           cfg.StrategyProfile,
		StrictEntry:               cfg.StrictEntry,
		SellPriceMode:             cfg.SellPriceMode,
		MinTradeAmount:            cfg.MinTradeAmount,
		MinHoldDays:               cfg.MinHoldDays,
		MaxHoldDays:               cfg.MaxHoldDays,
		StopLossPct:               cfg.StopLossPct,
		TakeProfitPct:             cfg.TakeProfitPct,
		CooldownDays:              cfg.CooldownDays,
		StopLossCooldownDays:      cfg.StopLossCooldownDays,
		PoorPerformerFilter:       cfg.PoorPerformerFilter,
		RepeatedStopLossFilter:    cfg.RepeatedStopLossFilter,
		PoorPerformerMinTrades:    cfg.PoorPerformerMinTrades,
		PoorPerformerMaxNetProfit: cfg.PoorPerformerMaxNetProfit,
		SingleLossFilter:          cfg.SingleLossFilter,
		SingleLossMaxNetProfit:    cfg.SingleLossMaxNetProfit,
		SingleLossMaxReturnPct:    cfg.SingleLossMaxReturnPct,
		LargeLossFilter:           cfg.LargeLossFilter,
		LargeLossMaxReturnPct:     cfg.LargeLossMaxReturnPct,
		LargeLossCooldownDays:     cfg.LargeLossCooldownDays,
		LossStreakPause:           cfg.LossStreakPause,
		LossStreakThreshold:       cfg.LossStreakThreshold,
		LossStreakPauseDays:       cfg.LossStreakPauseDays,
		Cost:                      cfg.Cost,
	})
	manifest, err := report.WritePaperReport(paperResult, report.Options{
		ReportDir: cfg.ReportDir,
		Prefix:    cfg.ReportPrefix,
		Date:      cfg.ReportDate,
	})
	if err != nil {
		return Result{}, fmt.Errorf("write daily paper report: %w", err)
	}
	return Result{Paper: paperResult, Report: manifest}, nil
}
