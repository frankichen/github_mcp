package serverrunner

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/appconfig"
	"github.com/frankichen/auto_gupiao/internal/daily"
	"github.com/frankichen/auto_gupiao/internal/data"
	"github.com/frankichen/auto_gupiao/internal/dataset"
	"github.com/frankichen/auto_gupiao/internal/domain"
	"github.com/frankichen/auto_gupiao/internal/marketdata/akshare"
	"github.com/frankichen/auto_gupiao/internal/marketdata/tushare"
	"github.com/frankichen/auto_gupiao/internal/quality"
	"github.com/frankichen/auto_gupiao/internal/report"
	"github.com/frankichen/auto_gupiao/internal/storage"
)

type Result struct {
	Bars             dataset.Summary `json:"bars"`
	Daily            daily.Result    `json:"daily"`
	DataStatus       string          `json:"data_status"`
	DataStatusReason string          `json:"data_status_reason,omitempty"`
	Notified         bool            `json:"notified"`
	WebhookNotified  bool            `json:"webhook_notified"`
	RunID            int64           `json:"run_id,omitempty"`
}

type barsLoadResult struct {
	Bars    []domain.DailyBar
	Summary dataset.Summary
	Status  string
	Reason  string
}

func Run(ctx context.Context, cfg appconfig.Config) (Result, error) {
	cfg = normalize(cfg)
	lock, err := acquireRunLock(cfg.Runtime.LockFile)
	if err != nil {
		return Result{}, err
	}
	defer lock.Release()

	generatedAt := time.Now()
	loaded, err := loadOrFetchBars(ctx, cfg)
	if err != nil {
		return Result{}, err
	}
	if err := handleQuality(quality.ValidateBars(loaded.Bars, quality.Options{MinRows: 1}), false); err != nil {
		return Result{}, err
	}
	dailyResult, err := daily.RunPaperReport(loaded.Bars, daily.Config{
		InitialCash:               cfg.Strategy.Cash,
		TopN:                      cfg.Strategy.TopN,
		MinScore:                  cfg.Strategy.MinScore,
		MaxPositionRatio:          cfg.Strategy.MaxPositionRatio,
		AllowMultipleBuys:         cfg.Strategy.AllowMultipleBuys,
		StrategyProfile:           cfg.Strategy.StrategyProfile,
		StrictEntry:               cfg.Strategy.StrictEntry,
		SellPriceMode:             cfg.Strategy.PaperSellPrice,
		MinTradeAmount:            cfg.Strategy.MinTradeAmount,
		MinHoldDays:               cfg.Strategy.MinHoldDays,
		MaxHoldDays:               cfg.Strategy.MaxHoldDays,
		StopLossPct:               cfg.Strategy.StopLossPct,
		TakeProfitPct:             cfg.Strategy.TakeProfitPct,
		CooldownDays:              cfg.Strategy.CooldownDays,
		StopLossCooldownDays:      cfg.Strategy.StopLossCooldownDays,
		PoorPerformerFilter:       boolValue(cfg.Strategy.PoorPerformerFilter),
		RepeatedStopLossFilter:    boolValue(cfg.Strategy.RepeatedStopLossFilter),
		PoorPerformerMinTrades:    cfg.Strategy.PoorPerformerMinTrades,
		PoorPerformerMaxNetProfit: cfg.Strategy.PoorPerformerMaxNetProfit,
		SingleLossFilter:          boolValue(cfg.Strategy.SingleLossFilter),
		SingleLossMaxNetProfit:    cfg.Strategy.SingleLossMaxNetProfit,
		SingleLossMaxReturnPct:    cfg.Strategy.SingleLossMaxReturnPct,
		LargeLossFilter:           boolValue(cfg.Strategy.LargeLossFilter),
		LargeLossMaxReturnPct:     cfg.Strategy.LargeLossMaxReturnPct,
		LargeLossCooldownDays:     cfg.Strategy.LargeLossCooldownDays,
		LossStreakPause:           boolValue(cfg.Strategy.LossStreakPause),
		LossStreakThreshold:       cfg.Strategy.LossStreakThreshold,
		LossStreakPauseDays:       cfg.Strategy.LossStreakPauseDays,
		ReportDir:                 cfg.Report.ReportDir,
		ReportPrefix:              cfg.Report.ReportPrefix,
		ReportDate:                cfg.Report.TradeDate,
	})
	if err != nil {
		return Result{}, err
	}
	result := Result{Bars: loaded.Summary, Daily: dailyResult, DataStatus: loaded.Status, DataStatusReason: loaded.Reason}
	if cfg.Database.Enabled {
		insights := report.BuildPaperInsights(dailyResult.Paper)
		runID, err := storage.NewSQLiteStore(cfg.Database.Path).SaveDailyRun(ctx, storage.DailyRunRecord{
			GeneratedAt: generatedAt,
			TradeDate:   cfg.Report.TradeDate,
			Bars:        loaded.Summary,
			Paper:       dailyResult.Paper,
			Report:      dailyResult.Report,
			ReportURLs: storage.ReportURLs{
				MarkdownURL: publicReportPath(cfg, dailyResult.Report.MarkdownPath),
				TradesURL:   publicReportPath(cfg, dailyResult.Report.TradesCSV),
				EquityURL:   publicReportPath(cfg, dailyResult.Report.EquityCSV),
			},
			RiskLevel:  insights.RiskLevel,
			Conclusion: insights.Conclusion,
		})
		if err != nil {
			return Result{}, err
		}
		result.RunID = runID
	}
	if err := writeLatestJSON(cfg, result, generatedAt); err != nil {
		return Result{}, err
	}
	if cfg.Notify.DingTalk.Enabled {
		if err := notifyDingTalk(ctx, cfg, result); err != nil {
			return Result{}, err
		}
		result.Notified = true
	}
	if cfg.Notify.Webhook.Enabled {
		if err := notifyWebhook(ctx, cfg, result, generatedAt); err != nil {
			return Result{}, err
		}
		result.WebhookNotified = true
	}
	return result, nil
}

func normalize(cfg appconfig.Config) appconfig.Config {
	now := time.Now()
	today := now.Format("20060102")
	if cfg.Data.Source == "" {
		cfg.Data.Source = "csv"
	}
	if cfg.Data.Universe == "" {
		cfg.Data.Universe = UniverseCustom
	}
	if cfg.Data.EndDate == "" {
		cfg.Data.EndDate = today
	}
	if cfg.Data.UniverseLimit <= 0 {
		cfg.Data.UniverseLimit = 30
	}
	if cfg.Data.MinUniversePrice <= 0 {
		cfg.Data.MinUniversePrice = 2
	}
	if cfg.Data.MaxUniversePrice <= 0 {
		cfg.Data.MaxUniversePrice = 80
	}
	if cfg.Data.MinUniverseAmount <= 0 {
		cfg.Data.MinUniverseAmount = 100000000
	}
	if cfg.Provider.PythonPath == "" {
		cfg.Provider.PythonPath = "python"
	}
	if cfg.Provider.AkshareScript == "" {
		cfg.Provider.AkshareScript = akshare.DefaultScriptPath
	}
	if cfg.Provider.AkshareCache == "" {
		cfg.Provider.AkshareCache = akshare.DefaultCacheDir
	}
	if cfg.Provider.AkshareLookbackDays <= 0 {
		cfg.Provider.AkshareLookbackDays = akshare.DefaultHistoryLookback
	}
	if cfg.Provider.AkshareRequestDelayMS <= 0 {
		cfg.Provider.AkshareRequestDelayMS = 1500
	}
	if cfg.Provider.AkshareMaxConsecutiveFailures <= 0 {
		cfg.Provider.AkshareMaxConsecutiveFailures = 3
	}
	if cfg.Strategy.Cash <= 0 {
		cfg.Strategy.Cash = 10000
	}
	if cfg.Strategy.TopN <= 0 {
		cfg.Strategy.TopN = 3
	}
	if cfg.Strategy.MinScore <= 0 {
		cfg.Strategy.MinScore = 90
	}
	if cfg.Strategy.MaxPositionRatio <= 0 {
		cfg.Strategy.MaxPositionRatio = 0.80
	}
	if cfg.Strategy.PaperSellPrice == "" {
		cfg.Strategy.PaperSellPrice = "open"
	}
	if cfg.Strategy.MinTradeAmount <= 0 {
		cfg.Strategy.MinTradeAmount = 3000
	}
	if cfg.Strategy.MinHoldDays <= 0 {
		cfg.Strategy.MinHoldDays = 3
	}
	if cfg.Strategy.MaxHoldDays <= 0 {
		cfg.Strategy.MaxHoldDays = 7
	}
	if cfg.Strategy.MaxHoldDays < cfg.Strategy.MinHoldDays {
		cfg.Strategy.MaxHoldDays = cfg.Strategy.MinHoldDays
	}
	if cfg.Strategy.StopLossPct <= 0 {
		cfg.Strategy.StopLossPct = 3.5
	}
	if cfg.Strategy.TakeProfitPct <= 0 {
		cfg.Strategy.TakeProfitPct = 8
	}
	if cfg.Strategy.CooldownDays <= 0 {
		cfg.Strategy.CooldownDays = 10
	}
	if cfg.Strategy.StopLossCooldownDays <= 0 {
		cfg.Strategy.StopLossCooldownDays = 30
	}
	if cfg.Strategy.PoorPerformerFilter == nil {
		cfg.Strategy.PoorPerformerFilter = boolPtr(true)
	}
	if cfg.Strategy.RepeatedStopLossFilter == nil {
		cfg.Strategy.RepeatedStopLossFilter = boolPtr(true)
	}
	if cfg.Strategy.PoorPerformerMinTrades <= 0 {
		cfg.Strategy.PoorPerformerMinTrades = 2
	}
	if cfg.Strategy.PoorPerformerMaxNetProfit >= 0 {
		cfg.Strategy.PoorPerformerMaxNetProfit = -300
	}
	if cfg.Strategy.SingleLossFilter == nil {
		cfg.Strategy.SingleLossFilter = boolPtr(true)
	}
	if cfg.Strategy.SingleLossMaxNetProfit >= 0 {
		cfg.Strategy.SingleLossMaxNetProfit = -300
	}
	if cfg.Strategy.SingleLossMaxReturnPct >= 0 {
		cfg.Strategy.SingleLossMaxReturnPct = -5
	}
	if cfg.Strategy.LargeLossFilter == nil {
		cfg.Strategy.LargeLossFilter = boolPtr(false)
	}
	if cfg.Strategy.LargeLossMaxReturnPct >= 0 {
		cfg.Strategy.LargeLossMaxReturnPct = -6.0
	}
	if cfg.Strategy.LargeLossCooldownDays <= 0 {
		cfg.Strategy.LargeLossCooldownDays = 15
	}
	if cfg.Strategy.LossStreakPause == nil {
		cfg.Strategy.LossStreakPause = boolPtr(false)
	}
	if cfg.Strategy.LossStreakThreshold <= 0 {
		cfg.Strategy.LossStreakThreshold = 4
	}
	if cfg.Strategy.LossStreakPauseDays <= 0 {
		cfg.Strategy.LossStreakPauseDays = 2
	}
	if cfg.Report.ReportDir == "" {
		cfg.Report.ReportDir = "reports"
	}
	if cfg.Report.TradeDate == "" {
		cfg.Report.TradeDate = today
	}
	if cfg.Report.ReportPrefix == "" {
		cfg.Report.ReportPrefix = "daily_" + cfg.Report.TradeDate
	}
	if reportCacheBustEnabled(cfg.Report) {
		cfg.Report.ReportPrefix = addReportRunSuffix(cfg.Report.ReportPrefix, now)
	}
	if cfg.Report.LatestFile == "" {
		cfg.Report.LatestFile = filepath.Join(cfg.Report.ReportDir, "latest.json")
	}
	if cfg.Database.Path == "" {
		cfg.Database.Path = "data/autogupiao.db"
	}
	if cfg.Dashboard.Address == "" {
		cfg.Dashboard.Address = ":8080"
	}
	if cfg.Runtime.LockFile == "" {
		cfg.Runtime.LockFile = "data/locks/daily.lock"
	}
	return cfg
}

func boolPtr(value bool) *bool {
	return &value
}

func boolValue(value *bool) bool {
	return value != nil && *value
}

func reportCacheBustEnabled(report appconfig.ReportConfig) bool {
	if report.CacheBust != nil {
		return *report.CacheBust
	}
	return strings.TrimSpace(report.PublicBaseURL) != ""
}

func addReportRunSuffix(prefix string, now time.Time) string {
	suffix := now.Format("150405")
	if strings.HasSuffix(prefix, "_"+suffix) {
		return prefix
	}
	return prefix + "_" + suffix
}

func loadOrFetchBars(ctx context.Context, cfg appconfig.Config) (barsLoadResult, error) {
	switch cfg.Data.Source {
	case "csv":
		if cfg.Data.BarsFile == "" {
			return barsLoadResult{}, fmt.Errorf("data.bars_file is required when data.source is csv")
		}
		bars, err := data.LoadDailyBarsCSVFile(cfg.Data.BarsFile)
		if err != nil {
			return barsLoadResult{}, err
		}
		return barsLoadResult{Bars: bars, Summary: dataset.SummarizeBars(bars, cfg.Data.BarsFile), Status: "fresh"}, nil
	case "akshare", "tushare":
		return fetchBarsWithFallback(ctx, cfg)
	default:
		return barsLoadResult{}, fmt.Errorf("unsupported data.source %q", cfg.Data.Source)
	}
}

func fetchBarsWithFallback(ctx context.Context, cfg appconfig.Config) (barsLoadResult, error) {
	if cfg.Data.BarsFile == "" {
		return barsLoadResult{}, fmt.Errorf("data.bars_file is required for fetched bars output")
	}
	provider, err := createProvider(cfg)
	if err != nil {
		return barsLoadResult{}, err
	}
	codes, err := resolveUniverseCodes(ctx, cfg, provider)
	if err != nil {
		return fallbackBars(cfg, fmt.Sprintf("resolve universe failed: %v", err))
	}
	if len(codes) == 0 {
		return fallbackBars(cfg, "resolved universe is empty")
	}
	all := make([]domain.DailyBar, 0)
	consecutiveFailures := 0
	for idx, code := range codes {
		if idx > 0 && cfg.Data.Source == "akshare" && cfg.Provider.AkshareRequestDelayMS > 0 {
			if err := sleepContext(ctx, time.Duration(cfg.Provider.AkshareRequestDelayMS)*time.Millisecond); err != nil {
				return fallbackBars(cfg, fmt.Sprintf("request delay interrupted: %v", err))
			}
		}
		bars, err := provider.DailyBars(ctx, code, cfg.Data.StartDate, cfg.Data.EndDate)
		if err != nil {
			consecutiveFailures++
			if cfg.Data.Source == "akshare" && consecutiveFailures >= cfg.Provider.AkshareMaxConsecutiveFailures {
				return fallbackBars(cfg, fmt.Sprintf("akshare circuit breaker after %d consecutive failures, last %s: %v", consecutiveFailures, code, err))
			}
			return fallbackBars(cfg, fmt.Sprintf("fetch %s failed: %v", code, err))
		}
		consecutiveFailures = 0
		all = append(all, bars...)
	}
	if err := dataset.WriteBarsCSVFile(cfg.Data.BarsFile, all); err != nil {
		return barsLoadResult{}, err
	}
	return barsLoadResult{Bars: all, Summary: dataset.SummarizeBars(all, cfg.Data.BarsFile), Status: "fresh"}, nil
}

func fallbackBars(cfg appconfig.Config, reason string) (barsLoadResult, error) {
	if cfg.Data.Source != "akshare" || !cfg.Provider.AkshareFallbackToBarsFile {
		return barsLoadResult{}, fmt.Errorf(reason)
	}
	bars, err := data.LoadDailyBarsCSVFile(cfg.Data.BarsFile)
	if err != nil {
		return barsLoadResult{}, fmt.Errorf("%s; fallback bars file unavailable: %w", reason, err)
	}
	return barsLoadResult{Bars: bars, Summary: dataset.SummarizeBars(bars, cfg.Data.BarsFile), Status: "cached_fallback", Reason: reason}, nil
}

func sleepContext(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func createProvider(cfg appconfig.Config) (interface {
	DailyBars(context.Context, string, string, string) ([]domain.DailyBar, error)
}, error) {
	if cfg.Data.Source == "akshare" {
		return akshare.NewProvider(
			akshare.WithPythonPath(cfg.Provider.PythonPath),
			akshare.WithScriptPath(cfg.Provider.AkshareScript),
			akshare.WithCacheDir(cfg.Provider.AkshareCache),
			akshare.WithRefreshCache(cfg.Provider.RefreshCache),
			akshare.WithHistoryLookback(cfg.Provider.AkshareLookbackDays),
		), nil
	}
	if cfg.Provider.TushareToken == "" {
		return nil, fmt.Errorf("provider.tushare_token is required when data.source is tushare")
	}
	return tushare.NewClient(cfg.Provider.TushareToken), nil
}

func mustReadCodesFile(path string) []string {
	codes, err := dataset.ReadCodesFile(path)
	if err != nil {
		return nil
	}
	return codes
}

func handleQuality(report quality.Report, strict bool) error {
	if report.Errors == 0 && report.Warnings == 0 {
		return nil
	}
	if report.HasErrors() || strict {
		return fmt.Errorf("data quality check failed: %s", report.String())
	}
	return nil
}
