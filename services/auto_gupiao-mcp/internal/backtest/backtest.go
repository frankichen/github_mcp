package backtest

import (
	"math"
	"sort"

	"github.com/frankichen/auto_gupiao/internal/domain"
	"github.com/frankichen/auto_gupiao/internal/indicator"
	"github.com/frankichen/auto_gupiao/internal/selector"
)

type Config struct {
	InitialCash       float64
	TopN              int
	MinScore          float64
	MaxPositionRatio  float64
	Cost              domain.CostModel
	AllowMultipleBuys bool
}

type Trade struct {
	Code         string   `json:"code"`
	BuyDate      string   `json:"buy_date"`
	SellDate     string   `json:"sell_date"`
	BuyPrice     float64  `json:"buy_price"`
	SellPrice    float64  `json:"sell_price"`
	Shares       int      `json:"shares"`
	BuyCost      float64  `json:"buy_cost"`
	SellProceeds float64  `json:"sell_proceeds"`
	NetProfit    float64  `json:"net_profit"`
	NetReturnPct float64  `json:"net_return_pct"`
	Score        float64  `json:"score"`
	RiskLevel    string   `json:"risk_level"`
	Reasons      []string `json:"reasons"`
}

type Result struct {
	InitialCash        float64  `json:"initial_cash"`
	FinalEquity        float64  `json:"final_equity"`
	TotalReturnPct     float64  `json:"total_return_pct"`
	MaxDrawdownPct     float64  `json:"max_drawdown_pct"`
	Trades             int      `json:"trades"`
	Wins               int      `json:"wins"`
	Losses             int      `json:"losses"`
	WinRatePct         float64  `json:"win_rate_pct"`
	AverageReturnPct   float64  `json:"average_return_pct"`
	AverageProfit      float64  `json:"average_profit"`
	AverageLoss        float64  `json:"average_loss"`
	ProfitFactor       *float64 `json:"profit_factor"`
	MaxConsecutiveLoss int      `json:"max_consecutive_loss"`
	TradesList         []Trade  `json:"trades_list"`
}

func Run(bars []domain.DailyBar, cfg Config) Result {
	cfg = normalizeConfig(cfg)
	bars = cleanBars(bars)
	byDate := groupBarsByDate(bars)
	dates := sortedDates(byDate)
	technicals := indicator.BuildTechnicals(bars)
	cash := cfg.InitialCash
	equityPeak := cash
	maxDrawdown := 0.0
	trades := make([]Trade, 0)
	consecutiveLoss := 0
	maxConsecutiveLoss := 0

	for i := 0; i < len(dates)-1; i++ {
		buyDate := dates[i]
		sellDate := dates[i+1]
		buyBars := byDate[buyDate]
		sellByCode := map[string]domain.DailyBar{}
		for _, bar := range byDate[sellDate] {
			sellByCode[bar.Code] = bar
		}

		snapshots := snapshotsFromBars(buyBars, technicals)
		selectCfg := selector.DefaultConfig()
		selectCfg.Cash = cash
		selectCfg.TopN = cfg.TopN
		selectCfg.MinScore = cfg.MinScore
		selectCfg.MaxPositionRatio = cfg.MaxPositionRatio
		selectCfg.Cost = cfg.Cost
		candidates := selector.Select(snapshots, selectCfg)
		if len(candidates) == 0 {
			continue
		}
		if !cfg.AllowMultipleBuys && len(candidates) > 1 {
			candidates = candidates[:1]
		}

		for _, candidate := range candidates {
			if candidate.EstimatedCost > cash || candidate.SuggestedShares <= 0 {
				continue
			}
			sellBar, ok := sellByCode[candidate.Snapshot.Code]
			if !ok || sellBar.Open <= 0 {
				continue
			}
			buyPrice := candidate.Snapshot.Close
			shares := candidate.SuggestedShares
			buyCost := cfg.Cost.EstimateBuy(float64(shares) * buyPrice)
			if buyCost > cash {
				continue
			}
			sellGross := float64(shares) * sellBar.Open
			sellProceeds := cfg.Cost.NetSellProceeds(sellGross)
			netProfit := sellProceeds - buyCost
			trade := Trade{
				Code:         candidate.Snapshot.Code,
				BuyDate:      buyDate,
				SellDate:     sellDate,
				BuyPrice:     round(buyPrice, 4),
				SellPrice:    round(sellBar.Open, 4),
				Shares:       shares,
				BuyCost:      round(buyCost, 2),
				SellProceeds: round(sellProceeds, 2),
				NetProfit:    round(netProfit, 2),
				NetReturnPct: round(netProfit/buyCost*100, 4),
				Score:        candidate.Score,
				RiskLevel:    candidate.RiskLevel,
				Reasons:      candidate.Reasons,
			}
			cash += netProfit
			trades = append(trades, trade)
			if trade.NetProfit < 0 {
				consecutiveLoss++
				if consecutiveLoss > maxConsecutiveLoss {
					maxConsecutiveLoss = consecutiveLoss
				}
			} else {
				consecutiveLoss = 0
			}
			if cash > equityPeak {
				equityPeak = cash
			}
			drawdown := 0.0
			if equityPeak > 0 {
				drawdown = (equityPeak - cash) / equityPeak * 100
			}
			if drawdown > maxDrawdown {
				maxDrawdown = drawdown
			}
		}
	}

	return summarize(cfg.InitialCash, cash, maxDrawdown, maxConsecutiveLoss, trades)
}

func normalizeConfig(cfg Config) Config {
	if cfg.InitialCash <= 0 {
		cfg.InitialCash = 10000
	}
	if cfg.TopN <= 0 {
		cfg.TopN = 1
	}
	if cfg.MinScore <= 0 {
		cfg.MinScore = 60
	}
	if cfg.MaxPositionRatio <= 0 || cfg.MaxPositionRatio > 1 {
		cfg.MaxPositionRatio = 0.30
	}
	if cfg.Cost == (domain.CostModel{}) {
		cfg.Cost = domain.DefaultCostModel()
	}
	return cfg
}

func cleanBars(bars []domain.DailyBar) []domain.DailyBar {
	out := make([]domain.DailyBar, 0, len(bars))
	for _, bar := range bars {
		bar.Date = normalizeDate(bar.Date)
		if bar.Code == "" || bar.Date == "" || bar.Close <= 0 {
			continue
		}
		out = append(out, bar)
	}
	return out
}

func groupBarsByDate(bars []domain.DailyBar) map[string][]domain.DailyBar {
	byDate := map[string][]domain.DailyBar{}
	for _, bar := range bars {
		byDate[bar.Date] = append(byDate[bar.Date], bar)
	}
	return byDate
}

func sortedDates(byDate map[string][]domain.DailyBar) []string {
	dates := make([]string, 0, len(byDate))
	for date := range byDate {
		dates = append(dates, date)
	}
	sort.Strings(dates)
	return dates
}

func snapshotsFromBars(bars []domain.DailyBar, technicals map[string]indicator.Technicals) []domain.StockSnapshot {
	snapshots := make([]domain.StockSnapshot, 0, len(bars))
	for _, bar := range bars {
		tech := technicals[bar.Code+"|"+normalizeDate(bar.Date)]
		snapshots = append(snapshots, domain.StockSnapshot{
			Date:         bar.Date,
			Code:         bar.Code,
			Close:        bar.Close,
			PrevClose:    bar.PrevClose,
			High:         bar.High,
			Low:          bar.Low,
			ChangePct:    bar.ChangePct,
			Amount:       bar.Amount,
			VolumeRatio:  1.5,
			TurnoverRate: 5,
			MA5:          tech.MA5,
			MA20:         tech.MA20,
			MA60:         tech.MA60,
			RSI6:         tech.RSI6,
			FiveDayPct:   tech.FiveDayPct,
			TwentyDayPct: tech.TwentyDayPct,
			PB:           2,
		})
	}
	return snapshots
}

func summarize(initialCash float64, finalCash float64, maxDrawdown float64, maxConsecutiveLoss int, trades []Trade) Result {
	wins := 0
	losses := 0
	totalReturn := 0.0
	totalProfit := 0.0
	totalLoss := 0.0
	grossProfit := 0.0
	grossLoss := 0.0
	for _, trade := range trades {
		totalReturn += trade.NetReturnPct
		if trade.NetProfit >= 0 {
			wins++
			totalProfit += trade.NetProfit
			grossProfit += trade.NetProfit
		} else {
			losses++
			totalLoss += trade.NetProfit
			grossLoss += -trade.NetProfit
		}
	}
	tradeCount := len(trades)
	winRate := 0.0
	avgReturn := 0.0
	avgProfit := 0.0
	avgLoss := 0.0
	var profitFactor *float64
	if tradeCount > 0 {
		winRate = float64(wins) / float64(tradeCount) * 100
		avgReturn = totalReturn / float64(tradeCount)
	}
	if wins > 0 {
		avgProfit = totalProfit / float64(wins)
	}
	if losses > 0 {
		avgLoss = totalLoss / float64(losses)
	}
	if grossLoss > 0 {
		value := round(grossProfit/grossLoss, 4)
		profitFactor = &value
	}
	return Result{
		InitialCash:        round(initialCash, 2),
		FinalEquity:        round(finalCash, 2),
		TotalReturnPct:     round((finalCash-initialCash)/initialCash*100, 4),
		MaxDrawdownPct:     round(maxDrawdown, 4),
		Trades:             tradeCount,
		Wins:               wins,
		Losses:             losses,
		WinRatePct:         round(winRate, 4),
		AverageReturnPct:   round(avgReturn, 4),
		AverageProfit:      round(avgProfit, 2),
		AverageLoss:        round(avgLoss, 2),
		ProfitFactor:       profitFactor,
		MaxConsecutiveLoss: maxConsecutiveLoss,
		TradesList:         trades,
	}
}

func normalizeDate(date string) string {
	out := ""
	for _, r := range date {
		if r >= '0' && r <= '9' {
			out += string(r)
		}
	}
	return out
}

func round(v float64, places int) float64 {
	if math.IsInf(v, 0) || math.IsNaN(v) {
		return v
	}
	p := math.Pow10(places)
	return math.Round(v*p) / p
}
