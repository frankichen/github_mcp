package domain

type StockBasic struct {
	Code       string `json:"code"`
	Symbol     string `json:"symbol"`
	Name       string `json:"name"`
	Area       string `json:"area"`
	Industry   string `json:"industry"`
	Market     string `json:"market"`
	ListDate   string `json:"list_date"`
	ListStatus string `json:"list_status"`
	IsHS       string `json:"is_hs"`
}

type DailyBar struct {
	Code      string  `json:"code"`
	Date      string  `json:"date"`
	Open      float64 `json:"open"`
	High      float64 `json:"high"`
	Low       float64 `json:"low"`
	Close     float64 `json:"close"`
	PrevClose float64 `json:"prev_close"`
	Change    float64 `json:"change"`
	ChangePct float64 `json:"change_pct"`
	Volume    float64 `json:"volume"`
	Amount    float64 `json:"amount"`
}

type Fundamental struct {
	Code            string  `json:"code"`
	Date            string  `json:"date"`
	TurnoverRate    float64 `json:"turnover_rate"`
	VolumeRatio     float64 `json:"volume_ratio"`
	PE              float64 `json:"pe"`
	PB              float64 `json:"pb"`
	MarketCap       float64 `json:"market_cap"`
	ROE             float64 `json:"roe"`
	RevenueGrowth   float64 `json:"revenue_growth"`
	NetProfitGrowth float64 `json:"net_profit_growth"`
	DebtAssetRatio  float64 `json:"debt_asset_ratio"`
}
