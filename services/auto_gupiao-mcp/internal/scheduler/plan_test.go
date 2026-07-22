package scheduler

import "testing"

func TestBuildPlanDefaults(t *testing.T) {
	plan := BuildPlan(Config{TradeDate: "2026-05-19"})
	if plan.TradeDate != "20260519" {
		t.Fatalf("unexpected trade date: %+v", plan)
	}
	if plan.NextTradeDate != "20260520" {
		t.Fatalf("unexpected next date: %+v", plan)
	}
	if len(plan.Tasks) != 4 {
		t.Fatalf("unexpected tasks: %+v", plan.Tasks)
	}
	if plan.Tasks[0].Action != "select" || plan.Tasks[2].Action != "simulate_sell" {
		t.Fatalf("unexpected task actions: %+v", plan.Tasks)
	}
}

func TestBuildPlanCustomTimes(t *testing.T) {
	plan := BuildPlan(Config{TradeDate: "20260519", NextTradeDate: "20260521", SelectTime: "13:55", BuyTime: "14:03", SellTime: "09:58", ReportTime: "15:10"})
	if plan.Tasks[0].Time != "20260519 13:55" {
		t.Fatalf("unexpected select time: %+v", plan.Tasks[0])
	}
	if plan.Tasks[2].Time != "20260521 09:58" {
		t.Fatalf("unexpected sell time: %+v", plan.Tasks[2])
	}
}
