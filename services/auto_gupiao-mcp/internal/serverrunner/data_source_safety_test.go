package serverrunner

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/frankichen/auto_gupiao/internal/appconfig"
)

func TestRunAkshareFallbackToBarsFile(t *testing.T) {
	dir := t.TempDir()
	barsFile := filepath.Join(dir, "bars.csv")
	content, err := os.ReadFile("../../examples/sample_daily_bars.csv")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	if err := os.WriteFile(barsFile, content, 0644); err != nil {
		t.Fatalf("write fallback fixture: %v", err)
	}
	result, err := Run(context.Background(), appconfig.Config{
		Data: appconfig.DataConfig{Source: "akshare", Codes: []string{"000001"}, BarsFile: barsFile},
		Provider: appconfig.ProviderConfig{PythonPath: filepath.Join(dir, "missing-python"), AkshareFallbackToBarsFile: true, AkshareRequestDelayMS: 1, AkshareMaxConsecutiveFailures: 1},
		Strategy: appconfig.StrategyConfig{Cash: 10000, TopN: 1, MinScore: 60, MaxPositionRatio: 0.3, PaperSellPrice: "open"},
		Report: appconfig.ReportConfig{ReportDir: dir, ReportPrefix: "fallback_test", TradeDate: "20260519"},
		Runtime: appconfig.RuntimeConfig{LockFile: filepath.Join(dir, "daily.lock")},
	})
	if err != nil {
		t.Fatalf("Run fallback failed: %v", err)
	}
	if result.DataStatus != "cached_fallback" || result.DataStatusReason == "" || result.Bars.Rows == 0 {
		t.Fatalf("unexpected fallback result: %+v", result)
	}
}

func TestDataStatusText(t *testing.T) {
	if got := dataStatusText(Result{DataStatus: "fresh"}); got != "新拉取" {
		t.Fatalf("unexpected fresh status: %s", got)
	}
	if got := dataStatusText(Result{DataStatus: "cached_fallback", DataStatusReason: "fetch failed"}); !contains(got, "缓存兜底") {
		t.Fatalf("unexpected fallback status: %s", got)
	}
}
