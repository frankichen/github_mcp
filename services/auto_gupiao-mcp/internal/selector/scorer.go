package selector

import (
	"math"
	"sort"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

const (
	ProfileFull  = "full"
	ProfileBasic = "basic"
)

type ScorerConfig struct {
	Cash              float64
	MaxPositionRatio  float64
	TopN              int
	MinScore          float64
	MinTurnoverRate   float64
	MaxDebtAssetRatio float64
	Profile           string
	StrictEntry       bool
	Cost              domain.CostModel
}

func DefaultConfig() ScorerConfig {
	return ScorerConfig{
		Cash:              10000,
		MaxPositionRatio:  0.30,
		TopN:              5,
		MinScore:          60,
		MinTurnoverRate:   1.0,
		MaxDebtAssetRatio: 75,
		Profile:           ProfileFull,
		Cost:              domain.DefaultCostModel(),
	}
}

func Select(snapshots []domain.StockSnapshot, cfg ScorerConfig) []domain.Candidate {
	cfg = normalizeConfig(cfg)
	budget := cfg.Cash * cfg.MaxPositionRatio
	if budget <= 0 {
		budget = cfg.Cash
	}

	candidates := make([]domain.Candidate, 0, len(snapshots))
	for _, snapshot := range snapshots {
		if !domain.IsTradable(snapshot) {
			continue
		}
		shares, cost := domain.RoundLotShares(snapshot.Close, budget, cfg.Cost)
		if shares <= 0 {
			continue
		}
		candidate := ScoreWithProfile(snapshot, cfg.Profile)
		candidate.SuggestedShares = shares
		candidate.EstimatedCost = round(cost, 2)
		if cfg.MinTurnoverRate > 0 && snapshot.TurnoverRate < cfg.MinTurnoverRate {
			candidate.Score -= 10
			candidate.Reasons = append(candidate.Reasons, "low_turnover_penalty")
		}
		if snapshot.DebtAssetRatio > cfg.MaxDebtAssetRatio {
			candidate.Score -= 8
			candidate.Reasons = append(candidate.Reasons, "high_debt_penalty")
		}
		candidate.Score = clamp(candidate.Score, 0, 100)
		candidate.RiskLevel = riskLevel(candidate.Score, snapshot)
		if cfg.StrictEntry && !passesStrictEntry(candidate) {
			continue
		}
		if candidate.Score >= cfg.MinScore {
			candidates = append(candidates, candidate)
		}
	}

	sort.SliceStable(candidates, func(i, j int) bool {
		if candidates[i].Score == candidates[j].Score {
			return candidates[i].EstimatedCost < candidates[j].EstimatedCost
		}
		return candidates[i].Score > candidates[j].Score
	})
	if cfg.TopN > 0 && len(candidates) > cfg.TopN {
		return candidates[:cfg.TopN]
	}
	return candidates
}

func passesStrictEntry(candidate domain.Candidate) bool {
	if candidate.Score < 90 || candidate.RiskLevel == "high" {
		return false
	}
	required := []string{"close_above_ma20", "ma5_above_ma20", "ma20_above_ma60", "rsi_in_momentum_zone"}
	for _, reason := range required {
		if !hasReason(candidate.Reasons, reason) {
			return false
		}
	}
	for _, blocked := range []string{"rsi_overheated", "abnormal_volume_ratio", "weak_or_overheated_intraday", "twenty_day_extreme", "low_turnover_penalty", "high_debt_penalty", "high_atr20", "high_20d_drawdown", "large_20d_drop", "far_above_ma20"} {
		if hasReason(candidate.Reasons, blocked) {
			return false
		}
	}
	return true
}

func hasReason(reasons []string, want string) bool {
	for _, reason := range reasons {
		if reason == want {
			return true
		}
	}
	return false
}

func Score(s domain.StockSnapshot) domain.Candidate {
	return ScoreWithProfile(s, ProfileFull)
}

func ScoreWithProfile(s domain.StockSnapshot, profile string) domain.Candidate {
	if profile == ProfileBasic {
		return ScoreBasic(s)
	}
	return ScoreFull(s)
}

func ScoreFull(s domain.StockSnapshot) domain.Candidate {
	score := 50.0
	reasons := make([]string, 0, 12)

	if s.Close > s.MA20 && s.MA20 > 0 {
		score += 10
		reasons = append(reasons, "close_above_ma20")
	}
	if s.MA5 > s.MA20 && s.MA20 > 0 {
		score += 8
		reasons = append(reasons, "ma5_above_ma20")
	}
	if s.MA20 > s.MA60 && s.MA60 > 0 {
		score += 6
		reasons = append(reasons, "ma20_above_ma60")
	}
	if s.VolumeRatio >= 1.2 && s.VolumeRatio <= 2.8 {
		score += 8
		reasons = append(reasons, "healthy_volume_ratio")
	} else if s.VolumeRatio > 4.0 {
		score -= 8
		reasons = append(reasons, "abnormal_volume_ratio")
	}
	if s.TurnoverRate >= 2 && s.TurnoverRate <= 12 {
		score += 7
		reasons = append(reasons, "active_turnover")
	}
	if s.RSI6 >= 45 && s.RSI6 <= 72 {
		score += 6
		reasons = append(reasons, "rsi_in_momentum_zone")
	} else if s.RSI6 > 82 {
		score -= 10
		reasons = append(reasons, "rsi_overheated")
	}
	if s.ChangePct >= 0.5 && s.ChangePct <= 5.5 {
		score += 8
		reasons = append(reasons, "moderate_intraday_strength")
	} else if s.ChangePct > 8.0 || s.ChangePct < -4.0 {
		score -= 8
		reasons = append(reasons, "weak_or_overheated_intraday")
	}
	if s.FiveDayPct > 0 && s.FiveDayPct <= 15 {
		score += 5
		reasons = append(reasons, "short_term_uptrend")
	}
	if s.TwentyDayPct > 30 || s.TwentyDayPct < -20 {
		score -= 6
		reasons = append(reasons, "twenty_day_extreme")
	}
	if s.ATR20Pct > 0 && s.ATR20Pct > 6 {
		score -= 8
		reasons = append(reasons, "high_atr20")
	}
	if s.MaxDrawdown20Pct > 18 {
		score -= 8
		reasons = append(reasons, "high_20d_drawdown")
	}
	if s.MaxDailyDrop20Pct < -8 {
		score -= 8
		reasons = append(reasons, "large_20d_drop")
	}
	if s.CloseMA20DistancePct > 12 {
		score -= 6
		reasons = append(reasons, "far_above_ma20")
	}
	if s.ROE >= 8 {
		score += 4
		reasons = append(reasons, "roe_supported")
	}
	if s.NetProfitGrowth > 0 || s.RevenueGrowth > 0 {
		score += 3
		reasons = append(reasons, "growth_supported")
	}
	if s.PB > 0 && s.PB <= 8 {
		score += 2
		reasons = append(reasons, "valuation_not_extreme")
	}

	return domain.Candidate{Snapshot: s, Score: round(clamp(score, 0, 100), 2), Reasons: reasons}
}

func ScoreBasic(s domain.StockSnapshot) domain.Candidate {
	score := 45.0
	reasons := make([]string, 0, 8)

	if s.Close > 0 {
		score += 3
		reasons = append(reasons, "valid_price")
	}
	if s.Amount >= 100000000 {
		score += 8
		reasons = append(reasons, "active_amount")
	} else if s.Amount > 0 && s.Amount < 20000000 {
		score -= 8
		reasons = append(reasons, "low_amount_penalty")
	}
	if s.ChangePct >= 0.3 && s.ChangePct <= 5.5 {
		score += 8
		reasons = append(reasons, "moderate_intraday_strength")
	} else if s.ChangePct > 8.0 || s.ChangePct < -5.0 {
		score -= 8
		reasons = append(reasons, "weak_or_overheated_intraday")
	}
	if s.TurnoverRate >= 1 && s.TurnoverRate <= 15 {
		score += 7
		reasons = append(reasons, "usable_turnover")
	}
	if s.VolumeRatio >= 1.1 && s.VolumeRatio <= 3.5 {
		score += 6
		reasons = append(reasons, "usable_volume_ratio")
	}
	if s.Close > s.MA20 && s.MA20 > 0 {
		score += 8
		reasons = append(reasons, "close_above_ma20")
	}
	if s.MA5 > s.MA20 && s.MA20 > 0 {
		score += 5
		reasons = append(reasons, "ma5_above_ma20")
	}
	if s.RSI6 >= 45 && s.RSI6 <= 78 {
		score += 4
		reasons = append(reasons, "rsi_usable")
	} else if s.RSI6 > 85 {
		score -= 8
		reasons = append(reasons, "rsi_overheated")
	}
	if s.FiveDayPct > 0 && s.FiveDayPct <= 18 {
		score += 4
		reasons = append(reasons, "short_term_uptrend")
	}
	if s.PB > 0 && s.PB <= 10 {
		score += 2
		reasons = append(reasons, "valuation_not_extreme")
	}

	return domain.Candidate{Snapshot: s, Score: round(clamp(score, 0, 100), 2), Reasons: reasons}
}

func normalizeConfig(cfg ScorerConfig) ScorerConfig {
	defaults := DefaultConfig()
	if cfg.Cash <= 0 {
		cfg.Cash = defaults.Cash
	}
	if cfg.MaxPositionRatio <= 0 || cfg.MaxPositionRatio > 1 {
		cfg.MaxPositionRatio = defaults.MaxPositionRatio
	}
	if cfg.TopN == 0 {
		cfg.TopN = defaults.TopN
	}
	if cfg.MinScore <= 0 {
		cfg.MinScore = defaults.MinScore
	}
	if cfg.MinTurnoverRate <= 0 && cfg.Profile != ProfileBasic {
		cfg.MinTurnoverRate = defaults.MinTurnoverRate
	}
	if cfg.MaxDebtAssetRatio <= 0 {
		cfg.MaxDebtAssetRatio = defaults.MaxDebtAssetRatio
	}
	if cfg.Profile == "" {
		cfg.Profile = defaults.Profile
	}
	if cfg.Cost == (domain.CostModel{}) {
		cfg.Cost = defaults.Cost
	}
	return cfg
}

func riskLevel(score float64, s domain.StockSnapshot) string {
	if score >= 82 && s.ChangePct <= 5.5 && s.VolumeRatio <= 2.8 && (s.ATR20Pct == 0 || s.ATR20Pct <= 6) && s.MaxDrawdown20Pct <= 18 {
		return "low"
	}
	if score >= 68 {
		return "medium"
	}
	return "high"
}

func clamp(v, min, max float64) float64 {
	if v < min {
		return min
	}
	if v > max {
		return max
	}
	return v
}

func round(v float64, places int) float64 {
	p := math.Pow10(places)
	return math.Round(v*p) / p
}
