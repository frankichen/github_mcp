package daily

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestRunPaperReport(t *testing.T) {
	dir := t.TempDir()
	bars := fixtureBars(80)
	result, err := RunPaperReport(bars, Config{
		InitialCash:      10000,
		TopN:             1,
		MinScore:         60,
		MaxPositionRatio: 0.3,
		ReportDir:        dir,
		ReportPrefix:     "daily_test",
		ReportDate:       "20260519",
	})
	if err != nil {
		t.Fatalf("RunPaperReport failed: %v", err)
	}
	if result.Paper.Days != 80 {
		t.Fatalf("unexpected paper days: %+v", result.Paper)
	}
	paths := []string{result.Report.MarkdownPath, result.Report.TradesCSV, result.Report.EquityCSV}
	for _, path := range paths {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("missing report file %s: %v", path, err)
		}
		if info.Size() == 0 {
			t.Fatalf("empty report file %s", path)
		}
	}
	if filepath.Base(result.Report.MarkdownPath) != "daily_test.md" {
		t.Fatalf("unexpected manifest: %+v", result.Report)
	}
}

func fixtureBars(days int) []domain.DailyBar {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	bars := make([]domain.DailyBar, 0, days)
	for i := 0; i < days; i++ {
		date := start.AddDate(0, 0, i).Format("20060102")
		price := 10.0 + float64(i)*0.12
		bars = append(bars, domain.DailyBar{
			Date:      date,
			Code:      "000001",
			Open:      price + 0.03,
			High:      price * 1.03,
			Low:       price * 0.98,
			Close:     price,
			PrevClose: price - 0.12,
			Change:    0.12,
			ChangePct: 1.0,
			Volume:    1000000,
			Amount:    12000000,
		})
	}
	return bars
}
