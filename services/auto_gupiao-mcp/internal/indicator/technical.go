package indicator

import (
	"math"
	"sort"
	"strings"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

const (
	DefaultRSIPeriod                 = 6
	TradingDaysInFiveSessionReturn   = 5
	TradingDaysInTwentySessionReturn = 20
)

type Technicals struct {
	MA5                  float64
	MA20                 float64
	MA60                 float64
	RSI6                 float64
	FiveDayPct           float64
	TwentyDayPct         float64
	ATR20Pct             float64
	AvgAmplitude20Pct    float64
	MaxDrawdown20Pct     float64
	MaxDailyDrop20Pct    float64
	CloseMA20DistancePct float64
}

// EnrichSnapshots fills MA5/MA20/MA60/RSI6/5-day return/20-day return for each snapshot.
// The history slice can be unordered. Indicators are calculated per stock code using bars
// sorted by trading date in ascending order. Moving averages include the current close.
func EnrichSnapshots(snapshots []domain.StockSnapshot, history []domain.DailyBar) []domain.StockSnapshot {
	if len(snapshots) == 0 || len(history) == 0 {
		return snapshots
	}
	technicals := BuildTechnicals(history)
	out := make([]domain.StockSnapshot, len(snapshots))
	copy(out, snapshots)
	for i := range out {
		key := key(out[i].Code, out[i].Date)
		value, ok := technicals[key]
		if !ok {
			continue
		}
		out[i].MA5 = value.MA5
		out[i].MA20 = value.MA20
		out[i].MA60 = value.MA60
		out[i].RSI6 = value.RSI6
		out[i].FiveDayPct = value.FiveDayPct
		out[i].TwentyDayPct = value.TwentyDayPct
		out[i].ATR20Pct = value.ATR20Pct
		out[i].AvgAmplitude20Pct = value.AvgAmplitude20Pct
		out[i].MaxDrawdown20Pct = value.MaxDrawdown20Pct
		out[i].MaxDailyDrop20Pct = value.MaxDailyDrop20Pct
		out[i].CloseMA20DistancePct = value.CloseMA20DistancePct
	}
	return out
}

func BuildTechnicals(history []domain.DailyBar) map[string]Technicals {
	byCode := make(map[string][]domain.DailyBar)
	for _, bar := range history {
		if bar.Code == "" || bar.Date == "" || bar.Close <= 0 {
			continue
		}
		byCode[bar.Code] = append(byCode[bar.Code], bar)
	}
	result := make(map[string]Technicals, len(history))
	for code, bars := range byCode {
		sort.SliceStable(bars, func(i, j int) bool {
			return normalizeDate(bars[i].Date) < normalizeDate(bars[j].Date)
		})
		closes := make([]float64, len(bars))
		for i, bar := range bars {
			closes[i] = bar.Close
		}
		for i, bar := range bars {
			ma20 := movingAverage(closes, i, 20)
			result[key(code, bar.Date)] = Technicals{
				MA5:                  round(movingAverage(closes, i, 5), 4),
				MA20:                 round(ma20, 4),
				MA60:                 round(movingAverage(closes, i, 60), 4),
				RSI6:                 round(rsi(closes, i, DefaultRSIPeriod), 4),
				FiveDayPct:           round(periodReturn(closes, i, TradingDaysInFiveSessionReturn), 4),
				TwentyDayPct:         round(periodReturn(closes, i, TradingDaysInTwentySessionReturn), 4),
				ATR20Pct:             round(atrPct(bars, i, 20), 4),
				AvgAmplitude20Pct:    round(avgAmplitudePct(bars, i, 20), 4),
				MaxDrawdown20Pct:     round(maxDrawdownPct(closes, i, 20), 4),
				MaxDailyDrop20Pct:    round(maxDailyDropPct(closes, i, 20), 4),
				CloseMA20DistancePct: round(distancePct(bar.Close, ma20), 4),
			}
		}
	}
	return result
}

func movingAverage(closes []float64, end int, window int) float64 {
	if window <= 0 || end < window-1 || end >= len(closes) {
		return 0
	}
	start := end - window + 1
	sum := 0.0
	for i := start; i <= end; i++ {
		sum += closes[i]
	}
	return sum / float64(window)
}

func periodReturn(closes []float64, end int, window int) float64 {
	if window <= 0 || end < window || end >= len(closes) {
		return 0
	}
	base := closes[end-window]
	if base <= 0 {
		return 0
	}
	return (closes[end] - base) / base * 100
}

func rsi(closes []float64, end int, period int) float64 {
	if period <= 0 || end < period || end >= len(closes) {
		return 0
	}
	gain := 0.0
	loss := 0.0
	start := end - period + 1
	for i := start; i <= end; i++ {
		change := closes[i] - closes[i-1]
		if change > 0 {
			gain += change
		} else {
			loss += -change
		}
	}
	avgGain := gain / float64(period)
	avgLoss := loss / float64(period)
	if avgLoss == 0 {
		if avgGain == 0 {
			return 50
		}
		return 100
	}
	rs := avgGain / avgLoss
	return 100 - 100/(1+rs)
}

func atrPct(bars []domain.DailyBar, end int, window int) float64 {
	if window <= 0 || end < window || end >= len(bars) {
		return 0
	}
	start := end - window + 1
	sumTR := 0.0
	for i := start; i <= end; i++ {
		prevClose := bars[i-1].Close
		tr := math.Max(bars[i].High-bars[i].Low, math.Max(math.Abs(bars[i].High-prevClose), math.Abs(bars[i].Low-prevClose)))
		sumTR += tr
	}
	atr := sumTR / float64(window)
	if bars[end].Close <= 0 {
		return 0
	}
	return atr / bars[end].Close * 100
}

func avgAmplitudePct(bars []domain.DailyBar, end int, window int) float64 {
	if window <= 0 || end < window-1 || end >= len(bars) {
		return 0
	}
	start := end - window + 1
	sum := 0.0
	count := 0
	for i := start; i <= end; i++ {
		if bars[i].Close <= 0 {
			continue
		}
		sum += (bars[i].High - bars[i].Low) / bars[i].Close * 100
		count++
	}
	if count == 0 {
		return 0
	}
	return sum / float64(count)
}

func maxDrawdownPct(closes []float64, end int, window int) float64 {
	if window <= 0 || end < window-1 || end >= len(closes) {
		return 0
	}
	start := end - window + 1
	peak := closes[start]
	maxDD := 0.0
	for i := start; i <= end; i++ {
		if closes[i] > peak {
			peak = closes[i]
		}
		if peak > 0 {
			dd := (peak - closes[i]) / peak * 100
			if dd > maxDD {
				maxDD = dd
			}
		}
	}
	return maxDD
}

func maxDailyDropPct(closes []float64, end int, window int) float64 {
	if window <= 0 || end < window || end >= len(closes) {
		return 0
	}
	start := end - window + 1
	maxDrop := 0.0
	for i := start; i <= end; i++ {
		if closes[i-1] <= 0 {
			continue
		}
		drop := (closes[i] - closes[i-1]) / closes[i-1] * 100
		if drop < maxDrop {
			maxDrop = drop
		}
	}
	return maxDrop
}

func distancePct(value float64, base float64) float64 {
	if base <= 0 {
		return 0
	}
	return (value - base) / base * 100
}

func key(code string, date string) string {
	return strings.TrimSpace(code) + "|" + normalizeDate(date)
}

func normalizeDate(date string) string {
	return strings.ReplaceAll(strings.TrimSpace(date), "-", "")
}

func round(v float64, places int) float64 {
	if v == 0 {
		return 0
	}
	p := math.Pow10(places)
	return math.Round(v*p) / p
}
