package akshare

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/data"
	"github.com/frankichen/auto_gupiao/internal/domain"
	"github.com/frankichen/auto_gupiao/internal/indicator"
	"github.com/frankichen/auto_gupiao/internal/selector"
)

const (
	DefaultScriptPath      = "scripts/akshare_fetch.py"
	DefaultCacheDir        = "data/cache/akshare"
	DefaultEnrichLimit     = 30
	DefaultHistoryLookback = 140
	defaultEnrichBudget    = 3000
)

type Provider struct {
	PythonPath      string
	ScriptPath      string
	CacheDir        string
	UseCache        bool
	RefreshCache    bool
	EnrichSnapshots bool
	EnrichLimit     int
	HistoryLookback int
}

type Option func(*Provider)

func WithPythonPath(path string) Option {
	return func(p *Provider) {
		if path != "" {
			p.PythonPath = path
		}
	}
}

func WithScriptPath(path string) Option {
	return func(p *Provider) {
		if path != "" {
			p.ScriptPath = path
		}
	}
}

func WithCacheDir(path string) Option {
	return func(p *Provider) {
		if path != "" {
			p.CacheDir = path
		}
	}
}

func WithCache(enabled bool) Option {
	return func(p *Provider) { p.UseCache = enabled }
}

func WithRefreshCache(refresh bool) Option {
	return func(p *Provider) { p.RefreshCache = refresh }
}

func WithEnrichSnapshots(enabled bool) Option {
	return func(p *Provider) { p.EnrichSnapshots = enabled }
}

func WithEnrichLimit(limit int) Option {
	return func(p *Provider) {
		if limit > 0 {
			p.EnrichLimit = limit
		}
	}
}

func WithHistoryLookback(days int) Option {
	return func(p *Provider) {
		if days > 0 {
			p.HistoryLookback = days
		}
	}
}

func NewProvider(opts ...Option) *Provider {
	provider := &Provider{
		PythonPath:      "python",
		ScriptPath:      DefaultScriptPath,
		CacheDir:        DefaultCacheDir,
		UseCache:        true,
		RefreshCache:    false,
		EnrichSnapshots: false,
		EnrichLimit:     DefaultEnrichLimit,
		HistoryLookback: DefaultHistoryLookback,
	}
	for _, opt := range opts {
		opt(provider)
	}
	return provider
}

func (p *Provider) ListStocks(ctx context.Context) ([]domain.StockBasic, error) {
	snapshots, err := p.ListSnapshots(ctx, "")
	if err != nil {
		return nil, err
	}
	stocks := make([]domain.StockBasic, 0, len(snapshots))
	for _, snapshot := range snapshots {
		stocks = append(stocks, domain.StockBasic{
			Code:   snapshot.Code,
			Symbol: snapshot.Code,
			Name:   snapshot.Name,
			Market: snapshot.Market,
		})
	}
	return stocks, nil
}

func (p *Provider) DailyBars(ctx context.Context, code string, startDate string, endDate string) ([]domain.DailyBar, error) {
	if code == "" {
		return nil, fmt.Errorf("akshare DailyBars requires code")
	}
	start := normalizeDate(startDate)
	end := normalizeDate(endDate)
	if end == "" {
		end = time.Now().Format("20060102")
	}
	if start == "" {
		start = lookbackStart(end, p.HistoryLookback)
	}
	cachePath := p.barsCachePath(code, start, end)
	if p.UseCache && !p.RefreshCache {
		if cached, ok, err := loadBarsCSV(cachePath); err != nil {
			return nil, err
		} else if ok {
			return cached, nil
		}
	}
	args := []string{p.ScriptPath, "--type", "bars", "--code", code, "--start-date", start, "--end-date", end}
	out, err := p.run(ctx, args...)
	if err != nil {
		return nil, err
	}
	if p.UseCache {
		if err := writeCache(cachePath, out); err != nil {
			return nil, err
		}
	}
	return data.LoadDailyBarsCSV(bytes.NewReader(out))
}

func (p *Provider) DailyBasics(ctx context.Context, tradeDate string) ([]domain.Fundamental, error) {
	return nil, fmt.Errorf("akshare DailyBasics is not implemented; use snapshots or CSV fundamentals")
}

func (p *Provider) TradeCalendar(ctx context.Context, exchange string, startDate string, endDate string) ([]domain.TradingDay, error) {
	return nil, fmt.Errorf("akshare TradeCalendar is not implemented; use Tushare calendar, CSV, or local trading-day inference")
}

func (p *Provider) ListSnapshots(ctx context.Context, tradeDate string) ([]domain.StockSnapshot, error) {
	day := normalizeDate(tradeDate)
	if day == "" {
		day = time.Now().Format("20060102")
	}
	cachePath := p.spotCachePath(day)
	if p.UseCache && !p.RefreshCache {
		if cached, ok, err := loadSnapshotsCSV(cachePath); err != nil {
			return nil, err
		} else if ok {
			cached = filterPricedSnapshots(cached)
			if p.EnrichSnapshots {
				return p.enrichSnapshots(ctx, cached, day), nil
			}
			return cached, nil
		}
	}
	args := []string{p.ScriptPath, "--type", "spot", "--trade-date", day}
	out, err := p.run(ctx, args...)
	if err != nil {
		return nil, err
	}
	if p.UseCache {
		if err := writeCache(cachePath, out); err != nil {
			return nil, err
		}
	}
	snapshots, err := data.LoadSnapshotsCSV(bytes.NewReader(out))
	if err != nil {
		return nil, err
	}
	snapshots = filterPricedSnapshots(snapshots)
	if p.EnrichSnapshots {
		return p.enrichSnapshots(ctx, snapshots, day), nil
	}
	return snapshots, nil
}

func filterPricedSnapshots(snapshots []domain.StockSnapshot) []domain.StockSnapshot {
	out := make([]domain.StockSnapshot, 0, len(snapshots))
	for _, snapshot := range snapshots {
		if snapshot.Close > 0 {
			out = append(out, snapshot)
		}
	}
	return out
}

func (p *Provider) enrichSnapshots(ctx context.Context, snapshots []domain.StockSnapshot, tradeDate string) []domain.StockSnapshot {
	if len(snapshots) == 0 || p.EnrichLimit <= 0 {
		return snapshots
	}
	out := make([]domain.StockSnapshot, len(snapshots))
	copy(out, snapshots)
	indexes := candidateIndexes(out, p.EnrichLimit)
	for _, idx := range indexes {
		select {
		case <-ctx.Done():
			return out
		default:
		}
		code := out[idx].Code
		bars, err := p.DailyBars(ctx, code, lookbackStart(tradeDate, p.HistoryLookback), tradeDate)
		if err != nil || len(bars) == 0 {
			continue
		}
		techs := indicator.BuildTechnicals(bars)
		latest := latestTechnical(code, techs)
		if latest == nil {
			continue
		}
		out[idx].MA5 = latest.MA5
		out[idx].MA20 = latest.MA20
		out[idx].MA60 = latest.MA60
		out[idx].RSI6 = latest.RSI6
		out[idx].FiveDayPct = latest.FiveDayPct
		out[idx].TwentyDayPct = latest.TwentyDayPct
	}
	return out
}

func candidateIndexes(snapshots []domain.StockSnapshot, limit int) []int {
	type scored struct {
		idx       int
		score     float64
		cost      float64
		liquidity float64
	}
	items := make([]scored, 0, len(snapshots))
	for i, snapshot := range snapshots {
		if !domain.IsTradable(snapshot) || snapshot.Close <= 0 {
			continue
		}
		selection := selector.ScoreWithProfile(snapshot, selector.ProfileBasic)
		shares, cost := domain.RoundLotShares(snapshot.Close, defaultEnrichBudget, domain.DefaultCostModel())
		score := selection.Score
		if shares > 0 {
			score += 20
		} else {
			score -= 20
		}
		liquidity := snapshot.Amount / 100000000
		if snapshot.ChangePct < -5 || snapshot.ChangePct > 9 {
			score -= 20
		}
		items = append(items, scored{idx: i, score: score, cost: cost, liquidity: liquidity})
	}
	sort.SliceStable(items, func(i, j int) bool {
		if items[i].score != items[j].score {
			return items[i].score > items[j].score
		}
		if items[i].cost != items[j].cost {
			return items[i].cost < items[j].cost
		}
		return items[i].liquidity > items[j].liquidity
	})
	if len(items) > limit {
		items = items[:limit]
	}
	indexes := make([]int, 0, len(items))
	for _, item := range items {
		indexes = append(indexes, item.idx)
	}
	return indexes
}

func latestTechnical(code string, techs map[string]indicator.Technicals) *indicator.Technicals {
	prefix := code + "|"
	latestKey := ""
	for key := range techs {
		if strings.HasPrefix(key, prefix) && key > latestKey {
			latestKey = key
		}
	}
	if latestKey == "" {
		return nil
	}
	value := techs[latestKey]
	return &value
}

func (p *Provider) run(ctx context.Context, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, p.PythonPath, args...)
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = strings.TrimSpace(stdout.String())
		}
		if message == "" {
			message = err.Error()
		}
		if ctxErr := ctx.Err(); ctxErr != nil {
			return nil, fmt.Errorf("run akshare bridge canceled or timed out: %w", ctxErr)
		}
		if errors.Is(err, exec.ErrNotFound) {
			return nil, fmt.Errorf("python executable %q not found; set -python to a valid Python path", p.PythonPath)
		}
		return nil, fmt.Errorf("run akshare bridge failed: %s", message)
	}
	return stdout.Bytes(), nil
}

func (p *Provider) spotCachePath(day string) string {
	return filepath.Join(p.CacheDir, "spot_"+normalizeDate(day)+".csv")
}

func (p *Provider) barsCachePath(code string, start string, end string) string {
	return filepath.Join(p.CacheDir, "bars_"+code+"_"+normalizeDate(start)+"_"+normalizeDate(end)+".csv")
}

func loadSnapshotsCSV(path string) ([]domain.StockSnapshot, bool, error) {
	dataBytes, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("read akshare snapshot cache: %w", err)
	}
	items, err := data.LoadSnapshotsCSV(bytes.NewReader(dataBytes))
	if err != nil {
		return nil, false, fmt.Errorf("parse akshare snapshot cache: %w", err)
	}
	return items, true, nil
}

func loadBarsCSV(path string) ([]domain.DailyBar, bool, error) {
	dataBytes, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("read akshare bars cache: %w", err)
	}
	items, err := data.LoadDailyBarsCSV(bytes.NewReader(dataBytes))
	if err != nil {
		return nil, false, fmt.Errorf("parse akshare bars cache: %w", err)
	}
	return items, true, nil
}

func writeCache(path string, content []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return fmt.Errorf("create akshare cache dir: %w", err)
	}
	return os.WriteFile(path, content, 0644)
}

func lookbackStart(endDate string, calendarDays int) string {
	end := normalizeDate(endDate)
	parsed, err := time.Parse("20060102", end)
	if err != nil {
		return end
	}
	if calendarDays <= 0 {
		calendarDays = DefaultHistoryLookback
	}
	return parsed.AddDate(0, 0, -calendarDays).Format("20060102")
}

func normalizeDate(date string) string {
	out := ""
	for _, r := range strings.TrimSpace(date) {
		if r >= '0' && r <= '9' {
			out += string(r)
		}
	}
	return out
}
