package domain

// TradingDay represents one exchange calendar day.
// Date uses YYYYMMDD format to match most A-share data vendors.
type TradingDay struct {
	Exchange     string `json:"exchange"`
	Date         string `json:"date"`
	IsOpen       bool   `json:"is_open"`
	PreTradeDate string `json:"pre_trade_date"`
}
