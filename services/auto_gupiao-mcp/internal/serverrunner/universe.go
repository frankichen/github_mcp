package serverrunner

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"github.com/frankichen/auto_gupiao/internal/appconfig"
	"github.com/frankichen/auto_gupiao/internal/dataset"
	"github.com/frankichen/auto_gupiao/internal/domain"
)

const (
	UniverseCustom = "custom"
	UniverseAllA   = "all_a"
)

type snapshotProvider interface {
	ListSnapshots(context.Context, string) ([]domain.StockSnapshot, error)
}

func resolveUniverseCodes(ctx context.Context, cfg appconfig.Config, provider any) ([]string, error) {
	universe := strings.TrimSpace(strings.ToLower(cfg.Data.Universe))
	if universe == "" {
		universe = UniverseCustom
	}
	inline := dataset.MergeCodes(cfg.Data.Codes, mustReadCodesFile(cfg.Data.CodesFile))
	switch universe {
	case UniverseCustom:
		if len(inline) == 0 {
			return nil, fmt.Errorf("data.codes or data.codes_file is required when data.universe is custom")
		}
		return inline, nil
	case UniverseAllA:
		snapshotSource, ok := provider.(snapshotProvider)
		if !ok {
			return nil, fmt.Errorf("data.universe all_a requires a provider that supports ListSnapshots")
		}
		snapshots, err := snapshotSource.ListSnapshots(ctx, cfg.Report.TradeDate)
		if err != nil {
			return nil, fmt.Errorf("load all_a universe snapshots: %w", err)
		}
		return selectUniverseCodes(snapshots, cfg.Data), nil
	default:
		return nil, fmt.Errorf("unsupported data.universe %q", cfg.Data.Universe)
	}
}

func selectUniverseCodes(snapshots []domain.StockSnapshot, cfg appconfig.DataConfig) []string {
	items := make([]domain.StockSnapshot, 0, len(snapshots))
	for _, snapshot := range snapshots {
		if !domain.IsTradable(snapshot) {
			continue
		}
		if cfg.MinUniversePrice > 0 && snapshot.Close < cfg.MinUniversePrice {
			continue
		}
		if cfg.MaxUniversePrice > 0 && snapshot.Close > cfg.MaxUniversePrice {
			continue
		}
		if cfg.MinUniverseAmount > 0 && snapshot.Amount < cfg.MinUniverseAmount {
			continue
		}
		items = append(items, snapshot)
	}
	sort.SliceStable(items, func(i, j int) bool {
		if items[i].Amount == items[j].Amount {
			return items[i].Code < items[j].Code
		}
		return items[i].Amount > items[j].Amount
	})
	limit := cfg.UniverseLimit
	if limit > 0 && len(items) > limit {
		items = items[:limit]
	}
	codes := make([]string, 0, len(items))
	seen := map[string]struct{}{}
	for _, item := range items {
		code := strings.TrimSpace(item.Code)
		if code == "" {
			continue
		}
		if _, ok := seen[code]; ok {
			continue
		}
		seen[code] = struct{}{}
		codes = append(codes, code)
	}
	return codes
}
