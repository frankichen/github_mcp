package serverrunner

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/frankichen/auto_gupiao/internal/appconfig"
	"github.com/frankichen/auto_gupiao/internal/notify/webhook"
)

func TestRunCSVConfig(t *testing.T) {
	result, err := Run(context.Background(), appconfig.Config{
		Data: appconfig.DataConfig{
			Source:   "csv",
			BarsFile: "../../examples/sample_daily_bars.csv",
		},
		Strategy: appconfig.StrategyConfig{
			Cash:             10000,
			TopN:             1,
			MinScore:         60,
			MaxPositionRatio: 0.3,
			PaperSellPrice:   "open",
		},
		Report: appconfig.ReportConfig{
			ReportDir:    t.TempDir(),
			ReportPrefix: "server_test",
			TradeDate:    "20260519",
		},
		Runtime: appconfig.RuntimeConfig{LockFile: filepath.Join(t.TempDir(), "daily.lock")},
	})
	if err != nil {
		t.Fatalf("Run failed: %v", err)
	}
	if result.Bars.Rows == 0 || result.Daily.Paper.Days == 0 || result.Daily.Report.MarkdownPath == "" {
		t.Fatalf("unexpected result: %+v", result)
	}
	if result.Notified {
		t.Fatalf("did not expect notification: %+v", result)
	}
}

func TestRunWritesLatestJSON(t *testing.T) {
	dir := t.TempDir()
	latestFile := filepath.Join(dir, "latest.json")
	result, err := Run(context.Background(), appconfig.Config{
		Data: appconfig.DataConfig{
			Source:   "csv",
			BarsFile: "../../examples/sample_daily_bars.csv",
		},
		Strategy: appconfig.StrategyConfig{
			Cash:             10000,
			TopN:             1,
			MinScore:         60,
			MaxPositionRatio: 0.3,
			PaperSellPrice:   "open",
		},
		Report: appconfig.ReportConfig{
			ReportDir:     dir,
			ReportPrefix:  "latest_test",
			TradeDate:     "20260519",
			PublicBaseURL: "https://example.com/reports",
			LatestFile:    latestFile,
		},
		Runtime: appconfig.RuntimeConfig{LockFile: filepath.Join(dir, "daily.lock")},
	})
	if err != nil {
		t.Fatalf("Run failed: %v", err)
	}
	content, err := os.ReadFile(latestFile)
	if err != nil {
		t.Fatalf("read latest json: %v", err)
	}
	var latest LatestReport
	if err := json.Unmarshal(content, &latest); err != nil {
		t.Fatalf("parse latest json: %v", err)
	}
	if latest.TradeDate != "20260519" || latest.MarkdownURL == "" || latest.Rows != result.Bars.Rows {
		t.Fatalf("unexpected latest: %+v", latest)
	}
}

func TestRunCSVConfigWithWebhookNotify(t *testing.T) {
	requests := 0
	var payload webhook.Payload
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.Header.Get(webhook.HeaderEvent) != dailyReportEvent {
			t.Fatalf("unexpected event: %s", r.Header.Get(webhook.HeaderEvent))
		}
		if r.Header.Get(webhook.HeaderIdempotencyKey) == "" {
			t.Fatalf("missing idempotency key")
		}
		if r.Header.Get(webhook.HeaderSignature) == "" {
			t.Fatalf("missing signature")
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("decode payload: %v", err)
		}
		_, _ = w.Write([]byte("ok"))
	}))
	defer server.Close()
	dir := t.TempDir()
	result, err := Run(context.Background(), appconfig.Config{
		Data: appconfig.DataConfig{
			Source:   "csv",
			BarsFile: "../../examples/sample_daily_bars.csv",
		},
		Strategy: appconfig.StrategyConfig{
			Cash:             10000,
			TopN:             1,
			MinScore:         60,
			MaxPositionRatio: 0.3,
			PaperSellPrice:   "open",
		},
		Report: appconfig.ReportConfig{
			ReportDir:    dir,
			ReportPrefix: "server_webhook_test",
			TradeDate:    "20260519",
		},
		Runtime: appconfig.RuntimeConfig{LockFile: filepath.Join(dir, "daily.lock")},
		Notify: appconfig.NotifyConfig{Webhook: appconfig.WebhookConfig{
			Enabled: true,
			URL:     server.URL,
			Secret:  "secret",
		}},
	})
	if err != nil {
		t.Fatalf("Run failed: %v", err)
	}
	if !result.WebhookNotified || requests != 1 || payload.TradeDate != "20260519" {
		t.Fatalf("unexpected webhook result=%+v requests=%d payload=%+v", result, requests, payload)
	}
}

func TestRunLockRejectsSecondRun(t *testing.T) {
	dir := t.TempDir()
	lockFile := filepath.Join(dir, "daily.lock")
	lock, err := acquireRunLock(lockFile)
	if err != nil {
		t.Fatalf("acquire lock: %v", err)
	}
	defer lock.Release()
	_, err = Run(context.Background(), appconfig.Config{
		Data: appconfig.DataConfig{
			Source:   "csv",
			BarsFile: "../../examples/sample_daily_bars.csv",
		},
		Strategy: appconfig.StrategyConfig{
			Cash:             10000,
			TopN:             1,
			MinScore:         60,
			MaxPositionRatio: 0.3,
			PaperSellPrice:   "open",
		},
		Report: appconfig.ReportConfig{
			ReportDir:    dir,
			ReportPrefix: "lock_test",
			TradeDate:    "20260519",
		},
		Runtime: appconfig.RuntimeConfig{LockFile: lockFile},
	})
	if err == nil || !contains(err.Error(), "already active") {
		t.Fatalf("expected lock error, got %v", err)
	}
}

func TestRunAddsReportRunSuffixWhenPublicBaseURLSet(t *testing.T) {
	dir := t.TempDir()
	result, err := Run(context.Background(), appconfig.Config{
		Data: appconfig.DataConfig{
			Source:   "csv",
			BarsFile: "../../examples/sample_daily_bars.csv",
		},
		Strategy: appconfig.StrategyConfig{
			Cash:             10000,
			TopN:             1,
			MinScore:         60,
			MaxPositionRatio: 0.3,
			PaperSellPrice:   "open",
		},
		Report: appconfig.ReportConfig{
			ReportDir:     dir,
			ReportPrefix:  "daily_20260520",
			TradeDate:     "20260520",
			PublicBaseURL: "https://example.com/reports",
		},
		Runtime: appconfig.RuntimeConfig{LockFile: filepath.Join(dir, "daily.lock")},
	})
	if err != nil {
		t.Fatalf("Run failed: %v", err)
	}
	base := filepath.Base(result.Daily.Report.MarkdownPath)
	if base == "daily_20260520.md" || !contains(base, "daily_20260520_") {
		t.Fatalf("expected cache-busted report name, got %s", base)
	}
	if !contains(filepath.Base(result.Daily.Report.TradesCSV), "daily_20260520_") || !contains(filepath.Base(result.Daily.Report.EquityCSV), "daily_20260520_") {
		t.Fatalf("expected cache-busted csv names: %+v", result.Daily.Report)
	}
}

func TestRunCanDisableReportRunSuffix(t *testing.T) {
	cacheBust := false
	dir := t.TempDir()
	result, err := Run(context.Background(), appconfig.Config{
		Data: appconfig.DataConfig{
			Source:   "csv",
			BarsFile: "../../examples/sample_daily_bars.csv",
		},
		Strategy: appconfig.StrategyConfig{
			Cash:             10000,
			TopN:             1,
			MinScore:         60,
			MaxPositionRatio: 0.3,
			PaperSellPrice:   "open",
		},
		Report: appconfig.ReportConfig{
			ReportDir:     dir,
			ReportPrefix:  "daily_20260520",
			TradeDate:     "20260520",
			PublicBaseURL: "https://example.com/reports",
			CacheBust:     &cacheBust,
		},
		Runtime: appconfig.RuntimeConfig{LockFile: filepath.Join(dir, "daily.lock")},
	})
	if err != nil {
		t.Fatalf("Run failed: %v", err)
	}
	if filepath.Base(result.Daily.Report.MarkdownPath) != "daily_20260520.md" {
		t.Fatalf("expected stable report name, got %+v", result.Daily.Report)
	}
}

func TestRunCSVConfigWithDingTalkNotify(t *testing.T) {
	requests := 0
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.URL.Query().Get("timestamp") != "" || r.URL.Query().Get("sign") != "" {
			t.Fatalf("secret is empty, should not sign request: %s", r.URL.RawQuery)
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("decode payload: %v", err)
		}
		_, _ = w.Write([]byte("{\"errcode\":0,\"errmsg\":\"ok\"}"))
	}))
	defer server.Close()
	dir := t.TempDir()
	result, err := Run(context.Background(), appconfig.Config{
		Data: appconfig.DataConfig{
			Source:   "csv",
			BarsFile: "../../examples/sample_daily_bars.csv",
		},
		Strategy: appconfig.StrategyConfig{
			Cash:             10000,
			TopN:             1,
			MinScore:         60,
			MaxPositionRatio: 0.3,
			PaperSellPrice:   "open",
		},
		Report: appconfig.ReportConfig{
			ReportDir:    dir,
			ReportPrefix: "server_notify_test",
			TradeDate:    "20260519",
		},
		Runtime: appconfig.RuntimeConfig{LockFile: filepath.Join(dir, "daily.lock")},
		Notify: appconfig.NotifyConfig{DingTalk: appconfig.DingTalkConfig{
			Enabled: true,
			Webhook: server.URL + "/robot/send?token=abc",
		}},
	})
	if err != nil {
		t.Fatalf("Run failed: %v", err)
	}
	if !result.Notified || requests != 1 {
		t.Fatalf("expected one notification, result=%+v requests=%d", result, requests)
	}
	if payload["msgtype"] != "markdown" {
		t.Fatalf("unexpected payload: %+v", payload)
	}
}

func TestBuildDingTalkMarkdown(t *testing.T) {
	result := Result{}
	result.Bars.Rows = 2
	result.Bars.Codes = []string{"000001", "600000"}
	result.Bars.StartDate = "20250102"
	result.Bars.EndDate = "20250103"
	result.Daily.Paper.InitialCash = 10000
	result.Daily.Paper.FinalEquity = 10100
	result.Daily.Paper.TotalReturnPct = 1
	result.Daily.Paper.MaxDrawdownPct = 0.5
	result.Daily.Report.MarkdownPath = "reports/a.md"
	result.Daily.Report.TradesCSV = "reports/a_trades.csv"
	result.Daily.Report.EquityCSV = "reports/a_equity.csv"
	text := BuildDingTalkMarkdown(result, appconfig.Config{Report: appconfig.ReportConfig{TradeDate: "20260519"}})
	if text == "" || !contains(text, "A股观察盘日报") || !contains(text, "000001") || !contains(text, "reports/a.md") {
		t.Fatalf("unexpected markdown: %s", text)
	}
}

func TestBuildDingTalkMarkdownWithPublicBaseURL(t *testing.T) {
	result := Result{}
	result.Bars.Rows = 2
	result.Bars.Codes = []string{"000001"}
	result.Bars.StartDate = "20250102"
	result.Bars.EndDate = "20250103"
	result.Daily.Paper.InitialCash = 10000
	result.Daily.Paper.FinalEquity = 10100
	result.Daily.Report.MarkdownPath = "reports/daily/a.md"
	result.Daily.Report.TradesCSV = "reports/daily/a_trades.csv"
	result.Daily.Report.EquityCSV = "reports/daily/a_equity.csv"
	text := BuildDingTalkMarkdown(result, appconfig.Config{
		Report: appconfig.ReportConfig{
			TradeDate:     "20260519",
			ReportDir:     "reports",
			PublicBaseURL: "https://example.com/auto_gupiao_reports",
		},
	})
	if !contains(text, "https://example.com/auto_gupiao_reports/daily/a.md") {
		t.Fatalf("expected public markdown link, got: %s", text)
	}
	if !contains(text, "https://example.com/auto_gupiao_reports/daily/a_trades.csv") {
		t.Fatalf("expected public trades link, got: %s", text)
	}
	if !contains(text, "https://example.com/auto_gupiao_reports/daily/a_equity.csv") {
		t.Fatalf("expected public equity link, got: %s", text)
	}
}

func TestNormalizeRiskControlDefaults(t *testing.T) {
	cfg := normalize(appconfig.Config{})

	if !boolValue(cfg.Strategy.PoorPerformerFilter) {
		t.Fatalf("expected poor performer filter default true")
	}
	if !boolValue(cfg.Strategy.RepeatedStopLossFilter) {
		t.Fatalf("expected repeated stop loss filter default true")
	}
	if !boolValue(cfg.Strategy.SingleLossFilter) {
		t.Fatalf("expected single loss filter default true")
	}
	if boolValue(cfg.Strategy.LargeLossFilter) {
		t.Fatalf("expected large loss filter default false")
	}
	if cfg.Strategy.LargeLossCooldownDays != 15 {
		t.Fatalf("unexpected large loss cooldown days: %d", cfg.Strategy.LargeLossCooldownDays)
	}
	if boolValue(cfg.Strategy.LossStreakPause) {
		t.Fatalf("expected loss streak pause default false")
	}
	if cfg.Strategy.LossStreakPauseDays != 2 {
		t.Fatalf("unexpected loss streak pause days: %d", cfg.Strategy.LossStreakPauseDays)
	}
}

func TestNormalizePreservesExplicitFalseRiskControls(t *testing.T) {
	disabled := false
	cfg := normalize(appconfig.Config{Strategy: appconfig.StrategyConfig{
		PoorPerformerFilter:    &disabled,
		RepeatedStopLossFilter: &disabled,
		SingleLossFilter:       &disabled,
		LargeLossFilter:        &disabled,
		LossStreakPause:        &disabled,
	}})

	if boolValue(cfg.Strategy.PoorPerformerFilter) {
		t.Fatalf("expected explicit poor performer false to be preserved")
	}
	if boolValue(cfg.Strategy.RepeatedStopLossFilter) {
		t.Fatalf("expected explicit repeated stop loss false to be preserved")
	}
	if boolValue(cfg.Strategy.SingleLossFilter) {
		t.Fatalf("expected explicit single loss false to be preserved")
	}
	if boolValue(cfg.Strategy.LargeLossFilter) {
		t.Fatalf("expected explicit large loss false to be preserved")
	}
	if boolValue(cfg.Strategy.LossStreakPause) {
		t.Fatalf("expected explicit loss streak pause false to be preserved")
	}
}

func contains(s, sub string) bool {
	return len(sub) == 0 || (len(s) >= len(sub) && (s == sub || contains(s[1:], sub) || s[:len(sub)] == sub))
}
