package quality

import (
	"fmt"
	"strings"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

type Options struct {
	MinRows            int
	RequireIndicators  bool
	IndicatorWarnRatio float64
}

func ValidateSnapshots(items []domain.StockSnapshot, opts Options) Report {
	var issues []Issue
	if opts.MinRows > 0 && len(items) < opts.MinRows {
		issues = append(issues, Issue{Level: LevelWarn, Field: "rows", Message: fmt.Sprintf("snapshot rows %d below expected %d", len(items), opts.MinRows)})
	}
	seen := make(map[string]struct{}, len(items))
	missingIndicators := 0
	for _, item := range items {
		code := strings.TrimSpace(item.Code)
		date := normalizeDate(item.Date)
		if code == "" {
			issues = append(issues, issue(LevelError, item, "code", "empty stock code"))
		}
		if date == "" {
			issues = append(issues, issue(LevelError, item, "date", "empty date"))
		}
		if item.Close <= 0 {
			issues = append(issues, issue(LevelError, item, "close", "close must be positive"))
		}
		if item.High > 0 && item.Low > 0 && item.High < item.Low {
			issues = append(issues, issue(LevelError, item, "high_low", "high is lower than low"))
		}
		if item.High > 0 && item.Close > item.High*1.2 {
			issues = append(issues, issue(LevelWarn, item, "close", "close is far above high"))
		}
		if item.Low > 0 && item.Close < item.Low*0.8 {
			issues = append(issues, issue(LevelWarn, item, "close", "close is far below low"))
		}
		if item.Amount < 0 || item.TurnoverRate < 0 || item.VolumeRatio < 0 {
			issues = append(issues, issue(LevelWarn, item, "liquidity", "negative liquidity field"))
		}
		key := code + "|" + date
		if key != "|" {
			if _, ok := seen[key]; ok {
				issues = append(issues, issue(LevelWarn, item, "duplicate", "duplicate code and date"))
			}
			seen[key] = struct{}{}
		}
		if item.MA5 == 0 || item.MA20 == 0 || item.RSI6 == 0 {
			missingIndicators++
		}
	}
	if opts.RequireIndicators && len(items) > 0 {
		ratio := float64(missingIndicators) / float64(len(items))
		warnRatio := opts.IndicatorWarnRatio
		if warnRatio <= 0 {
			warnRatio = 0.5
		}
		if ratio >= warnRatio {
			issues = append(issues, Issue{Level: LevelWarn, Field: "indicators", Message: fmt.Sprintf("%.1f%% snapshots missing technical indicators", ratio*100)})
		}
	}
	return summarize(issues)
}

func ValidateBars(items []domain.DailyBar, opts Options) Report {
	var issues []Issue
	if opts.MinRows > 0 && len(items) < opts.MinRows {
		issues = append(issues, Issue{Level: LevelWarn, Field: "rows", Message: fmt.Sprintf("bar rows %d below expected %d", len(items), opts.MinRows)})
	}
	seen := make(map[string]struct{}, len(items))
	for _, item := range items {
		snapshot := domain.StockSnapshot{Code: item.Code, Date: item.Date, Close: item.Close, High: item.High, Low: item.Low}
		code := strings.TrimSpace(item.Code)
		date := normalizeDate(item.Date)
		if code == "" {
			issues = append(issues, issue(LevelError, snapshot, "code", "empty stock code"))
		}
		if date == "" {
			issues = append(issues, issue(LevelError, snapshot, "date", "empty date"))
		}
		if item.Open <= 0 || item.Close <= 0 {
			issues = append(issues, issue(LevelError, snapshot, "open_close", "open and close must be positive"))
		}
		if item.High > 0 && item.Low > 0 && item.High < item.Low {
			issues = append(issues, issue(LevelError, snapshot, "high_low", "high is lower than low"))
		}
		if item.High > 0 && (item.Open > item.High*1.2 || item.Close > item.High*1.2) {
			issues = append(issues, issue(LevelWarn, snapshot, "high", "open or close far above high"))
		}
		if item.Low > 0 && (item.Open < item.Low*0.8 || item.Close < item.Low*0.8) {
			issues = append(issues, issue(LevelWarn, snapshot, "low", "open or close far below low"))
		}
		key := code + "|" + date
		if key != "|" {
			if _, ok := seen[key]; ok {
				issues = append(issues, issue(LevelWarn, snapshot, "duplicate", "duplicate code and date"))
			}
			seen[key] = struct{}{}
		}
	}
	return summarize(issues)
}

func issue(level Level, item domain.StockSnapshot, field string, message string) Issue {
	return Issue{Level: level, Code: item.Code, Date: normalizeDate(item.Date), Field: field, Message: message}
}

func normalizeDate(date string) string {
	var b strings.Builder
	for _, r := range strings.TrimSpace(date) {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}
