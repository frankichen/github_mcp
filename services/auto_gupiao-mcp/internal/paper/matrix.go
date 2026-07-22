package paper

import (
	"sort"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

type MatrixCase struct {
	Name                string     `json:"name"`
	RepeatedFilter      bool       `json:"repeated_filter"`
	LargeLossFilter     bool       `json:"large_loss_filter"`
	LossStreakPause     bool       `json:"loss_streak_pause"`
	OverrideRiskFilters bool       `json:"override_risk_filters,omitempty"`
	EntryGuard          EntryGuard `json:"entry_guard,omitempty"`
}

type MatrixMetric struct {
	Name                    string       `json:"name"`
	RepeatedFilter          bool         `json:"repeated_filter"`
	LargeLossFilter         bool         `json:"large_loss_filter"`
	LossStreakPause         bool         `json:"loss_streak_pause"`
	TotalReturnPct          float64      `json:"total_return_pct"`
	MaxDrawdownPct          float64      `json:"max_drawdown_pct"`
	ProfitFactor            *float64     `json:"profit_factor"`
	MaxConsecutiveLoss      int          `json:"max_consecutive_loss"`
	Trades                  int          `json:"trades"`
	StopLossCount           int          `json:"stop_loss_count"`
	StopLossNetProfit       float64      `json:"stop_loss_net_profit"`
	TakeProfitCount         int          `json:"take_profit_count"`
	TakeProfitNetProfit     float64      `json:"take_profit_net_profit"`
	OpenPositions           int          `json:"open_positions"`
	LatestSellDate          string       `json:"latest_sell_date"`
	FilterStatsByReason     []ReasonStat `json:"filter_stats_by_reason"`
	FilterStatsByCodeReason []FilterStat `json:"filter_stats_by_code_reason"`
	Result                  Result       `json:"-"`
}

type ReasonStat struct {
	Reason   string `json:"reason"`
	Count    int    `json:"count"`
	LastDate string `json:"last_date"`
}

func DefaultMatrixCases() []MatrixCase {
	return []MatrixCase{
		{Name: "baseline"},
		{Name: "atr_guard", EntryGuard: EntryGuard{MaxATR20Pct: 8}},
		{Name: "drawdown20_guard", EntryGuard: EntryGuard{MaxDrawdown20Pct: 18}},
		{Name: "daily_drop_guard", EntryGuard: EntryGuard{MaxDailyDrop20Pct: -7}},
		{Name: "close_ma20_distance_guard", EntryGuard: EntryGuard{MaxCloseMA20DistancePct: 12}},
		{Name: "overheat_guard", EntryGuard: EntryGuard{MaxFiveDayPct: 15, MaxTwentyDayPct: 35}},
		{Name: "light_combined_guard", EntryGuard: EntryGuard{MaxATR20Pct: 8, MaxDrawdown20Pct: 18, MaxDailyDrop20Pct: -7, MaxCloseMA20DistancePct: 12}},
	}
}

func RunMatrix(bars []domain.DailyBar, base Config, cases []MatrixCase) []MatrixMetric {
	if len(cases) == 0 {
		cases = DefaultMatrixCases()
	}
	out := make([]MatrixMetric, 0, len(cases))
	for _, item := range cases {
		cfg := base
		if item.OverrideRiskFilters || item.RepeatedFilter || item.LargeLossFilter || item.LossStreakPause {
			cfg.RepeatedStopLossFilter = item.RepeatedFilter
			cfg.LargeLossFilter = item.LargeLossFilter
			cfg.LossStreakPause = item.LossStreakPause
		}
		cfg.EntryGuard = item.EntryGuard
		result := Run(bars, cfg)
		out = append(out, summarizeMatrix(item, result))
	}
	return out
}

func summarizeMatrix(item MatrixCase, result Result) MatrixMetric {
	metric := MatrixMetric{
		Name:                    item.Name,
		RepeatedFilter:          item.RepeatedFilter,
		LargeLossFilter:         item.LargeLossFilter,
		LossStreakPause:         item.LossStreakPause,
		TotalReturnPct:          result.TotalReturnPct,
		MaxDrawdownPct:          result.MaxDrawdownPct,
		ProfitFactor:            result.ProfitFactor,
		MaxConsecutiveLoss:      result.MaxConsecutiveLoss,
		Trades:                  result.Trades,
		OpenPositions:           len(result.OpenPositions),
		LatestSellDate:          latestSellDate(result),
		FilterStatsByCodeReason: result.FilterStats,
		Result:                  result,
	}
	reasons := map[string]*ReasonStat{}
	for _, trade := range result.TradesList {
		switch trade.ExitReason {
		case "stop_loss":
			metric.StopLossCount++
			metric.StopLossNetProfit = round(metric.StopLossNetProfit+trade.NetProfit, 2)
		case "take_profit":
			metric.TakeProfitCount++
			metric.TakeProfitNetProfit = round(metric.TakeProfitNetProfit+trade.NetProfit, 2)
		}
	}
	for _, stat := range result.FilterStats {
		item := reasons[stat.Reason]
		if item == nil {
			item = &ReasonStat{Reason: stat.Reason}
			reasons[stat.Reason] = item
		}
		item.Count += stat.Count
		if stat.LastDate > item.LastDate {
			item.LastDate = stat.LastDate
		}
	}
	for _, item := range reasons {
		metric.FilterStatsByReason = append(metric.FilterStatsByReason, *item)
	}
	sort.SliceStable(metric.FilterStatsByReason, func(i, j int) bool {
		if metric.FilterStatsByReason[i].Count == metric.FilterStatsByReason[j].Count {
			return metric.FilterStatsByReason[i].Reason < metric.FilterStatsByReason[j].Reason
		}
		return metric.FilterStatsByReason[i].Count > metric.FilterStatsByReason[j].Count
	})
	return metric
}
