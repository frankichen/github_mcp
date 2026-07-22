package storage

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/frankichen/auto_gupiao/internal/dataset"
	"github.com/frankichen/auto_gupiao/internal/paper"
	"github.com/frankichen/auto_gupiao/internal/report"
)

func TestSQLiteStoreSaveAndReadRun(t *testing.T) {
	store := NewSQLiteStore(filepath.Join(t.TempDir(), "autogupiao.db"))
	pf := 1.5
	record := DailyRunRecord{
		GeneratedAt: time.Date(2026, 5, 20, 16, 0, 0, 0, time.UTC),
		TradeDate:   "20260520",
		Bars: dataset.Summary{
			Rows:      2,
			Codes:     []string{"000001", "600000"},
			StartDate: "20260519",
			EndDate:   "20260520",
		},
		Paper: paper.Result{
			InitialCash:        10000,
			FinalEquity:        10500,
			TotalReturnPct:     5,
			MaxDrawdownPct:     1.2,
			Days:               2,
			Trades:             1,
			Wins:               1,
			WinRatePct:         100,
			ProfitFactor:       &pf,
			MaxConsecutiveLoss: 0,
			DailyEquity: []paper.DailyEquity{
				{Date: "20260519", Cash: 10000, Equity: 10000},
				{Date: "20260520", Cash: 10500, Equity: 10500, DailyReturnPct: 5},
			},
			TradesList: []paper.Trade{
				{BuyDate: "20260519", SellDate: "20260520", Code: "000001", Shares: 100, BuyPrice: 10, SellPrice: 10.5, HoldingDays: 1, ExitReason: "take_profit", BuyCost: 1000, SellProceeds: 1050, NetProfit: 50, NetReturnPct: 5, Score: 90, RiskLevel: "low", Reasons: []string{"test"}},
			},
		},
		Report:     report.Manifest{MarkdownPath: "reports/a.md", TradesCSV: "reports/a_trades.csv", EquityCSV: "reports/a_equity.csv"},
		ReportURLs: ReportURLs{MarkdownURL: "https://example.com/a.md", TradesURL: "https://example.com/a_trades.csv", EquityURL: "https://example.com/a_equity.csv"},
	}
	runID, err := store.SaveDailyRun(context.Background(), record)
	if err != nil {
		t.Fatalf("SaveDailyRun failed: %v", err)
	}
	if runID <= 0 {
		t.Fatalf("invalid run id: %d", runID)
	}
	runs, err := store.ListRuns(context.Background(), 10)
	if err != nil {
		t.Fatalf("ListRuns failed: %v", err)
	}
	if len(runs) != 1 || runs[0].FinalEquity != 10500 || !runs[0].ProfitFactor.Valid {
		t.Fatalf("unexpected runs: %+v", runs)
	}
	detail, err := store.GetRun(context.Background(), runID)
	if err != nil {
		t.Fatalf("GetRun failed: %v", err)
	}
	if len(detail.Equity) != 2 || len(detail.Trades) != 1 || len(detail.ByCode) != 1 || len(detail.ByExitReason) != 1 {
		t.Fatalf("unexpected detail: %+v", detail)
	}
}

func TestSQLiteStoreSaveAndReadRunNote(t *testing.T) {
	store := NewSQLiteStore(filepath.Join(t.TempDir(), "autogupiao.db"))
	runID, err := store.SaveDailyRun(context.Background(), DailyRunRecord{
		GeneratedAt: time.Date(2026, 5, 20, 16, 0, 0, 0, time.UTC),
		TradeDate:   "20260520",
		Bars:        dataset.Summary{Rows: 1, Codes: []string{"000001"}, StartDate: "20260520", EndDate: "20260520"},
		Paper:       paper.Result{InitialCash: 10000, FinalEquity: 10100, TotalReturnPct: 1, Days: 1, DailyEquity: []paper.DailyEquity{{Date: "20260520", Equity: 10100}}},
		RiskLevel:   "低",
		Conclusion:  "测试结论",
	})
	if err != nil {
		t.Fatalf("SaveDailyRun failed: %v", err)
	}
	if err := store.SaveRunNote(context.Background(), RunNote{RunID: runID, Status: NoteStatusChecked, Memo: "已查看"}); err != nil {
		t.Fatalf("SaveRunNote failed: %v", err)
	}
	note, err := store.GetRunNote(context.Background(), runID)
	if err != nil {
		t.Fatalf("GetRunNote failed: %v", err)
	}
	if note.Status != NoteStatusChecked || note.Memo != "已查看" || note.UpdatedAt == "" {
		t.Fatalf("unexpected note: %+v", note)
	}
}
