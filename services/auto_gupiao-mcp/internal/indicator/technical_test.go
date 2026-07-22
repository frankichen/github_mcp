package indicator

import (
	"testing"
	"time"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestEnrichSnapshotsCalculatesTechnicalIndicators(t *testing.T) {
	history := make([]domain.DailyBar, 0, 61)
	for i := 1; i <= 61; i++ {
		history = append(history, domain.DailyBar{
			Code:  "600001.SH",
			Date:  date(i),
			Close: float64(i),
		})
	}
	snapshots := []domain.StockSnapshot{{Code: "600001.SH", Date: date(61), Close: 61}}

	got := EnrichSnapshots(snapshots, history)
	if got[0].MA5 != 59 || got[0].MA20 != 51.5 || got[0].MA60 != 31.5 {
		t.Fatalf("unexpected moving averages: %+v", got[0])
	}
	if got[0].RSI6 != 100 {
		t.Fatalf("unexpected rsi: %v", got[0].RSI6)
	}
	if got[0].FiveDayPct <= 0 || got[0].TwentyDayPct <= 0 {
		t.Fatalf("expected positive returns: %+v", got[0])
	}
}

func TestRSIHandlesFlatSeries(t *testing.T) {
	history := []domain.DailyBar{
		{Code: "000001.SZ", Date: "20260101", Close: 10},
		{Code: "000001.SZ", Date: "20260102", Close: 10},
		{Code: "000001.SZ", Date: "20260103", Close: 10},
		{Code: "000001.SZ", Date: "20260104", Close: 10},
		{Code: "000001.SZ", Date: "20260105", Close: 10},
		{Code: "000001.SZ", Date: "20260106", Close: 10},
		{Code: "000001.SZ", Date: "20260107", Close: 10},
	}
	got := BuildTechnicals(history)["000001.SZ|20260107"]
	if got.RSI6 != 50 {
		t.Fatalf("expected flat RSI to be 50, got %v", got.RSI6)
	}
}

func date(day int) string {
	return time.Date(2026, time.March, 1, 0, 0, 0, 0, time.UTC).
		AddDate(0, 0, day-1).
		Format("20060102")
}
