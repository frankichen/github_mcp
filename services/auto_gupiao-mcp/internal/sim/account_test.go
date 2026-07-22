package sim

import (
	"testing"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestAccountBuyAndSellAll(t *testing.T) {
	account := NewAccount(10000, domain.DefaultCostModel())
	candidate := domain.Candidate{
		Snapshot:        domain.StockSnapshot{Date: "20260518", Code: "600001.SH", Name: "测试银行", Close: 10},
		SuggestedShares: 300,
		EstimatedCost:   3000,
	}
	if _, err := account.BuyCandidate(candidate); err != nil {
		t.Fatalf("buy candidate: %v", err)
	}
	if account.Cash >= 10000 || len(account.Positions) != 1 {
		t.Fatalf("unexpected account after buy: %+v", account)
	}
	errs := account.SellAll([]domain.StockSnapshot{{Date: "20260519", Code: "600001.SH", Name: "测试银行", Close: 10.5}})
	if len(errs) != 0 {
		t.Fatalf("sell all errors: %+v", errs)
	}
	report := account.Report(nil)
	if len(report.Positions) != 0 {
		t.Fatalf("expected no positions: %+v", report)
	}
	if len(report.Fills) != 2 {
		t.Fatalf("expected buy and sell fills: %+v", report)
	}
	if report.RealizedProfit <= 0 {
		t.Fatalf("expected realized profit, got %+v", report)
	}
}

func TestAccountRejectsInvalidLotSize(t *testing.T) {
	account := NewAccount(10000, domain.DefaultCostModel())
	_, err := account.Buy(Order{Date: "20260518", Code: "600001.SH", Price: 10, Shares: 50})
	if err == nil {
		t.Fatalf("expected lot size error")
	}
}
