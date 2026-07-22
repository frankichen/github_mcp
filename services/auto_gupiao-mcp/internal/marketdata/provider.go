package marketdata

import (
	"context"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

// Provider hides the concrete market data vendor from strategy code.
// Implementations can be backed by Tushare, a broker quotation API, CSV files,
// or a local cache. Strategy modules should depend on this interface only.
type Provider interface {
	ListStocks(ctx context.Context) ([]domain.StockBasic, error)
	DailyBars(ctx context.Context, code string, startDate string, endDate string) ([]domain.DailyBar, error)
	DailyBasics(ctx context.Context, tradeDate string) ([]domain.Fundamental, error)
	TradeCalendar(ctx context.Context, exchange string, startDate string, endDate string) ([]domain.TradingDay, error)
	ListSnapshots(ctx context.Context, tradeDate string) ([]domain.StockSnapshot, error)
}
