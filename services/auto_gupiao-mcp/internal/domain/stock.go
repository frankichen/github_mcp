package domain

import "math"

const LotSize = 100

type StockSnapshot struct {
	Date                 string  `json:"date"`
	Code                 string  `json:"code"`
	Name                 string  `json:"name"`
	Market               string  `json:"market"`
	Close                float64 `json:"close"`
	PrevClose            float64 `json:"prev_close"`
	High                 float64 `json:"high"`
	Low                  float64 `json:"low"`
	ChangePct            float64 `json:"change_pct"`
	TurnoverRate         float64 `json:"turnover_rate"`
	VolumeRatio          float64 `json:"volume_ratio"`
	Amount               float64 `json:"amount"`
	MarketCap            float64 `json:"market_cap"`
	PE                   float64 `json:"pe"`
	PB                   float64 `json:"pb"`
	ROE                  float64 `json:"roe"`
	RevenueGrowth        float64 `json:"revenue_growth"`
	NetProfitGrowth      float64 `json:"net_profit_growth"`
	DebtAssetRatio       float64 `json:"debt_asset_ratio"`
	RSI6                 float64 `json:"rsi6"`
	MA5                  float64 `json:"ma5"`
	MA20                 float64 `json:"ma20"`
	MA60                 float64 `json:"ma60"`
	FiveDayPct           float64 `json:"five_day_pct"`
	TwentyDayPct         float64 `json:"twenty_day_pct"`
	ATR20Pct             float64 `json:"atr20_pct"`
	AvgAmplitude20Pct    float64 `json:"avg_amplitude20_pct"`
	MaxDrawdown20Pct     float64 `json:"max_drawdown20_pct"`
	MaxDailyDrop20Pct    float64 `json:"max_daily_drop20_pct"`
	CloseMA20DistancePct float64 `json:"close_ma20_distance_pct"`
	LimitUp              bool    `json:"limit_up"`
	LimitDown            bool    `json:"limit_down"`
	Suspended            bool    `json:"suspended"`
	ST                   bool    `json:"st"`
}

type Candidate struct {
	Snapshot        StockSnapshot `json:"snapshot"`
	Score           float64       `json:"score"`
	Reasons         []string      `json:"reasons"`
	SuggestedShares int           `json:"suggested_shares"`
	EstimatedCost   float64       `json:"estimated_cost"`
	RiskLevel       string        `json:"risk_level"`
}

type CostModel struct {
	CommissionRate float64 `json:"commission_rate"`
	MinCommission  float64 `json:"min_commission"`
	TransferRate   float64 `json:"transfer_rate"`
	StampDutyRate  float64 `json:"stamp_duty_rate"`
	SlippageRate   float64 `json:"slippage_rate"`
}

func DefaultCostModel() CostModel {
	return CostModel{
		CommissionRate: 0.00025,
		MinCommission:  5,
		TransferRate:   0.00001,
		StampDutyRate:  0.0005,
		SlippageRate:   0.001,
	}
}

func (m CostModel) EstimateBuy(grossAmount float64) float64 {
	if grossAmount <= 0 {
		return 0
	}
	commission := m.estimateCommission(grossAmount)
	return grossAmount + commission + grossAmount*m.TransferRate + grossAmount*m.SlippageRate
}

func (m CostModel) EstimateSell(grossAmount float64) float64 {
	if grossAmount <= 0 {
		return 0
	}
	commission := m.estimateCommission(grossAmount)
	return commission + grossAmount*m.TransferRate + grossAmount*m.StampDutyRate + grossAmount*m.SlippageRate
}

func (m CostModel) NetSellProceeds(grossAmount float64) float64 {
	if grossAmount <= 0 {
		return 0
	}
	fees := m.EstimateSell(grossAmount)
	if fees >= grossAmount {
		return 0
	}
	return grossAmount - fees
}

func (m CostModel) estimateCommission(grossAmount float64) float64 {
	commission := grossAmount * m.CommissionRate
	if commission < m.MinCommission {
		commission = m.MinCommission
	}
	return commission
}

func RoundLotShares(price float64, budget float64, cost CostModel) (shares int, totalCost float64) {
	if price <= 0 || budget <= 0 {
		return 0, 0
	}
	maxLots := int(math.Floor(budget / (price * LotSize)))
	for lots := maxLots; lots >= 1; lots-- {
		shares := lots * LotSize
		gross := float64(shares) * price
		total := cost.EstimateBuy(gross)
		if total <= budget {
			return shares, total
		}
	}
	return 0, 0
}

func IsTradable(s StockSnapshot) bool {
	return !s.Suspended && !s.ST && !s.LimitUp && !s.LimitDown && s.Close > 0
}
