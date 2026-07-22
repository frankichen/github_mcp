package selector

import (
	"testing"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestSelectFiltersUntradableAndUnaffordable(t *testing.T) {
	snapshots := []domain.StockSnapshot{
		{Code: "600001", Name: "good", Close: 9.8, MA5: 9.7, MA20: 9.4, MA60: 8.9, VolumeRatio: 1.8, TurnoverRate: 5, RSI6: 58, ChangePct: 2.1, FiveDayPct: 4, ROE: 12, NetProfitGrowth: 10, PB: 2.1},
		{Code: "600002", Name: "st", Close: 5, ST: true},
		{Code: "600003", Name: "expensive", Close: 1000, MA20: 990, TurnoverRate: 4},
	}

	got := Select(snapshots, DefaultConfig())
	if len(got) != 1 {
		t.Fatalf("expected one candidate, got %d", len(got))
	}
	if got[0].Snapshot.Code != "600001" {
		t.Fatalf("unexpected candidate: %s", got[0].Snapshot.Code)
	}
	if got[0].SuggestedShares%domain.LotSize != 0 || got[0].SuggestedShares <= 0 {
		t.Fatalf("invalid shares: %d", got[0].SuggestedShares)
	}
}

func TestScoreRewardsMomentumButPenalizesOverheated(t *testing.T) {
	stable := Score(domain.StockSnapshot{Close: 10, MA5: 10.1, MA20: 9.8, MA60: 9.5, VolumeRatio: 1.8, TurnoverRate: 5, RSI6: 60, ChangePct: 2, FiveDayPct: 5, ROE: 10, NetProfitGrowth: 10, PB: 2})
	overheated := Score(domain.StockSnapshot{Close: 10, MA5: 10.1, MA20: 9.8, MA60: 9.5, VolumeRatio: 5, TurnoverRate: 5, RSI6: 90, ChangePct: 9, TwentyDayPct: 45, PB: 20})

	if stable.Score <= overheated.Score {
		t.Fatalf("expected stable setup to score higher: stable=%v overheated=%v", stable.Score, overheated.Score)
	}
}

func TestStrictEntryAllowsOnlyStrongTrend(t *testing.T) {
	strong := domain.StockSnapshot{Code: "600001", Name: "strong", Close: 10.5, MA5: 10.2, MA20: 9.8, MA60: 9.2, VolumeRatio: 1.8, TurnoverRate: 5, RSI6: 60, ChangePct: 2, FiveDayPct: 5, ROE: 10, NetProfitGrowth: 10, PB: 2}
	weakTrend := strong
	weakTrend.Code = "600002"
	weakTrend.MA5 = 9.7
	overheated := strong
	overheated.Code = "600003"
	overheated.RSI6 = 90
	overheated.VolumeRatio = 5
	got := Select([]domain.StockSnapshot{strong, weakTrend, overheated}, ScorerConfig{Cash: 10000, MaxPositionRatio: 0.8, TopN: 5, MinScore: 60, StrictEntry: true})
	if len(got) != 1 {
		t.Fatalf("expected one strict candidate, got %+v", got)
	}
	if got[0].Snapshot.Code != "600001" {
		t.Fatalf("unexpected strict candidate: %+v", got[0])
	}
}

func TestStrictEntryBlocksHighRiskEvenIfScorePasses(t *testing.T) {
	candidate := domain.Candidate{Score: 92, RiskLevel: "high", Reasons: []string{"close_above_ma20", "ma5_above_ma20", "ma20_above_ma60", "rsi_in_momentum_zone"}}
	if passesStrictEntry(candidate) {
		t.Fatalf("strict entry should block high risk candidate")
	}
}
