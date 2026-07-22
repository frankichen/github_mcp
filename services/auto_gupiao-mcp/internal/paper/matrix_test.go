package paper

import "testing"

func TestRunMatrixReturnsDefaultCasesAndMetrics(t *testing.T) {
	bars := fixtureBars(80)
	base := Config{
		InitialCash:            10000,
		TopN:                   1,
		MinScore:               60,
		MaxPositionRatio:       0.5,
		MinTradeAmount:         1000,
		MinHoldDays:            3,
		MaxHoldDays:            7,
		StopLossPct:            30,
		TakeProfitPct:          8,
		StrategyProfile:        "full",
		RepeatedStopLossFilter: true,
	}
	metrics := RunMatrix(bars, base, nil)

	if len(metrics) != len(DefaultMatrixCases()) {
		t.Fatalf("unexpected matrix case count: %d", len(metrics))
	}
	if metrics[0].Name != "baseline" {
		t.Fatalf("unexpected first case: %s", metrics[0].Name)
	}
	baseResult := Run(bars, base)
	if metrics[0].TotalReturnPct != baseResult.TotalReturnPct || metrics[0].Trades != baseResult.Trades {
		t.Fatalf("baseline should match base run: metric=%+v base=%+v", metrics[0], baseResult)
	}
	expectedNames := map[string]bool{
		"baseline":                  true,
		"atr_guard":                 true,
		"drawdown20_guard":          true,
		"daily_drop_guard":          true,
		"close_ma20_distance_guard": true,
		"overheat_guard":            true,
		"light_combined_guard":      true,
	}
	for _, metric := range metrics {
		if !expectedNames[metric.Name] {
			t.Fatalf("unexpected matrix case: %s", metric.Name)
		}
		if metric.Trades == 0 {
			t.Fatalf("expected trades for %s: %+v", metric.Name, metric)
		}
	}
}

func TestRunMatrixReportsLatestSellDateAndOpenPositions(t *testing.T) {
	bars := fixtureBars(80)
	metrics := RunMatrix(bars, Config{
		InitialCash:       10000,
		TopN:              1,
		MinScore:          60,
		MaxPositionRatio:  0.5,
		MinTradeAmount:    1000,
		MinHoldDays:       3,
		MaxHoldDays:       100,
		StopLossPct:       30,
		TakeProfitPct:     8,
		StrategyProfile:   "full",
		AllowMultipleBuys: false,
	}, nil)

	if metrics[0].LatestSellDate == "" {
		t.Fatalf("expected latest sell date: %+v", metrics[0])
	}
	if metrics[0].OpenPositions == 0 {
		t.Fatalf("expected open positions: %+v", metrics[0])
	}
}
