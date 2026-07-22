package paper

import (
	"testing"
	"time"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestRunProducesDailyEquityAndTrades(t *testing.T) {
	bars := fixtureBars(80)
	result := Run(bars, Config{InitialCash: 10000, TopN: 1, MinScore: 60, MaxPositionRatio: 0.5, MinTradeAmount: 1000, MinHoldDays: 3, MaxHoldDays: 7})
	if result.Days != 80 {
		t.Fatalf("unexpected days: %+v", result)
	}
	if result.FinalEquity <= 0 {
		t.Fatalf("invalid final equity: %+v", result)
	}
	if len(result.DailyEquity) != result.Days {
		t.Fatalf("daily equity mismatch: %+v", result)
	}
	if result.Trades == 0 {
		t.Fatalf("expected trades, got %+v", result)
	}
	for _, trade := range result.TradesList {
		if trade.HoldingDays < 3 && trade.ExitReason != "stop_loss" {
			t.Fatalf("unexpected short holding trade: %+v", trade)
		}
		if trade.ExitReason == "" {
			t.Fatalf("missing exit reason: %+v", trade)
		}
	}
}

func TestRunHandlesEmptyBars(t *testing.T) {
	result := Run(nil, Config{InitialCash: 10000})
	if result.FinalEquity != 10000 || result.Trades != 0 || result.Days != 0 {
		t.Fatalf("unexpected empty result: %+v", result)
	}
}

func TestRunSkipsSmallTrades(t *testing.T) {
	bars := fixtureBars(80)
	result := Run(bars, Config{InitialCash: 10000, TopN: 1, MinScore: 60, MaxPositionRatio: 0.3, MinTradeAmount: 5000})
	if result.Trades != 0 {
		t.Fatalf("expected small trades to be skipped, got %+v", result)
	}
}

func TestRunReportsOpenPositions(t *testing.T) {
	bars := fixtureBars(80)
	result := Run(bars, Config{InitialCash: 10000, TopN: 1, MinScore: 60, MaxPositionRatio: 0.5, MinTradeAmount: 1000, MinHoldDays: 3, MaxHoldDays: 100, StopLossPct: 30, TakeProfitPct: 300})
	if len(result.OpenPositions) == 0 {
		t.Fatalf("expected open positions, got %+v", result)
	}
	pos := result.OpenPositions[0]
	if pos.Code == "" || pos.CurrentDate == "" || pos.CurrentPrice <= 0 || pos.MarketValue <= 0 {
		t.Fatalf("unexpected open position: %+v", pos)
	}
}

func TestIsPoorPerformerUsesClosedTradesOnly(t *testing.T) {
	cfg := normalizeConfig(Config{PoorPerformerFilter: true, PoorPerformerMinTrades: 2, PoorPerformerMaxNetProfit: -300})
	trades := []Trade{
		{Code: "600001", NetProfit: -120},
		{Code: "600001", NetProfit: -200},
		{Code: "000001", NetProfit: -500},
	}
	if !isPoorPerformer("600001", trades, cfg) {
		t.Fatalf("expected 600001 to be filtered")
	}
	if isPoorPerformer("000001", trades, cfg) {
		t.Fatalf("did not expect 000001 to be filtered with only one closed trade")
	}
	if isPoorPerformer("300001", trades, cfg) {
		t.Fatalf("did not expect unseen code to be filtered")
	}
}

func TestRepeatedStopLossesMarkPoorPerformer(t *testing.T) {
	cfg := normalizeConfig(Config{RepeatedStopLossFilter: true, PoorPerformerMinTrades: 2, PoorPerformerMaxNetProfit: -300})
	trades := []Trade{
		{Code: "600001", ExitReason: "stop_loss", NetProfit: -120},
		{Code: "600001", ExitReason: "stop_loss", NetProfit: -80},
		{Code: "000001", ExitReason: "stop_loss", NetProfit: -500},
		{Code: "300001", ExitReason: "max_hold_days", NetProfit: -120},
		{Code: "300001", ExitReason: "max_hold_days", NetProfit: -80},
	}
	if !isPoorPerformer("600001", trades, cfg) {
		t.Fatalf("expected repeated stop-loss stock to be filtered")
	}
	if isPoorPerformer("000001", trades, cfg) {
		t.Fatalf("did not expect one stop-loss stock to be filtered")
	}
	if isPoorPerformer("300001", trades, cfg) {
		t.Fatalf("did not expect repeated small non-stop-loss losses to be filtered")
	}
}

func TestRepeatedStopLossFilterCanBeDisabled(t *testing.T) {
	cfg := normalizeConfig(Config{PoorPerformerFilter: false, RepeatedStopLossFilter: false, PoorPerformerMinTrades: 2, PoorPerformerMaxNetProfit: -300})
	trades := []Trade{
		{Code: "600001", ExitReason: "stop_loss", NetProfit: -120},
		{Code: "600001", ExitReason: "stop_loss", NetProfit: -80},
	}

	if isPoorPerformer("600001", trades, cfg) {
		t.Fatalf("did not expect repeated stop-loss stock to be filtered when disabled")
	}
}

func TestSingleLossFilterReason(t *testing.T) {
	cfg := normalizeConfig(Config{SingleLossFilter: true, SingleLossMaxNetProfit: -300, SingleLossMaxReturnPct: -5})
	trades := []Trade{
		{Code: "600001", ExitReason: "stop_loss", NetProfit: -305, NetReturnPct: -4.0},
	}
	if got := filterCandidateReason("600001", trades, cfg, 10, nil, nil); got != "single_large_stop_loss" {
		t.Fatalf("unexpected filter reason: %s", got)
	}
	if got := filterCandidateReason("000001", trades, cfg, 10, nil, nil); got != "" {
		t.Fatalf("unexpected other code filter reason: %s", got)
	}
}

func TestRecentStopLossCooldownReason(t *testing.T) {
	cfg := normalizeConfig(Config{StopLossCooldownDays: 30})
	cooldownUntil := map[string]int{"600001": 40}
	cooldownReason := map[string]string{"600001": "recent_stop_loss_cooldown"}
	if got := filterCandidateReason("600001", nil, cfg, 20, cooldownUntil, cooldownReason); got != "recent_stop_loss_cooldown" {
		t.Fatalf("unexpected cooldown reason: %s", got)
	}
	if got := filterCandidateReason("600001", nil, cfg, 41, cooldownUntil, cooldownReason); got != "" {
		t.Fatalf("unexpected expired cooldown reason: %s", got)
	}
}

func TestLargeStopLossUsesLongerCooldown(t *testing.T) {
	cfg := normalizeConfig(Config{CooldownDays: 5, StopLossCooldownDays: 30, LargeLossFilter: true, LargeLossMaxReturnPct: -6, LargeLossCooldownDays: 60})
	cooldownUntil := map[string]int{}
	cooldownReason := map[string]string{}

	applyLossCooldown("600001", 10, "stop_loss", -6.5, -320, cfg, cooldownUntil, cooldownReason)

	if got := cooldownUntil["600001"]; got != 70 {
		t.Fatalf("unexpected cooldown until: %d", got)
	}
	if got := cooldownReason["600001"]; got != "large_stop_loss_cooldown" {
		t.Fatalf("unexpected cooldown reason: %s", got)
	}
}

func TestLossStreakPauseFiltersNewBuys(t *testing.T) {
	cfg := normalizeConfig(Config{LossStreakPause: true, LossStreakThreshold: 2, LossStreakPauseDays: 5})
	streak, pauseUntil := updateLossStreak(-100, 10, cfg, 0, 0)
	streak, pauseUntil = updateLossStreak(-80, 11, cfg, streak, pauseUntil)

	if streak != 2 {
		t.Fatalf("unexpected loss streak: %d", streak)
	}
	if pauseUntil != 16 {
		t.Fatalf("unexpected pause until: %d", pauseUntil)
	}
	if got := lossStreakPauseReason(cfg, 12, pauseUntil); got != "loss_streak_pause" {
		t.Fatalf("unexpected pause reason: %s", got)
	}
	if got := lossStreakPauseReason(cfg, 16, pauseUntil); got != "" {
		t.Fatalf("unexpected expired pause reason: %s", got)
	}
}

func TestLossStreakPauseResetsAfterWin(t *testing.T) {
	cfg := normalizeConfig(Config{LossStreakPause: true, LossStreakThreshold: 2, LossStreakPauseDays: 5})
	streak, pauseUntil := updateLossStreak(-100, 10, cfg, 0, 0)
	streak, pauseUntil = updateLossStreak(50, 11, cfg, streak, pauseUntil)
	streak, pauseUntil = updateLossStreak(-80, 12, cfg, streak, pauseUntil)

	if streak != 1 {
		t.Fatalf("unexpected loss streak after win reset: %d", streak)
	}
	if pauseUntil != 0 {
		t.Fatalf("unexpected pause after win reset: %d", pauseUntil)
	}
	if got := lossStreakPauseReason(cfg, 13, pauseUntil); got != "" {
		t.Fatalf("unexpected pause reason after win reset: %s", got)
	}
}

func TestFilterStatsListOrdering(t *testing.T) {
	stats := map[string]*FilterStat{}
	recordFilter(stats, "600001", "single_large_stop_loss", "20260101")
	recordFilter(stats, "600001", "single_large_stop_loss", "20260102")
	recordFilter(stats, "000001", "poor_performer", "20260103")
	items := filterStatsList(stats)
	if len(items) != 2 || items[0].Code != "600001" || items[0].Count != 2 || items[0].LastDate != "20260102" {
		t.Fatalf("unexpected filter stats: %+v", items)
	}
}

func TestRiskExitCanHappenBeforeMinHoldDays(t *testing.T) {
	cfg := normalizeConfig(Config{MinHoldDays: 5, MaxHoldDays: 10, StopLossPct: 3, TakeProfitPct: 20})
	pos := position{BuyPrice: 10}
	if !shouldSellPosition(pos, 9.6, 1, cfg) {
		t.Fatalf("expected risk exit before min hold days")
	}
	if got := exitReason(pos, 9.6, 1, cfg); got != "stop_loss" {
		t.Fatalf("unexpected exit reason: %s", got)
	}
	if shouldSellPosition(pos, 10.2, 1, cfg) {
		t.Fatalf("did not expect normal position to exit before min hold days")
	}
}

func TestRunMaxHoldDaysExit(t *testing.T) {
	bars := fixtureBars(80)
	result := Run(bars, Config{InitialCash: 10000, TopN: 1, MinScore: 60, MaxPositionRatio: 0.5, MinTradeAmount: 1000, MinHoldDays: 3, MaxHoldDays: 4, StopLossPct: 30, TakeProfitPct: 30})
	if result.Trades == 0 {
		t.Fatalf("expected max hold trades, got %+v", result)
	}
	for _, trade := range result.TradesList {
		if trade.ExitReason == "max_hold_days" && trade.HoldingDays != 4 {
			t.Fatalf("unexpected max hold trade: %+v", trade)
		}
	}
}

func fixtureBars(days int) []domain.DailyBar {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	bars := make([]domain.DailyBar, 0, days*2)
	for i := 0; i < days; i++ {
		date := start.AddDate(0, 0, i).Format("20060102")
		priceA := 10.0 + float64(i)*0.12
		bars = append(bars, domain.DailyBar{Date: date, Code: "600001", Open: priceA + 0.03, High: priceA * 1.03, Low: priceA * 0.98, Close: priceA, PrevClose: priceA - 0.12, Change: 0.12, ChangePct: 1.0, Volume: 1000000, Amount: 12000000})
		priceB := 20.0
		bars = append(bars, domain.DailyBar{Date: date, Code: "000001", Open: priceB, High: priceB * 1.01, Low: priceB * 0.99, Close: priceB, PrevClose: priceB, Change: 0, ChangePct: 0, Volume: 1000000, Amount: 12000000})
	}
	return bars
}
