package quality

import (
	"testing"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestValidateSnapshotsFindsErrorsAndWarnings(t *testing.T) {
	report := ValidateSnapshots([]domain.StockSnapshot{
		{Date: "20260519", Code: "600001", Close: 10, High: 9, Low: 11},
		{Date: "20260519", Code: "600001", Close: 10, High: 11, Low: 9},
		{Date: "20260520", Code: "000001", Close: 0},
	}, Options{RequireIndicators: true, IndicatorWarnRatio: 0.5})
	if report.Errors == 0 {
		t.Fatalf("expected errors, got %+v", report)
	}
	if report.Warnings == 0 {
		t.Fatalf("expected warnings, got %+v", report)
	}
}

func TestValidateBarsFindsDuplicate(t *testing.T) {
	report := ValidateBars([]domain.DailyBar{
		{Date: "20260519", Code: "000001", Open: 10, Close: 10, High: 11, Low: 9},
		{Date: "20260519", Code: "000001", Open: 10, Close: 10, High: 11, Low: 9},
	}, Options{})
	if report.Errors != 0 {
		t.Fatalf("expected no errors, got %+v", report)
	}
	if report.Warnings == 0 {
		t.Fatalf("expected duplicate warning, got %+v", report)
	}
}
