package paper

import (
	"math"
	"sort"
	"strings"

	"github.com/frankichen/auto_gupiao/internal/domain"
	"github.com/frankichen/auto_gupiao/internal/indicator"
	"github.com/frankichen/auto_gupiao/internal/selector"
)

const (
	SellAtOpen  = "open"
	SellAtClose = "close"
)

type Config struct {
	InitialCash               float64
	TopN                      int
	MinScore                  float64
	MaxPositionRatio          float64
	AllowMultipleBuys         bool
	StrategyProfile           string
	StrictEntry               bool
	SellPriceMode             string
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
	LargeLossFilter           bool
	LargeLossMaxReturnPct     float64
	LargeLossCooldownDays     int
	LossStreakPause           bool
	LossStreakThreshold       int
	LossStreakPauseDays       int
	EntryGuard                EntryGuard
	Cost                      domain.CostModel
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
	HoldingDays  int      `json:"holding_days"`
	ExitReason   string   `json:"exit_reason"`
	Score        float64  `json:"score"`
	RiskLevel    string   `json:"risk_level"`
	Reasons      []string `json:"reasons"`
}

type OpenPosition struct {
	Code                string   `json:"code"`
	BuyDate             string   `json:"buy_date"`
	CurrentDate         string   `json:"current_date"`
	BuyPrice            float64  `json:"buy_price"`
	CurrentPrice        float64  `json:"current_price"`
	Shares              int      `json:"shares"`
	BuyCost             float64  `json:"buy_cost"`
	MarketValue         float64  `json:"market_value"`
	EstimatedNetValue   float64  `json:"estimated_net_value"`
	UnrealizedProfit    float64  `json:"unrealized_profit"`
	UnrealizedReturnPct float64  `json:"unrealized_return_pct"`
	HoldingDays         int      `json:"holding_days"`
	Score               float64  `json:"score"`
	RiskLevel           string   `json:"risk_level"`
	Reasons             []string `json:"reasons"`
}

type FilterStat struct {
	Code     string `json:"code"`
	Reason   string `json:"reason"`
	Count    int    `json:"count"`
	LastDate string `json:"last_date"`
}

type DailyEquity struct {
	Date           string  `json:"date"`
	Cash           float64 `json:"cash"`
	MarketValue    float64 `json:"market_value"`
	Equity         float64 `json:"equity"`
	DailyReturnPct float64 `json:"daily_return_pct"`
	OpenPositions  int     `json:"open_positions"`
}

type Result struct {
	InitialCash        float64        `json:"initial_cash"`
	FinalEquity        float64        `json:"final_equity"`
	TotalReturnPct     float64        `json:"total_return_pct"`
	MaxDrawdownPct     float64        `json:"max_drawdown_pct"`
	Days               int            `json:"days"`
	Trades             int            `json:"trades"`
	Wins               int            `json:"wins"`
	Losses             int            `json:"losses"`
	WinRatePct         float64        `json:"win_rate_pct"`
	ProfitFactor       *float64       `json:"profit_factor"`
	MaxConsecutiveLoss int            `json:"max_consecutive_loss"`
	DailyEquity        []DailyEquity  `json:"daily_equity"`
	TradesList         []Trade        `json:"trades_list"`
	OpenPositions      []OpenPosition `json:"open_positions"`
	FilterStats        []FilterStat   `json:"filter_stats"`
}

type position struct {
	Code      string
	BuyDate   string
	BuyIndex  int
	BuyPrice  float64
	Shares    int
	BuyCost   float64
	Score     float64
	RiskLevel string
	Reasons   []string
}

func Run(bars []domain.DailyBar, cfg Config) Result {
	cfg = normalizeConfig(cfg)
	bars = cleanBars(bars)
	byDate := groupBarsByDate(bars)
	dates := sortedDates(byDate)
	technicals := indicator.BuildTechnicals(bars)
	cash := cfg.InitialCash
	positions := make([]position, 0)
	trades := make([]Trade, 0)
	daily := make([]DailyEquity, 0, len(dates))
	filterStats := map[string]*FilterStat{}
	cooldownUntil := map[string]int{}
	cooldownReason := map[string]string{}
	globalLossStreak := 0
	pauseNewBuysUntil := 0
	peak := cfg.InitialCash
	maxDrawdown := 0.0
	previousEquity := cfg.InitialCash

	for i, date := range dates {
		barsToday := byDate[date]
		barByCode := indexBars(barsToday)
		var remaining []position
		for _, pos := range positions {
			bar, ok := barByCode[pos.Code]
			if !ok {
				remaining = append(remaining, pos)
				continue
			}
			sellPrice := priceForSell(bar, cfg.SellPriceMode)
			if sellPrice <= 0 {
				remaining = append(remaining, pos)
				continue
			}
			holdingDays := i - pos.BuyIndex
			if !shouldSellPosition(pos, sellPrice, holdingDays, cfg) {
				remaining = append(remaining, pos)
				continue
			}
			exitReason := exitReason(pos, sellPrice, holdingDays, cfg)
			sellGross := float64(pos.Shares) * sellPrice
			sellProceeds := cfg.Cost.NetSellProceeds(sellGross)
			netProfit := sellProceeds - pos.BuyCost
			netReturnPct := percent(netProfit, pos.BuyCost)
			cash += sellProceeds
			globalLossStreak, pauseNewBuysUntil = updateLossStreak(netProfit, i, cfg, globalLossStreak, pauseNewBuysUntil)
			applyLossCooldown(pos.Code, i, exitReason, netReturnPct, netProfit, cfg, cooldownUntil, cooldownReason)
			trades = append(trades, Trade{Code: pos.Code, BuyDate: pos.BuyDate, SellDate: date, BuyPrice: round(pos.BuyPrice, 4), SellPrice: round(sellPrice, 4), Shares: pos.Shares, BuyCost: round(pos.BuyCost, 2), SellProceeds: round(sellProceeds, 2), NetProfit: round(netProfit, 2), NetReturnPct: round(netReturnPct, 4), HoldingDays: holdingDays, ExitReason: exitReason, Score: pos.Score, RiskLevel: pos.RiskLevel, Reasons: pos.Reasons})
		}
		positions = remaining

		if i < len(dates)-1 {
			snapshots := snapshotsFromBars(barsToday, technicals)
			selectCfg := selector.DefaultConfig()
			selectCfg.Cash = cash
			selectCfg.TopN = cfg.TopN
			selectCfg.MinScore = cfg.MinScore
			selectCfg.MaxPositionRatio = cfg.MaxPositionRatio
			selectCfg.Cost = cfg.Cost
			selectCfg.Profile = cfg.StrategyProfile
			selectCfg.StrictEntry = cfg.StrictEntry
			if selectCfg.Profile == selector.ProfileBasic {
				selectCfg.MinTurnoverRate = 0
			}
			candidates := selector.Select(snapshots, selectCfg)
			maxBuys := cfg.TopN
			if !cfg.AllowMultipleBuys {
				maxBuys = 1
			}
			openCodes := openPositionCodes(positions)
			bought := 0
			for _, candidate := range candidates {
				if maxBuys > 0 && bought >= maxBuys {
					break
				}
				code := candidate.Snapshot.Code
				if candidate.SuggestedShares <= 0 || candidate.Snapshot.Close <= 0 || openCodes[code] {
					continue
				}
				if reason := lossStreakPauseReason(cfg, i, pauseNewBuysUntil); reason != "" {
					recordFilter(filterStats, code, reason, date)
					continue
				}
				if reason := filterCandidateReason(code, trades, cfg, i, cooldownUntil, cooldownReason); reason != "" {
					recordFilter(filterStats, code, reason, date)
					continue
				}
				if reason := entryGuardReason(candidate.Snapshot, cfg.EntryGuard); reason != "" {
					recordFilter(filterStats, code, reason, date)
					continue
				}
				buyCost := cfg.Cost.EstimateBuy(float64(candidate.SuggestedShares) * candidate.Snapshot.Close)
				if buyCost < cfg.MinTradeAmount || buyCost > cash {
					continue
				}
				cash -= buyCost
				positions = append(positions, position{Code: code, BuyDate: date, BuyIndex: i, BuyPrice: candidate.Snapshot.Close, Shares: candidate.SuggestedShares, BuyCost: buyCost, Score: candidate.Score, RiskLevel: candidate.RiskLevel, Reasons: candidate.Reasons})
				openCodes[code] = true
				bought++
			}
		}

		marketValue := markToMarket(positions, barByCode)
		equity := cash + marketValue
		if equity > peak {
			peak = equity
		}
		if peak > 0 {
			drawdown := (peak - equity) / peak * 100
			if drawdown > maxDrawdown {
				maxDrawdown = drawdown
			}
		}
		daily = append(daily, DailyEquity{Date: date, Cash: round(cash, 2), MarketValue: round(marketValue, 2), Equity: round(equity, 2), DailyReturnPct: round(percent(equity-previousEquity, previousEquity), 4), OpenPositions: len(positions)})
		previousEquity = equity
	}

	finalEquity := cash
	if len(daily) > 0 {
		finalEquity = daily[len(daily)-1].Equity
	}
	openPositions := make([]OpenPosition, 0)
	if len(dates) > 0 {
		lastIndex := len(dates) - 1
		lastDate := dates[lastIndex]
		openPositions = buildOpenPositions(positions, indexBars(byDate[lastDate]), lastDate, lastIndex, cfg.Cost)
	}
	return summarize(cfg.InitialCash, finalEquity, maxDrawdown, trades, daily, openPositions, filterStatsList(filterStats))
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
	if cfg.SellPriceMode == "" {
		cfg.SellPriceMode = SellAtOpen
	}
	if cfg.StrategyProfile == "" {
		cfg.StrategyProfile = selector.ProfileFull
	}
	if cfg.MinTradeAmount <= 0 {
		cfg.MinTradeAmount = 1000
	}
	if cfg.MinHoldDays <= 0 {
		cfg.MinHoldDays = 3
	}
	if cfg.MaxHoldDays <= 0 {
		cfg.MaxHoldDays = 7
	}
	if cfg.MaxHoldDays < cfg.MinHoldDays {
		cfg.MaxHoldDays = cfg.MinHoldDays
	}
	if cfg.StopLossPct <= 0 {
		cfg.StopLossPct = 3.5
	}
	if cfg.TakeProfitPct <= 0 {
		cfg.TakeProfitPct = 8.0
	}
	if cfg.CooldownDays <= 0 {
		cfg.CooldownDays = 5
	}
	if cfg.StopLossCooldownDays <= 0 {
		cfg.StopLossCooldownDays = 30
	}
	if cfg.PoorPerformerMinTrades <= 0 {
		cfg.PoorPerformerMinTrades = 2
	}
	if cfg.PoorPerformerMaxNetProfit >= 0 {
		cfg.PoorPerformerMaxNetProfit = -300
	}
	if cfg.SingleLossMaxNetProfit >= 0 {
		cfg.SingleLossMaxNetProfit = -300
	}
	if cfg.SingleLossMaxReturnPct >= 0 {
		cfg.SingleLossMaxReturnPct = -5
	}
	if cfg.LargeLossMaxReturnPct >= 0 {
		cfg.LargeLossMaxReturnPct = -6
	}
	if cfg.LargeLossCooldownDays <= 0 {
		cfg.LargeLossCooldownDays = 15
	}
	if cfg.LossStreakThreshold <= 0 {
		cfg.LossStreakThreshold = 4
	}
	if cfg.LossStreakPauseDays <= 0 {
		cfg.LossStreakPauseDays = 2
	}
	if cfg.Cost == (domain.CostModel{}) {
		cfg.Cost = domain.DefaultCostModel()
	}
	return cfg
}

func summarize(initialCash, finalEquity, maxDrawdown float64, trades []Trade, daily []DailyEquity, openPositions []OpenPosition, filterStats []FilterStat) Result {
	wins, losses := 0, 0
	grossProfit, grossLoss := 0.0, 0.0
	consecutiveLoss, maxConsecutiveLoss := 0, 0
	for _, trade := range trades {
		if trade.NetProfit >= 0 {
			wins++
			grossProfit += trade.NetProfit
			consecutiveLoss = 0
		} else {
			losses++
			grossLoss += -trade.NetProfit
			consecutiveLoss++
			if consecutiveLoss > maxConsecutiveLoss {
				maxConsecutiveLoss = consecutiveLoss
			}
		}
	}
	var profitFactor *float64
	if grossLoss > 0 {
		value := round(grossProfit/grossLoss, 4)
		profitFactor = &value
	}
	winRate := 0.0
	if len(trades) > 0 {
		winRate = float64(wins) / float64(len(trades)) * 100
	}
	return Result{InitialCash: round(initialCash, 2), FinalEquity: round(finalEquity, 2), TotalReturnPct: round(percent(finalEquity-initialCash, initialCash), 4), MaxDrawdownPct: round(maxDrawdown, 4), Days: len(daily), Trades: len(trades), Wins: wins, Losses: losses, WinRatePct: round(winRate, 4), ProfitFactor: profitFactor, MaxConsecutiveLoss: maxConsecutiveLoss, DailyEquity: daily, TradesList: trades, OpenPositions: openPositions, FilterStats: filterStats}
}

func applyLossCooldown(code string, index int, exitReason string, netReturnPct float64, netProfit float64, cfg Config, cooldownUntil map[string]int, cooldownReason map[string]string) {
	if netProfit >= 0 {
		return
	}
	cooldownDays := cfg.CooldownDays
	reason := "loss_cooldown"
	if exitReason == "stop_loss" && cfg.StopLossCooldownDays > cooldownDays {
		cooldownDays = cfg.StopLossCooldownDays
		reason = "recent_stop_loss_cooldown"
	}
	if cfg.LargeLossFilter && exitReason == "stop_loss" && netReturnPct <= cfg.LargeLossMaxReturnPct && cfg.LargeLossCooldownDays > cooldownDays {
		cooldownDays = cfg.LargeLossCooldownDays
		reason = "large_stop_loss_cooldown"
	}
	if cooldownDays > 0 {
		cooldownUntil[code] = index + cooldownDays
		cooldownReason[code] = reason
	}
}

func updateLossStreak(netProfit float64, index int, cfg Config, globalLossStreak int, pauseNewBuysUntil int) (int, int) {
	if netProfit >= 0 {
		return 0, pauseNewBuysUntil
	}
	globalLossStreak++
	if cfg.LossStreakPause && globalLossStreak >= cfg.LossStreakThreshold {
		pauseNewBuysUntil = maxInt(pauseNewBuysUntil, index+cfg.LossStreakPauseDays)
	}
	return globalLossStreak, pauseNewBuysUntil
}

func lossStreakPauseReason(cfg Config, index int, pauseNewBuysUntil int) string {
	if cfg.LossStreakPause && index < pauseNewBuysUntil {
		return "loss_streak_pause"
	}
	return ""
}

func filterCandidateReason(code string, trades []Trade, cfg Config, index int, cooldownUntil map[string]int, cooldownReason map[string]string) string {
	if until, ok := cooldownUntil[code]; ok && index < until {
		if reason := cooldownReason[code]; reason != "" {
			return reason
		}
		return "loss_cooldown"
	}
	if cfg.SingleLossFilter && hasLargeSingleLoss(code, trades, cfg) {
		return "single_large_stop_loss"
	}
	if cfg.PoorPerformerFilter && hasPoorNetProfit(code, trades, cfg) {
		return "poor_performer"
	}
	if cfg.RepeatedStopLossFilter && hasRepeatedStopLoss(code, trades, cfg) {
		return "repeated_stop_loss_poor_performer"
	}
	return ""
}

func hasLargeSingleLoss(code string, trades []Trade, cfg Config) bool {
	for _, trade := range trades {
		if trade.Code != code || trade.ExitReason != "stop_loss" {
			continue
		}
		if trade.NetProfit <= cfg.SingleLossMaxNetProfit || trade.NetReturnPct <= cfg.SingleLossMaxReturnPct {
			return true
		}
	}
	return false
}

func isPoorPerformer(code string, trades []Trade, cfg Config) bool {
	return (cfg.PoorPerformerFilter && hasPoorNetProfit(code, trades, cfg)) || (cfg.RepeatedStopLossFilter && hasRepeatedStopLoss(code, trades, cfg))
}

func hasPoorNetProfit(code string, trades []Trade, cfg Config) bool {
	count := 0
	netProfit := 0.0
	for _, trade := range trades {
		if trade.Code != code {
			continue
		}
		count++
		netProfit += trade.NetProfit
	}
	return count >= cfg.PoorPerformerMinTrades && netProfit <= cfg.PoorPerformerMaxNetProfit
}

func hasRepeatedStopLoss(code string, trades []Trade, cfg Config) bool {
	stopLossCount := 0
	stopLossNetProfit := 0.0
	for _, trade := range trades {
		if trade.Code != code || trade.ExitReason != "stop_loss" {
			continue
		}
		stopLossCount++
		stopLossNetProfit += trade.NetProfit
	}
	return stopLossCount >= cfg.PoorPerformerMinTrades && stopLossNetProfit < 0
}

func recordFilter(stats map[string]*FilterStat, code string, reason string, date string) {
	key := code + "|" + reason
	item := stats[key]
	if item == nil {
		item = &FilterStat{Code: code, Reason: reason}
		stats[key] = item
	}
	item.Count++
	item.LastDate = date
}

func filterStatsList(stats map[string]*FilterStat) []FilterStat {
	out := make([]FilterStat, 0, len(stats))
	for _, item := range stats {
		out = append(out, *item)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Count == out[j].Count {
			if out[i].Code == out[j].Code {
				return out[i].Reason < out[j].Reason
			}
			return out[i].Code < out[j].Code
		}
		return out[i].Count > out[j].Count
	})
	return out
}

func buildOpenPositions(positions []position, bars map[string]domain.DailyBar, currentDate string, currentIndex int, cost domain.CostModel) []OpenPosition {
	out := make([]OpenPosition, 0, len(positions))
	for _, pos := range positions {
		price := pos.BuyPrice
		if bar, ok := bars[pos.Code]; ok && bar.Close > 0 {
			price = bar.Close
		}
		marketValue := float64(pos.Shares) * price
		estimatedNetValue := cost.NetSellProceeds(marketValue)
		unrealizedProfit := estimatedNetValue - pos.BuyCost
		out = append(out, OpenPosition{Code: pos.Code, BuyDate: pos.BuyDate, CurrentDate: currentDate, BuyPrice: round(pos.BuyPrice, 4), CurrentPrice: round(price, 4), Shares: pos.Shares, BuyCost: round(pos.BuyCost, 2), MarketValue: round(marketValue, 2), EstimatedNetValue: round(estimatedNetValue, 2), UnrealizedProfit: round(unrealizedProfit, 2), UnrealizedReturnPct: round(percent(unrealizedProfit, pos.BuyCost), 4), HoldingDays: currentIndex - pos.BuyIndex, Score: pos.Score, RiskLevel: pos.RiskLevel, Reasons: pos.Reasons})
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].UnrealizedProfit < out[j].UnrealizedProfit })
	return out
}

func shouldSellPosition(pos position, sellPrice float64, holdingDays int, cfg Config) bool {
	if holdingDays <= 0 || sellPrice <= 0 {
		return false
	}
	returnPct := percent(sellPrice-pos.BuyPrice, pos.BuyPrice)
	if cfg.StopLossPct > 0 && returnPct <= -cfg.StopLossPct {
		return true
	}
	if holdingDays < cfg.MinHoldDays {
		return false
	}
	if cfg.TakeProfitPct > 0 && returnPct >= cfg.TakeProfitPct {
		return true
	}
	return cfg.MaxHoldDays > 0 && holdingDays >= cfg.MaxHoldDays
}

func exitReason(pos position, sellPrice float64, holdingDays int, cfg Config) string {
	returnPct := percent(sellPrice-pos.BuyPrice, pos.BuyPrice)
	if cfg.StopLossPct > 0 && returnPct <= -cfg.StopLossPct {
		return "stop_loss"
	}
	if cfg.TakeProfitPct > 0 && returnPct >= cfg.TakeProfitPct {
		return "take_profit"
	}
	if cfg.MaxHoldDays > 0 && holdingDays >= cfg.MaxHoldDays {
		return "max_hold_days"
	}
	return "rule_exit"
}

func openPositionCodes(positions []position) map[string]bool {
	out := make(map[string]bool, len(positions))
	for _, pos := range positions {
		out[pos.Code] = true
	}
	return out
}

func cleanBars(bars []domain.DailyBar) []domain.DailyBar {
	out := make([]domain.DailyBar, 0, len(bars))
	for _, bar := range bars {
		bar.Date = normalizeDate(bar.Date)
		if bar.Code == "" || bar.Date == "" || bar.Open <= 0 || bar.Close <= 0 {
			continue
		}
		out = append(out, bar)
	}
	return out
}

func groupBarsByDate(bars []domain.DailyBar) map[string][]domain.DailyBar {
	out := map[string][]domain.DailyBar{}
	for _, bar := range bars {
		out[bar.Date] = append(out[bar.Date], bar)
	}
	return out
}

func sortedDates(byDate map[string][]domain.DailyBar) []string {
	dates := make([]string, 0, len(byDate))
	for date := range byDate {
		dates = append(dates, date)
	}
	sort.Strings(dates)
	return dates
}

func indexBars(bars []domain.DailyBar) map[string]domain.DailyBar {
	out := make(map[string]domain.DailyBar, len(bars))
	for _, bar := range bars {
		out[bar.Code] = bar
	}
	return out
}

func snapshotsFromBars(bars []domain.DailyBar, technicals map[string]indicator.Technicals) []domain.StockSnapshot {
	out := make([]domain.StockSnapshot, 0, len(bars))
	for _, bar := range bars {
		tech := technicals[bar.Code+"|"+normalizeDate(bar.Date)]
		out = append(out, domain.StockSnapshot{Date: bar.Date, Code: bar.Code, Close: bar.Close, PrevClose: bar.PrevClose, High: bar.High, Low: bar.Low, ChangePct: bar.ChangePct, Amount: bar.Amount, VolumeRatio: 1.5, TurnoverRate: 5, MA5: tech.MA5, MA20: tech.MA20, MA60: tech.MA60, RSI6: tech.RSI6, FiveDayPct: tech.FiveDayPct, TwentyDayPct: tech.TwentyDayPct, ATR20Pct: tech.ATR20Pct, AvgAmplitude20Pct: tech.AvgAmplitude20Pct, MaxDrawdown20Pct: tech.MaxDrawdown20Pct, MaxDailyDrop20Pct: tech.MaxDailyDrop20Pct, CloseMA20DistancePct: tech.CloseMA20DistancePct, PB: 2})
	}
	return out
}

func markToMarket(positions []position, bars map[string]domain.DailyBar) float64 {
	value := 0.0
	for _, pos := range positions {
		price := pos.BuyPrice
		if bar, ok := bars[pos.Code]; ok && bar.Close > 0 {
			price = bar.Close
		}
		value += float64(pos.Shares) * price
	}
	return value
}

func priceForSell(bar domain.DailyBar, mode string) float64 {
	if strings.EqualFold(mode, SellAtClose) {
		return bar.Close
	}
	if bar.Open > 0 {
		return bar.Open
	}
	return bar.Close
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

func percent(numerator, denominator float64) float64 {
	if denominator == 0 {
		return 0
	}
	return numerator / denominator * 100
}

func maxInt(a int, b int) int {
	if a > b {
		return a
	}
	return b
}

func round(v float64, places int) float64 {
	if math.IsInf(v, 0) || math.IsNaN(v) {
		return v
	}
	p := math.Pow10(places)
	return math.Round(v*p) / p
}
