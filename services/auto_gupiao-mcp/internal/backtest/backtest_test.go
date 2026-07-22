package backtest

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestRunProducesTradesAndMetrics(t *testing.T) {
	bars := make([]domain.DailyBar, 0)
	for i := 1; i <= 70; i++ {
		bars = append(bars, domain.DailyBar{
			Code:      "600001.SH",
			Date:      date(i),
			Open:      float64(10 + i),
			High:      float64(10+i) * 1.02,
			Low:       float64(10+i) * 0.98,
			Close:     float64(10 + i),
			PrevClose: float64(9 + i),
			ChangePct: 1.2,
			Volume:    10000,
			Amount:    1000000,
		})
		bars = append(bars, domain.DailyBar{
			Code:      "000001.SZ",
			Date:      date(i),
			Open:      20,
			High:      20,
			Low:       20,
			Close:     20,
			PrevClose: 20,
			ChangePct: 0,
			Volume:    10000,
			Amount:    1000000,
		})
	}

	result := Run(bars, Config{InitialCash: 10000, TopN: 1, MinScore: 60, MaxPositionRatio: 0.3})
	if result.Trades == 0 {
		t.Fatalf("expected trades, got %+v", result)
	}
	if result.FinalEquity <= 0 {
		t.Fatalf("invalid final equity: %+v", result)
	}
	if len(result.TradesList) != result.Trades {
		t.Fatalf("trade list mismatch: %+v", result)
	}
	if _, err := json.Marshal(result); err != nil {
		t.Fatalf("result should be JSON serializable: %v", err)
	}
}

func TestRunHandlesNoTrades(t *testing.T) {
	bars := []domain.DailyBar{
		{Code: "600001.SH", Date: "20260101", Open: 10, Close: 10},
		{Code: "600001.SH", Date: "20260102", Open: 10, Close: 10},
	}
	result := Run(bars, Config{InitialCash: 10000, MinScore: 99})
	if result.Trades != 0 || result.FinalEquity != 10000 {
		t.Fatalf("expected no trades and unchanged cash, got %+v", result)
	}
}

func date(day int) string {
	return time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC).
		AddDate(0, 0, day-1).
		Format("20060102")
}
