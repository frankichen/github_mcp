package paper

import (
	"fmt"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

type EntryGuard struct {
	MaxATR20Pct             float64 `json:"max_atr20_pct,omitempty"`
	MaxDrawdown20Pct        float64 `json:"max_drawdown20_pct,omitempty"`
	MaxDailyDrop20Pct       float64 `json:"max_daily_drop20_pct,omitempty"`
	MaxCloseMA20DistancePct float64 `json:"max_close_ma20_distance_pct,omitempty"`
	MaxFiveDayPct           float64 `json:"max_five_day_pct,omitempty"`
	MaxTwentyDayPct         float64 `json:"max_twenty_day_pct,omitempty"`
}

func entryGuardReason(s domain.StockSnapshot, guard EntryGuard) string {
	if guard.MaxATR20Pct > 0 && s.ATR20Pct > guard.MaxATR20Pct {
		return fmt.Sprintf("entry_atr20_gt_%.2f", guard.MaxATR20Pct)
	}
	if guard.MaxDrawdown20Pct > 0 && s.MaxDrawdown20Pct > guard.MaxDrawdown20Pct {
		return fmt.Sprintf("entry_drawdown20_gt_%.2f", guard.MaxDrawdown20Pct)
	}
	if guard.MaxDailyDrop20Pct < 0 && s.MaxDailyDrop20Pct < guard.MaxDailyDrop20Pct {
		return fmt.Sprintf("entry_daily_drop20_lt_%.2f", guard.MaxDailyDrop20Pct)
	}
	if guard.MaxCloseMA20DistancePct > 0 && s.CloseMA20DistancePct > guard.MaxCloseMA20DistancePct {
		return fmt.Sprintf("entry_close_ma20_distance_gt_%.2f", guard.MaxCloseMA20DistancePct)
	}
	if guard.MaxFiveDayPct > 0 && s.FiveDayPct > guard.MaxFiveDayPct {
		return fmt.Sprintf("entry_five_day_gt_%.2f", guard.MaxFiveDayPct)
	}
	if guard.MaxTwentyDayPct > 0 && s.TwentyDayPct > guard.MaxTwentyDayPct {
		return fmt.Sprintf("entry_twenty_day_gt_%.2f", guard.MaxTwentyDayPct)
	}
	return ""
}

func latestSellDate(result Result) string {
	latest := ""
	for _, trade := range result.TradesList {
		if trade.SellDate > latest {
			latest = trade.SellDate
		}
	}
	return latest
}
