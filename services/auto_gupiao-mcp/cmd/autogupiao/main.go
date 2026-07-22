package main

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/backtest"
	"github.com/frankichen/auto_gupiao/internal/daily"
	"github.com/frankichen/auto_gupiao/internal/data"
	"github.com/frankichen/auto_gupiao/internal/dataset"
	"github.com/frankichen/auto_gupiao/internal/domain"
	"github.com/frankichen/auto_gupiao/internal/marketdata/akshare"
	"github.com/frankichen/auto_gupiao/internal/marketdata/tushare"
	"github.com/frankichen/auto_gupiao/internal/paper"
	"github.com/frankichen/auto_gupiao/internal/quality"
	"github.com/frankichen/auto_gupiao/internal/report"
	"github.com/frankichen/auto_gupiao/internal/scheduler"
	"github.com/frankichen/auto_gupiao/internal/selector"
	"github.com/frankichen/auto_gupiao/internal/sim"
	"github.com/frankichen/auto_gupiao/internal/storage"
)

func main() {
	mode := flag.String("mode", "select", "run mode: select, backtest, simulate, fetch-bars, fetch-batch-bars, combine-bars, plan, paper, paper-matrix, report, or daily")
	input := flag.String("input", "", "CSV file/list with stock snapshots/daily bars, or paper JSON for report mode")
	sellInput := flag.String("sell-input", "", "optional CSV file with sell-day snapshots for simulate mode")
	source := flag.String("source", "csv", "market data source: csv, tushare, or akshare")
	tushareToken := flag.String("tushare-token", os.Getenv("TUSHARE_TOKEN"), "Tushare token, defaults to TUSHARE_TOKEN env")
	tradeDate := flag.String("trade-date", time.Now().Format("20060102"), "trading day/report day, format YYYYMMDD or YYYY-MM-DD")
	nextTradeDate := flag.String("next-trade-date", "", "next trading day for plan mode, format YYYYMMDD or YYYY-MM-DD")
	selectTime := flag.String("select-time", "14:00", "select task time for plan mode")
	buyTime := flag.String("buy-time", "14:05", "simulate buy task time for plan mode")
	sellTime := flag.String("sell-time", "10:00", "simulate sell task time for plan mode")
	reportTime := flag.String("report-time", "15:30", "report task time for plan mode")
	reportDir := flag.String("report-dir", "reports", "directory for generated report files")
	reportPrefix := flag.String("report-prefix", "", "filename prefix for generated report files")
	startDate := flag.String("start-date", "", "start date for fetch-bars/fetch-batch-bars, format YYYYMMDD or YYYY-MM-DD")
	endDate := flag.String("end-date", "", "end date for fetch-bars/fetch-batch-bars, format YYYYMMDD or YYYY-MM-DD")
	code := flag.String("code", "", "stock code for fetch-bars")
	codes := flag.String("codes", "", "comma/space/newline separated stock codes for fetch-batch-bars")
	codesFile := flag.String("codes-file", "", "file with stock codes for fetch-batch-bars")
	output := flag.String("output", "", "output CSV path for fetch-bars/fetch-batch-bars/combine-bars, defaults to stdout for fetch-bars only")
	pythonPath := flag.String("python", "python", "python executable path for akshare source")
	akshareScript := flag.String("akshare-script", akshare.DefaultScriptPath, "AKShare bridge script path")
	akshareCache := flag.String("akshare-cache", akshare.DefaultCacheDir, "AKShare local CSV cache directory")
	akshareEnrich := flag.Bool("akshare-enrich", false, "enrich AKShare spot snapshots with historical technical indicators")
	akshareEnrichLimit := flag.Int("akshare-enrich-limit", akshare.DefaultEnrichLimit, "maximum AKShare spot rows to enrich with historical bars")
	akshareLookbackDays := flag.Int("akshare-lookback-days", akshare.DefaultHistoryLookback, "AKShare technical indicator lookback natural days")
	strategyProfile := flag.String("strategy-profile", "", "strategy profile: full or basic; defaults to basic for akshare and full otherwise")
	strictEntry := flag.Bool("strict-entry", false, "enable strict paper entry rules")
	strictQuality := flag.Bool("strict-quality", false, "treat quality warnings as fatal")
	paperSellPrice := flag.String("paper-sell-price", paper.SellAtOpen, "paper mode sell price: open or close")
	minTradeAmount := flag.Float64("min-trade-amount", 1000, "minimum paper trade amount")
	minHoldDays := flag.Int("min-hold-days", 3, "minimum paper holding days")
	maxHoldDays := flag.Int("max-hold-days", 7, "maximum paper holding days")
	stopLossPct := flag.Float64("stop-loss-pct", 3.5, "paper stop loss percentage")
	takeProfitPct := flag.Float64("take-profit-pct", 8.0, "paper take profit percentage")
	cooldownDays := flag.Int("cooldown-days", 5, "paper loss cooldown days")
	stopLossCooldownDays := flag.Int("stop-loss-cooldown-days", 30, "paper stop loss cooldown days")
	poorPerformerMinTrades := flag.Int("poor-performer-min-trades", 2, "minimum closed trades before poor performer filtering")
	poorPerformerMaxNetProfit := flag.Float64("poor-performer-max-net-profit", -300, "maximum net profit for poor performer filtering")
	poorPerformerFilter := flag.Bool("poor-performer-filter", true, "enable net-profit poor performer filtering in paper modes")
	repeatedStopLossFilter := flag.Bool("repeated-stop-loss-filter", true, "enable repeated stop loss filtering in paper modes")
	singleLossFilter := flag.Bool("single-loss-filter", false, "enable single large stop loss filtering in paper modes")
	singleLossMaxNetProfit := flag.Float64("single-loss-max-net-profit", -300, "single loss net profit threshold")
	singleLossMaxReturnPct := flag.Float64("single-loss-max-return-pct", -5, "single loss return percentage threshold")
	largeLossMaxReturnPct := flag.Float64("large-loss-max-return-pct", -6.0, "large stop loss return percentage threshold")
	largeLossCooldownDays := flag.Int("large-loss-cooldown-days", 15, "large stop loss cooldown days when enabled")
	lossStreakThreshold := flag.Int("loss-streak-threshold", 4, "loss streak threshold for pause filtering")
	lossStreakPauseDays := flag.Int("loss-streak-pause-days", 2, "loss streak pause days when enabled")
	cachePath := flag.String("cache", "data/cache/snapshots", "local snapshot cache file or directory path")
	calendarCachePath := flag.String("calendar-cache", "data/cache/calendar", "local trade calendar cache file or directory path")
	refreshCache := flag.Bool("refresh-cache", false, "refresh snapshots from provider even if cache exists")
	refreshCalendar := flag.Bool("refresh-calendar", false, "refresh trade calendar from provider even if cache exists")
	warmCalendar := flag.Bool("warm-calendar", true, "load and cache trade calendar before loading snapshots")
	calendarExchange := flag.String("calendar-exchange", "SSE", "exchange for trade calendar, e.g. SSE or SZSE")
	calendarLookbackDays := flag.Int("calendar-lookback-days", 30, "calendar lookback natural days")
	calendarForwardDays := flag.Int("calendar-forward-days", 7, "calendar forward natural days")
	cash := flag.Float64("cash", 10000, "available cash")
	topN := flag.Int("top", 5, "number of candidates to output")
	minScore := flag.Float64("min-score", 60, "minimum score threshold")
	maxPositionRatio := flag.Float64("max-position-ratio", 0.30, "maximum cash ratio per position")
	allowMultipleBuys := flag.Bool("allow-multiple-buys", false, "allow backtest/simulate/paper/daily to buy multiple candidates on the same day")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
	defer cancel()

	switch *mode {
	case "select":
		err := runSelect(ctx, selectOptions{
			Source:               *source,
			Input:                *input,
			TushareToken:         *tushareToken,
			TradeDate:            tushare.NormalizeDate(*tradeDate),
			PythonPath:           *pythonPath,
			AkshareScript:        *akshareScript,
			AkshareCache:         *akshareCache,
			AkshareEnrich:        *akshareEnrich,
			AkshareEnrichLimit:   *akshareEnrichLimit,
			AkshareLookbackDays:  *akshareLookbackDays,
			StrategyProfile:      *strategyProfile,
			StrictQuality:        *strictQuality,
			CachePath:            *cachePath,
			CalendarCachePath:    *calendarCachePath,
			RefreshCache:         *refreshCache,
			RefreshCalendar:      *refreshCalendar,
			WarmCalendar:         *warmCalendar,
			CalendarExchange:     *calendarExchange,
			CalendarLookbackDays: *calendarLookbackDays,
			CalendarForwardDays:  *calendarForwardDays,
			Cash:                 *cash,
			TopN:                 *topN,
			MinScore:             *minScore,
		})
		exitOnError("select", err)
	case "backtest":
		err := runBacktest(backtestOptions{Input: *input, Cash: *cash, TopN: *topN, MinScore: *minScore, MaxPositionRatio: *maxPositionRatio, AllowMultipleBuys: *allowMultipleBuys, StrictQuality: *strictQuality})
		exitOnError("backtest", err)
	case "paper":
		err := runPaper(paperOptions{Input: *input, Cash: *cash, TopN: *topN, MinScore: *minScore, MaxPositionRatio: *maxPositionRatio, AllowMultipleBuys: *allowMultipleBuys, StrategyProfile: *strategyProfile, StrictEntry: *strictEntry, SellPriceMode: *paperSellPrice, StrictQuality: *strictQuality, MinTradeAmount: *minTradeAmount, MinHoldDays: *minHoldDays, MaxHoldDays: *maxHoldDays, StopLossPct: *stopLossPct, TakeProfitPct: *takeProfitPct, CooldownDays: *cooldownDays, StopLossCooldownDays: *stopLossCooldownDays, PoorPerformerFilter: *poorPerformerFilter, RepeatedStopLossFilter: *repeatedStopLossFilter, PoorPerformerMinTrades: *poorPerformerMinTrades, PoorPerformerMaxNetProfit: *poorPerformerMaxNetProfit, SingleLossFilter: *singleLossFilter, SingleLossMaxNetProfit: *singleLossMaxNetProfit, SingleLossMaxReturnPct: *singleLossMaxReturnPct, LargeLossMaxReturnPct: *largeLossMaxReturnPct, LargeLossCooldownDays: *largeLossCooldownDays, LossStreakThreshold: *lossStreakThreshold, LossStreakPauseDays: *lossStreakPauseDays})
		exitOnError("paper", err)
	case "paper-matrix":
		err := runPaperMatrix(paperOptions{Input: *input, Cash: *cash, TopN: *topN, MinScore: *minScore, MaxPositionRatio: *maxPositionRatio, AllowMultipleBuys: *allowMultipleBuys, StrategyProfile: *strategyProfile, StrictEntry: *strictEntry, SellPriceMode: *paperSellPrice, StrictQuality: *strictQuality, MinTradeAmount: *minTradeAmount, MinHoldDays: *minHoldDays, MaxHoldDays: *maxHoldDays, StopLossPct: *stopLossPct, TakeProfitPct: *takeProfitPct, CooldownDays: *cooldownDays, StopLossCooldownDays: *stopLossCooldownDays, PoorPerformerFilter: *poorPerformerFilter, RepeatedStopLossFilter: *repeatedStopLossFilter, PoorPerformerMinTrades: *poorPerformerMinTrades, PoorPerformerMaxNetProfit: *poorPerformerMaxNetProfit, SingleLossFilter: *singleLossFilter, SingleLossMaxNetProfit: *singleLossMaxNetProfit, SingleLossMaxReturnPct: *singleLossMaxReturnPct, LargeLossMaxReturnPct: *largeLossMaxReturnPct, LargeLossCooldownDays: *largeLossCooldownDays, LossStreakThreshold: *lossStreakThreshold, LossStreakPauseDays: *lossStreakPauseDays})
		exitOnError("paper-matrix", err)
	case "daily":
		err := runDaily(dailyOptions{Input: *input, Cash: *cash, TopN: *topN, MinScore: *minScore, MaxPositionRatio: *maxPositionRatio, AllowMultipleBuys: *allowMultipleBuys, StrategyProfile: *strategyProfile, SellPriceMode: *paperSellPrice, StrictQuality: *strictQuality, ReportDir: *reportDir, ReportPrefix: *reportPrefix, ReportDate: *tradeDate})
		exitOnError("daily", err)
	case "report":
		err := runReport(reportOptions{Input: *input, ReportDir: *reportDir, Prefix: *reportPrefix, Date: *tradeDate})
		exitOnError("report", err)
	case "simulate":
		err := runSimulate(simulateOptions{Input: *input, SellInput: *sellInput, Cash: *cash, TopN: *topN, MinScore: *minScore, MaxPositionRatio: *maxPositionRatio, AllowMultipleBuys: *allowMultipleBuys, StrictQuality: *strictQuality})
		exitOnError("simulate", err)
	case "fetch-bars":
		err := runFetchBars(ctx, fetchBarsOptions{Source: *source, Code: *code, StartDate: *startDate, EndDate: *endDate, Output: *output, PythonPath: *pythonPath, AkshareScript: *akshareScript, AkshareCache: *akshareCache, AkshareLookbackDays: *akshareLookbackDays, RefreshCache: *refreshCache, TushareToken: *tushareToken, StrictQuality: *strictQuality})
		exitOnError("fetch-bars", err)
	case "fetch-batch-bars":
		err := runFetchBatchBars(ctx, fetchBatchBarsOptions{Source: *source, Codes: *codes, CodesFile: *codesFile, StartDate: *startDate, EndDate: *endDate, Output: *output, PythonPath: *pythonPath, AkshareScript: *akshareScript, AkshareCache: *akshareCache, AkshareLookbackDays: *akshareLookbackDays, RefreshCache: *refreshCache, TushareToken: *tushareToken, StrictQuality: *strictQuality})
		exitOnError("fetch-batch-bars", err)
	case "combine-bars":
		err := runCombineBars(combineBarsOptions{Input: *input, Output: *output, StrictQuality: *strictQuality})
		exitOnError("combine-bars", err)
	case "plan":
		plan := scheduler.BuildPlan(scheduler.Config{TradeDate: *tradeDate, NextTradeDate: *nextTradeDate, SelectTime: *selectTime, BuyTime: *buyTime, SellTime: *sellTime, ReportTime: *reportTime})
		exitOnError("plan", writeJSON(plan))
	default:
		fmt.Fprintf(os.Stderr, "unsupported mode %q\n", *mode)
		os.Exit(2)
	}
}

func exitOnError(name string, err error) {
	if err == nil {
		return
	}
	fmt.Fprintf(os.Stderr, "%s: %v\n", name, err)
	os.Exit(1)
}

type selectOptions struct {
	Source               string
	Input                string
	TushareToken         string
	TradeDate            string
	PythonPath           string
	AkshareScript        string
	AkshareCache         string
	AkshareEnrich        bool
	AkshareEnrichLimit   int
	AkshareLookbackDays  int
	StrategyProfile      string
	StrictQuality        bool
	CachePath            string
	CalendarCachePath    string
	RefreshCache         bool
	RefreshCalendar      bool
	WarmCalendar         bool
	CalendarExchange     string
	CalendarLookbackDays int
	CalendarForwardDays  int
	Cash                 float64
	TopN                 int
	MinScore             float64
}

type backtestOptions struct {
	Input             string
	Cash              float64
	TopN              int
	MinScore          float64
	MaxPositionRatio  float64
	AllowMultipleBuys bool
	StrictQuality     bool
}

type paperOptions struct {
	Input                     string
	Cash                      float64
	TopN                      int
	MinScore                  float64
	MaxPositionRatio          float64
	AllowMultipleBuys         bool
	StrategyProfile           string
	StrictEntry               bool
	SellPriceMode             string
	StrictQuality             bool
	MinTradeAmount            float64
	MinHoldDays               int
	MaxHoldDays               int
	StopLossPct               float64
	TakeProfitPct             float64
	CooldownDays              int
	StopLossCooldownDays      int
	PoorPerformerFilter       bool
	RepeatedStopLossFilter    bool
	PoorPerformerMinTrades    int
	PoorPerformerMaxNetProfit float64
	SingleLossFilter          bool
	SingleLossMaxNetProfit    float64
	SingleLossMaxReturnPct    float64
	LargeLossMaxReturnPct     float64
	LargeLossCooldownDays     int
	LossStreakThreshold       int
	LossStreakPauseDays       int
}

type dailyOptions struct {
	Input             string
	Cash              float64
	TopN              int
	MinScore          float64
	MaxPositionRatio  float64
	AllowMultipleBuys bool
	StrategyProfile   string
	SellPriceMode     string
	StrictQuality     bool
	ReportDir         string
	ReportPrefix      string
	ReportDate        string
}

type reportOptions struct {
	Input     string
	ReportDir string
	Prefix    string
	Date      string
}

type simulateOptions struct {
	Input             string
	SellInput         string
	Cash              float64
	TopN              int
	MinScore          float64
	MaxPositionRatio  float64
	AllowMultipleBuys bool
	StrictQuality     bool
}

type fetchBarsOptions struct {
	Source              string
	Code                string
	StartDate           string
	EndDate             string
	Output              string
	PythonPath          string
	AkshareScript       string
	AkshareCache        string
	AkshareLookbackDays int
	RefreshCache        bool
	TushareToken        string
	StrictQuality       bool
}

type fetchBatchBarsOptions struct {
	Source              string
	Codes               string
	CodesFile           string
	StartDate           string
	EndDate             string
	Output              string
	PythonPath          string
	AkshareScript       string
	AkshareCache        string
	AkshareLookbackDays int
	RefreshCache        bool
	TushareToken        string
	StrictQuality       bool
}

type combineBarsOptions struct {
	Input         string
	Output        string
	StrictQuality bool
}

func runSelect(ctx context.Context, opts selectOptions) error {
	snapshots, err := loadSnapshots(ctx, opts)
	if err != nil {
		return err
	}
	if err := handleQuality(quality.ValidateSnapshots(snapshots, quality.Options{MinRows: 1, RequireIndicators: opts.AkshareEnrich, IndicatorWarnRatio: 0.6}), opts.StrictQuality); err != nil {
		return err
	}
	cfg := selector.DefaultConfig()
	cfg.Cash = opts.Cash
	cfg.TopN = opts.TopN
	cfg.MinScore = opts.MinScore
	cfg.Profile = resolveProfile(opts.StrategyProfile, opts.Source)
	if cfg.Profile == selector.ProfileBasic {
		cfg.MinTurnoverRate = 0
	}
	return writeJSON(selector.Select(snapshots, cfg))
}

func runBacktest(opts backtestOptions) error {
	bars, err := loadBarsForRun(opts.Input, opts.StrictQuality, 2)
	if err != nil {
		return err
	}
	return writeJSON(backtest.Run(bars, backtest.Config{InitialCash: opts.Cash, TopN: opts.TopN, MinScore: opts.MinScore, MaxPositionRatio: opts.MaxPositionRatio, AllowMultipleBuys: opts.AllowMultipleBuys}))
}

func runPaper(opts paperOptions) error {
	bars, err := loadBarsForRun(opts.Input, opts.StrictQuality, 2)
	if err != nil {
		return err
	}
	return writeJSON(paper.Run(bars, paperConfigFromOptions(opts)))
}

func runPaperMatrix(opts paperOptions) error {
	bars, err := loadBarsForRun(opts.Input, opts.StrictQuality, 2)
	if err != nil {
		return err
	}
	return writeJSON(paper.RunMatrix(bars, paperConfigFromOptions(opts), nil))
}

func paperConfigFromOptions(opts paperOptions) paper.Config {
	profile := opts.StrategyProfile
	if profile == "" {
		profile = selector.ProfileFull
	}
	return paper.Config{
		InitialCash:               opts.Cash,
		TopN:                      opts.TopN,
		MinScore:                  opts.MinScore,
		MaxPositionRatio:          opts.MaxPositionRatio,
		AllowMultipleBuys:         opts.AllowMultipleBuys,
		StrategyProfile:           profile,
		StrictEntry:               opts.StrictEntry,
		SellPriceMode:             opts.SellPriceMode,
		MinTradeAmount:            opts.MinTradeAmount,
		MinHoldDays:               opts.MinHoldDays,
		MaxHoldDays:               opts.MaxHoldDays,
		StopLossPct:               opts.StopLossPct,
		TakeProfitPct:             opts.TakeProfitPct,
		CooldownDays:              opts.CooldownDays,
		StopLossCooldownDays:      opts.StopLossCooldownDays,
		PoorPerformerFilter:       opts.PoorPerformerFilter,
		RepeatedStopLossFilter:    opts.RepeatedStopLossFilter,
		PoorPerformerMinTrades:    opts.PoorPerformerMinTrades,
		PoorPerformerMaxNetProfit: opts.PoorPerformerMaxNetProfit,
		SingleLossFilter:          opts.SingleLossFilter,
		SingleLossMaxNetProfit:    opts.SingleLossMaxNetProfit,
		SingleLossMaxReturnPct:    opts.SingleLossMaxReturnPct,
		LargeLossMaxReturnPct:     opts.LargeLossMaxReturnPct,
		LargeLossCooldownDays:     opts.LargeLossCooldownDays,
		LossStreakThreshold:       opts.LossStreakThreshold,
		LossStreakPauseDays:       opts.LossStreakPauseDays,
	}
}

func runDaily(opts dailyOptions) error {
	bars, err := loadBarsForRun(opts.Input, opts.StrictQuality, 2)
	if err != nil {
		return err
	}
	profile := opts.StrategyProfile
	if profile == "" {
		profile = selector.ProfileFull
	}
	result, err := daily.RunPaperReport(bars, daily.Config{InitialCash: opts.Cash, TopN: opts.TopN, MinScore: opts.MinScore, MaxPositionRatio: opts.MaxPositionRatio, AllowMultipleBuys: opts.AllowMultipleBuys, StrategyProfile: profile, SellPriceMode: opts.SellPriceMode, ReportDir: opts.ReportDir, ReportPrefix: opts.ReportPrefix, ReportDate: opts.ReportDate})
	if err != nil {
		return err
	}
	return writeJSON(result)
}

func runReport(opts reportOptions) error {
	if opts.Input == "" {
		return fmt.Errorf("missing -input for report mode")
	}
	file, err := os.Open(opts.Input)
	if err != nil {
		return fmt.Errorf("open paper result: %w", err)
	}
	defer file.Close()
	result, err := report.LoadPaperResult(file)
	if err != nil {
		return err
	}
	manifest, err := report.WritePaperReport(result, report.Options{ReportDir: opts.ReportDir, Prefix: opts.Prefix, Date: opts.Date})
	if err != nil {
		return err
	}
	return writeJSON(manifest)
}

func runFetchBatchBars(ctx context.Context, opts fetchBatchBarsOptions) error {
	if opts.Output == "" {
		return fmt.Errorf("missing -output for fetch-batch-bars mode")
	}
	inlineCodes := dataset.ParseCodes(opts.Codes)
	fileCodes, err := dataset.ReadCodesFile(opts.CodesFile)
	if err != nil {
		return err
	}
	codes := dataset.MergeCodes(inlineCodes, fileCodes)
	if len(codes) == 0 {
		return fmt.Errorf("missing -codes or -codes-file for fetch-batch-bars mode")
	}
	provider, err := createBarProvider(fetchBarsOptions{Source: opts.Source, PythonPath: opts.PythonPath, AkshareScript: opts.AkshareScript, AkshareCache: opts.AkshareCache, AkshareLookbackDays: opts.AkshareLookbackDays, RefreshCache: opts.RefreshCache, TushareToken: opts.TushareToken})
	if err != nil {
		return err
	}
	all := make([]domain.DailyBar, 0)
	for _, code := range codes {
		bars, err := provider.DailyBars(ctx, code, opts.StartDate, opts.EndDate)
		if err != nil {
			return fmt.Errorf("fetch %s: %w", code, err)
		}
		all = append(all, bars...)
	}
	if err := handleQuality(quality.ValidateBars(all, quality.Options{MinRows: 1}), opts.StrictQuality); err != nil {
		return err
	}
	if err := writeDatasetBarsCSV(opts.Output, all); err != nil {
		return err
	}
	return writeJSON(dataset.SummarizeBars(all, opts.Output))
}

func runCombineBars(opts combineBarsOptions) error {
	if opts.Input == "" {
		return fmt.Errorf("missing -input for combine-bars mode")
	}
	if opts.Output == "" {
		return fmt.Errorf("missing -output for combine-bars mode")
	}
	paths := dataset.ParsePaths(opts.Input)
	if len(paths) == 0 {
		return fmt.Errorf("missing input paths")
	}
	all := make([]domain.DailyBar, 0)
	for _, path := range paths {
		bars, err := loadBarsForRun(path, opts.StrictQuality, 1)
		if err != nil {
			return fmt.Errorf("load %s: %w", path, err)
		}
		all = append(all, bars...)
	}
	if err := writeDatasetBarsCSV(opts.Output, all); err != nil {
		return err
	}
	return writeJSON(dataset.SummarizeBars(all, opts.Output))
}

func loadBarsForRun(input string, strict bool, minRows int) ([]domain.DailyBar, error) {
	if input == "" {
		return nil, fmt.Errorf("missing -input")
	}
	file, err := os.Open(input)
	if err != nil {
		return nil, fmt.Errorf("open input: %w", err)
	}
	defer file.Close()
	bars, err := data.LoadDailyBarsCSV(file)
	if err != nil {
		return nil, fmt.Errorf("load daily bars: %w", err)
	}
	if err := handleQuality(quality.ValidateBars(bars, quality.Options{MinRows: minRows}), strict); err != nil {
		return nil, err
	}
	return bars, nil
}

func runSimulate(opts simulateOptions) error {
	if opts.Input == "" {
		return fmt.Errorf("missing -input for simulate mode")
	}
	buySnapshots, err := loadSnapshotsCSV(opts.Input)
	if err != nil {
		return fmt.Errorf("load buy snapshots: %w", err)
	}
	if err := handleQuality(quality.ValidateSnapshots(buySnapshots, quality.Options{MinRows: 1}), opts.StrictQuality); err != nil {
		return err
	}
	cfg := selector.DefaultConfig()
	cfg.Cash = opts.Cash
	cfg.TopN = opts.TopN
	cfg.MinScore = opts.MinScore
	cfg.MaxPositionRatio = opts.MaxPositionRatio
	candidates := selector.Select(buySnapshots, cfg)
	if !opts.AllowMultipleBuys && len(candidates) > 1 {
		candidates = candidates[:1]
	}
	account := sim.NewAccount(opts.Cash, domain.DefaultCostModel())
	for _, candidate := range candidates {
		if _, err := account.BuyCandidate(candidate); err != nil {
			return fmt.Errorf("buy %s: %w", candidate.Snapshot.Code, err)
		}
	}
	markSnapshots := buySnapshots
	if opts.SellInput != "" {
		sellSnapshots, err := loadSnapshotsCSV(opts.SellInput)
		if err != nil {
			return fmt.Errorf("load sell snapshots: %w", err)
		}
		if err := handleQuality(quality.ValidateSnapshots(sellSnapshots, quality.Options{MinRows: 1}), opts.StrictQuality); err != nil {
			return err
		}
		if errs := account.SellAll(sellSnapshots); len(errs) > 0 {
			return fmt.Errorf("sell all: %v", errs)
		}
		markSnapshots = sellSnapshots
	}
	return writeJSON(account.Report(markSnapshots))
}

func runFetchBars(ctx context.Context, opts fetchBarsOptions) error {
	if opts.Code == "" {
		return fmt.Errorf("missing -code for fetch-bars mode")
	}
	provider, err := createBarProvider(opts)
	if err != nil {
		return err
	}
	bars, err := provider.DailyBars(ctx, opts.Code, opts.StartDate, opts.EndDate)
	if err != nil {
		return err
	}
	if err := handleQuality(quality.ValidateBars(bars, quality.Options{MinRows: 1}), opts.StrictQuality); err != nil {
		return err
	}
	return writeBarsCSV(opts.Output, bars)
}

func handleQuality(report quality.Report, strict bool) error {
	if report.Errors == 0 && report.Warnings == 0 {
		return nil
	}
	encoder := json.NewEncoder(os.Stderr)
	encoder.SetIndent("", "  ")
	_ = encoder.Encode(report)
	if report.HasErrors() || strict {
		return fmt.Errorf("data quality check failed: %s", report.String())
	}
	return nil
}

func createBarProvider(opts fetchBarsOptions) (interface {
	DailyBars(context.Context, string, string, string) ([]domain.DailyBar, error)
}, error) {
	switch opts.Source {
	case "akshare":
		return akshare.NewProvider(akshare.WithPythonPath(opts.PythonPath), akshare.WithScriptPath(opts.AkshareScript), akshare.WithCacheDir(opts.AkshareCache), akshare.WithRefreshCache(opts.RefreshCache), akshare.WithHistoryLookback(opts.AkshareLookbackDays)), nil
	case "tushare":
		if opts.TushareToken == "" {
			return nil, fmt.Errorf("missing tushare token; use AKShare or set TUSHARE_TOKEN")
		}
		return tushare.NewClient(opts.TushareToken), nil
	default:
		return nil, fmt.Errorf("fetch-bars supports source akshare or tushare, got %q", opts.Source)
	}
}

func writeBarsCSV(path string, bars []domain.DailyBar) error {
	var file *os.File
	var err error
	if path == "" {
		file = os.Stdout
	} else {
		dir := filepath.Dir(path)
		if dir != "." && dir != "" {
			if err := os.MkdirAll(dir, 0755); err != nil {
				return fmt.Errorf("create output directory: %w", err)
			}
		}
		file, err = os.Create(path)
		if err != nil {
			return fmt.Errorf("create output: %w", err)
		}
		defer file.Close()
	}
	writer := csv.NewWriter(file)
	defer writer.Flush()
	if err := writer.Write([]string{"date", "code", "open", "high", "low", "close", "prev_close", "change", "change_pct", "volume", "amount"}); err != nil {
		return err
	}
	for _, bar := range bars {
		record := []string{bar.Date, bar.Code, fmtFloat(bar.Open), fmtFloat(bar.High), fmtFloat(bar.Low), fmtFloat(bar.Close), fmtFloat(bar.PrevClose), fmtFloat(bar.Change), fmtFloat(bar.ChangePct), fmtFloat(bar.Volume), fmtFloat(bar.Amount)}
		if err := writer.Write(record); err != nil {
			return err
		}
	}
	return writer.Error()
}

func writeDatasetBarsCSV(path string, bars []domain.DailyBar) error {
	dir := filepath.Dir(path)
	if dir != "." && dir != "" {
		if err := os.MkdirAll(dir, 0755); err != nil {
			return fmt.Errorf("create output directory: %w", err)
		}
	}
	file, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("create output: %w", err)
	}
	defer file.Close()
	return dataset.WriteBarsCSV(file, bars)
}

func fmtFloat(v float64) string { return fmt.Sprintf("%.6f", v) }

func loadSnapshotsCSV(path string) ([]domain.StockSnapshot, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open input: %w", err)
	}
	defer file.Close()
	return data.LoadSnapshotsCSV(file)
}

func loadSnapshots(ctx context.Context, opts selectOptions) ([]domain.StockSnapshot, error) {
	switch opts.Source {
	case "csv":
		if opts.Input == "" {
			return nil, fmt.Errorf("missing -input for csv source")
		}
		return loadSnapshotsCSV(opts.Input)
	case "akshare":
		provider := akshare.NewProvider(akshare.WithPythonPath(opts.PythonPath), akshare.WithScriptPath(opts.AkshareScript), akshare.WithCacheDir(opts.AkshareCache), akshare.WithRefreshCache(opts.RefreshCache), akshare.WithEnrichSnapshots(opts.AkshareEnrich), akshare.WithEnrichLimit(opts.AkshareEnrichLimit), akshare.WithHistoryLookback(opts.AkshareLookbackDays))
		return provider.ListSnapshots(ctx, opts.TradeDate)
	case "tushare":
		if opts.TradeDate == "" {
			return nil, fmt.Errorf("missing -trade-date for tushare source")
		}
		cache := storage.NewSnapshotCache(opts.CachePath)
		if !opts.RefreshCache {
			if snapshots, ok, err := cache.Load(opts.TradeDate); err != nil {
				return nil, err
			} else if ok {
				return snapshots, nil
			}
		}
		if opts.TushareToken == "" {
			return nil, fmt.Errorf("missing tushare token: set -tushare-token or TUSHARE_TOKEN")
		}
		provider := tushare.NewClient(opts.TushareToken)
		if opts.WarmCalendar {
			if err := warmTradeCalendar(ctx, provider, opts); err != nil {
				return nil, err
			}
		}
		snapshots, err := provider.ListSnapshots(ctx, opts.TradeDate)
		if err != nil {
			return nil, improveTushareError(err)
		}
		if err := cache.Save(opts.TradeDate, snapshots); err != nil {
			return nil, err
		}
		return snapshots, nil
	default:
		return nil, fmt.Errorf("unsupported source %q", opts.Source)
	}
}

func resolveProfile(profile string, source string) string {
	if profile != "" {
		return profile
	}
	if source == "akshare" {
		return selector.ProfileBasic
	}
	return selector.ProfileFull
}

func warmTradeCalendar(ctx context.Context, provider *tushare.Client, opts selectOptions) error {
	start, end := calendarRange(opts.TradeDate, opts.CalendarLookbackDays, opts.CalendarForwardDays)
	cache := storage.NewCalendarCache(opts.CalendarCachePath)
	if !opts.RefreshCalendar {
		if _, ok, err := cache.Load(opts.CalendarExchange, start, end); err != nil {
			return err
		} else if ok {
			return nil
		}
	}
	days, err := provider.TradeCalendar(ctx, opts.CalendarExchange, start, end)
	if err != nil {
		return improveTushareError(fmt.Errorf("load trade calendar: %w", err))
	}
	return cache.Save(opts.CalendarExchange, start, end, days)
}

func improveTushareError(err error) error {
	message := err.Error()
	if strings.Contains(message, "trade_cal") && strings.Contains(message, "40203") {
		return fmt.Errorf("%w; 当前 Tushare 账号缺少 trade_cal 权限，可先使用 -warm-calendar=false 跳过日历预热，或使用 -source akshare", err)
	}
	if strings.Contains(message, "daily") && strings.Contains(message, "40203") {
		return fmt.Errorf("%w; 当前 Tushare 账号缺少 daily 权限，请使用 -source akshare 或 CSV 模式", err)
	}
	return err
}

func calendarRange(tradeDate string, lookbackDays int, forwardDays int) (string, string) {
	if lookbackDays < 0 {
		lookbackDays = 0
	}
	if forwardDays < 0 {
		forwardDays = 0
	}
	parsed, err := time.Parse("20060102", tushare.NormalizeDate(tradeDate))
	if err != nil {
		day := tushare.NormalizeDate(tradeDate)
		return day, day
	}
	return parsed.AddDate(0, 0, -lookbackDays).Format("20060102"), parsed.AddDate(0, 0, forwardDays).Format("20060102")
}

func writeJSON(v any) error {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(v)
}
