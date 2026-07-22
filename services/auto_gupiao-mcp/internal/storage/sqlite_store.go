package storage

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/dataset"
	"github.com/frankichen/auto_gupiao/internal/paper"
	"github.com/frankichen/auto_gupiao/internal/report"
	_ "modernc.org/sqlite"
)

type SQLiteStore struct {
	Path string
}

type ReportURLs struct {
	MarkdownURL string
	TradesURL   string
	EquityURL   string
}

type DailyRunRecord struct {
	GeneratedAt time.Time
	TradeDate   string
	Bars        dataset.Summary
	Paper       paper.Result
	Report      report.Manifest
	ReportURLs  ReportURLs
	RiskLevel   string
	Conclusion  string
}

type RunSummary struct {
	ID                 int64
	GeneratedAt        string
	TradeDate          string
	DataStart          string
	DataEnd            string
	StockPool          string
	BarsRows           int
	InitialCash        float64
	FinalEquity        float64
	TotalReturnPct     float64
	MaxDrawdownPct     float64
	Trades             int
	Wins               int
	Losses             int
	WinRatePct         float64
	ProfitFactor       sql.NullFloat64
	MaxConsecutiveLoss int
	RiskLevel          string
	Conclusion         string
	MarkdownURL        string
	TradesURL          string
	EquityURL          string
}

type RunDetail struct {
	Summary      RunSummary
	Equity       []paper.DailyEquity
	Trades       []paper.Trade
	ByCode       []report.AttributionRow
	ByExitReason []report.AttributionRow
}

func NewSQLiteStore(path string) SQLiteStore {
	return SQLiteStore{Path: path}
}

func (s SQLiteStore) Open(ctx context.Context) (*sql.DB, error) {
	if strings.TrimSpace(s.Path) == "" {
		return nil, fmt.Errorf("empty sqlite path")
	}
	if err := os.MkdirAll(filepath.Dir(s.Path), 0o755); err != nil {
		return nil, fmt.Errorf("create sqlite dir: %w", err)
	}
	db, err := sql.Open("sqlite", s.Path)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	if _, err := db.ExecContext(ctx, "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;"); err != nil {
		db.Close()
		return nil, fmt.Errorf("configure sqlite: %w", err)
	}
	if err := ensureSchema(ctx, db); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func (s SQLiteStore) SaveDailyRun(ctx context.Context, record DailyRunRecord) (int64, error) {
	db, err := s.Open(ctx)
	if err != nil {
		return 0, err
	}
	defer db.Close()
	return saveDailyRun(ctx, db, record)
}

func (s SQLiteStore) ListRuns(ctx context.Context, limit int) ([]RunSummary, error) {
	db, err := s.Open(ctx)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	if limit <= 0 || limit > 200 {
		limit = 60
	}
	rows, err := db.QueryContext(ctx, `SELECT id, generated_at, trade_date, data_start, data_end, stock_pool, bars_rows,
initial_cash, final_equity, total_return_pct, max_drawdown_pct, trades, wins, losses, win_rate_pct,
profit_factor, max_consecutive_loss, risk_level, conclusion, markdown_url, trades_url, equity_url
FROM daily_runs ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, fmt.Errorf("query runs: %w", err)
	}
	defer rows.Close()
	out := make([]RunSummary, 0)
	for rows.Next() {
		var item RunSummary
		if err := rows.Scan(&item.ID, &item.GeneratedAt, &item.TradeDate, &item.DataStart, &item.DataEnd, &item.StockPool, &item.BarsRows,
			&item.InitialCash, &item.FinalEquity, &item.TotalReturnPct, &item.MaxDrawdownPct, &item.Trades, &item.Wins, &item.Losses, &item.WinRatePct,
			&item.ProfitFactor, &item.MaxConsecutiveLoss, &item.RiskLevel, &item.Conclusion, &item.MarkdownURL, &item.TradesURL, &item.EquityURL); err != nil {
			return nil, fmt.Errorf("scan run: %w", err)
		}
		out = append(out, item)
	}
	return out, rows.Err()
}

func (s SQLiteStore) GetRun(ctx context.Context, id int64) (RunDetail, error) {
	db, err := s.Open(ctx)
	if err != nil {
		return RunDetail{}, err
	}
	defer db.Close()
	var summary RunSummary
	err = db.QueryRowContext(ctx, `SELECT id, generated_at, trade_date, data_start, data_end, stock_pool, bars_rows,
initial_cash, final_equity, total_return_pct, max_drawdown_pct, trades, wins, losses, win_rate_pct,
profit_factor, max_consecutive_loss, risk_level, conclusion, markdown_url, trades_url, equity_url
FROM daily_runs WHERE id = ?`, id).Scan(&summary.ID, &summary.GeneratedAt, &summary.TradeDate, &summary.DataStart, &summary.DataEnd, &summary.StockPool, &summary.BarsRows,
		&summary.InitialCash, &summary.FinalEquity, &summary.TotalReturnPct, &summary.MaxDrawdownPct, &summary.Trades, &summary.Wins, &summary.Losses, &summary.WinRatePct,
		&summary.ProfitFactor, &summary.MaxConsecutiveLoss, &summary.RiskLevel, &summary.Conclusion, &summary.MarkdownURL, &summary.TradesURL, &summary.EquityURL)
	if err != nil {
		return RunDetail{}, fmt.Errorf("get run: %w", err)
	}
	equity, err := queryEquity(ctx, db, id)
	if err != nil {
		return RunDetail{}, err
	}
	trades, err := queryTrades(ctx, db, id)
	if err != nil {
		return RunDetail{}, err
	}
	byCode, err := queryAttribution(ctx, db, id, "code")
	if err != nil {
		return RunDetail{}, err
	}
	byExit, err := queryAttribution(ctx, db, id, "exit_reason")
	if err != nil {
		return RunDetail{}, err
	}
	return RunDetail{Summary: summary, Equity: equity, Trades: trades, ByCode: byCode, ByExitReason: byExit}, nil
}

func ensureSchema(ctx context.Context, db *sql.DB) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS daily_runs (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			generated_at TEXT NOT NULL,
			trade_date TEXT NOT NULL,
			data_start TEXT NOT NULL,
			data_end TEXT NOT NULL,
			stock_pool TEXT NOT NULL,
			bars_rows INTEGER NOT NULL,
			initial_cash REAL NOT NULL,
			final_equity REAL NOT NULL,
			total_return_pct REAL NOT NULL,
			max_drawdown_pct REAL NOT NULL,
			trades INTEGER NOT NULL,
			wins INTEGER NOT NULL,
			losses INTEGER NOT NULL,
			win_rate_pct REAL NOT NULL,
			profit_factor REAL,
			max_consecutive_loss INTEGER NOT NULL,
			risk_level TEXT NOT NULL,
			conclusion TEXT NOT NULL,
			markdown_path TEXT NOT NULL,
			trades_path TEXT NOT NULL,
			equity_path TEXT NOT NULL,
			markdown_url TEXT NOT NULL,
			trades_url TEXT NOT NULL,
			equity_url TEXT NOT NULL
		);`,
		`CREATE INDEX IF NOT EXISTS idx_daily_runs_generated_at ON daily_runs(generated_at DESC);`,
		`CREATE TABLE IF NOT EXISTS daily_equity (
			run_id INTEGER NOT NULL,
			date TEXT NOT NULL,
			cash REAL NOT NULL,
			market_value REAL NOT NULL,
			equity REAL NOT NULL,
			daily_return_pct REAL NOT NULL,
			open_positions INTEGER NOT NULL,
			FOREIGN KEY(run_id) REFERENCES daily_runs(id) ON DELETE CASCADE
		);`,
		`CREATE INDEX IF NOT EXISTS idx_daily_equity_run_id ON daily_equity(run_id, date);`,
		`CREATE TABLE IF NOT EXISTS daily_trades (
			run_id INTEGER NOT NULL,
			buy_date TEXT NOT NULL,
			sell_date TEXT NOT NULL,
			code TEXT NOT NULL,
			shares INTEGER NOT NULL,
			buy_price REAL NOT NULL,
			sell_price REAL NOT NULL,
			holding_days INTEGER NOT NULL,
			exit_reason TEXT NOT NULL,
			buy_cost REAL NOT NULL,
			sell_proceeds REAL NOT NULL,
			net_profit REAL NOT NULL,
			net_return_pct REAL NOT NULL,
			score REAL NOT NULL,
			risk_level TEXT NOT NULL,
			reasons TEXT NOT NULL,
			FOREIGN KEY(run_id) REFERENCES daily_runs(id) ON DELETE CASCADE
		);`,
		`CREATE INDEX IF NOT EXISTS idx_daily_trades_run_id ON daily_trades(run_id, code);`,
		`CREATE TABLE IF NOT EXISTS daily_attributions (
			run_id INTEGER NOT NULL,
			type TEXT NOT NULL,
			key TEXT NOT NULL,
			trades INTEGER NOT NULL,
			wins INTEGER NOT NULL,
			win_rate_pct REAL NOT NULL,
			net_profit REAL NOT NULL,
			avg_return_pct REAL NOT NULL,
			max_profit REAL NOT NULL,
			max_loss REAL NOT NULL,
			avg_holding_days REAL NOT NULL,
			FOREIGN KEY(run_id) REFERENCES daily_runs(id) ON DELETE CASCADE
		);`,
		`CREATE INDEX IF NOT EXISTS idx_daily_attr_run_id ON daily_attributions(run_id, type);`,
	}
	for _, stmt := range stmts {
		if _, err := db.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("ensure sqlite schema: %w", err)
		}
	}
	return nil
}

func saveDailyRun(ctx context.Context, db *sql.DB, record DailyRunRecord) (int64, error) {
	if record.GeneratedAt.IsZero() {
		record.GeneratedAt = time.Now()
	}
	insights := report.BuildPaperInsights(record.Paper)
	if record.RiskLevel == "" {
		record.RiskLevel = insights.RiskLevel
	}
	if record.Conclusion == "" {
		record.Conclusion = insights.Conclusion
	}
	dataStart, dataEnd := "", ""
	if len(record.Paper.DailyEquity) > 0 {
		dataStart = record.Paper.DailyEquity[0].Date
		dataEnd = record.Paper.DailyEquity[len(record.Paper.DailyEquity)-1].Date
	}
	var profitFactor any
	if record.Paper.ProfitFactor != nil {
		profitFactor = *record.Paper.ProfitFactor
	}
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("begin sqlite tx: %w", err)
	}
	defer tx.Rollback()
	res, err := tx.ExecContext(ctx, `INSERT INTO daily_runs (generated_at, trade_date, data_start, data_end, stock_pool, bars_rows,
initial_cash, final_equity, total_return_pct, max_drawdown_pct, trades, wins, losses, win_rate_pct, profit_factor,
max_consecutive_loss, risk_level, conclusion, markdown_path, trades_path, equity_path, markdown_url, trades_url, equity_url)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		record.GeneratedAt.Format(time.RFC3339), record.TradeDate, dataStart, dataEnd, strings.Join(record.Bars.Codes, ", "), record.Bars.Rows,
		record.Paper.InitialCash, record.Paper.FinalEquity, record.Paper.TotalReturnPct, record.Paper.MaxDrawdownPct, record.Paper.Trades, record.Paper.Wins, record.Paper.Losses, record.Paper.WinRatePct, profitFactor,
		record.Paper.MaxConsecutiveLoss, record.RiskLevel, record.Conclusion, record.Report.MarkdownPath, record.Report.TradesCSV, record.Report.EquityCSV, record.ReportURLs.MarkdownURL, record.ReportURLs.TradesURL, record.ReportURLs.EquityURL)
	if err != nil {
		return 0, fmt.Errorf("insert daily run: %w", err)
	}
	runID, err := res.LastInsertId()
	if err != nil {
		return 0, fmt.Errorf("daily run id: %w", err)
	}
	for _, item := range record.Paper.DailyEquity {
		if _, err := tx.ExecContext(ctx, `INSERT INTO daily_equity (run_id, date, cash, market_value, equity, daily_return_pct, open_positions) VALUES (?, ?, ?, ?, ?, ?, ?)`, runID, item.Date, item.Cash, item.MarketValue, item.Equity, item.DailyReturnPct, item.OpenPositions); err != nil {
			return 0, fmt.Errorf("insert daily equity: %w", err)
		}
	}
	for _, trade := range record.Paper.TradesList {
		if _, err := tx.ExecContext(ctx, `INSERT INTO daily_trades (run_id, buy_date, sell_date, code, shares, buy_price, sell_price, holding_days, exit_reason, buy_cost, sell_proceeds, net_profit, net_return_pct, score, risk_level, reasons) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, runID, trade.BuyDate, trade.SellDate, trade.Code, trade.Shares, trade.BuyPrice, trade.SellPrice, trade.HoldingDays, trade.ExitReason, trade.BuyCost, trade.SellProceeds, trade.NetProfit, trade.NetReturnPct, trade.Score, trade.RiskLevel, strings.Join(trade.Reasons, ";")); err != nil {
			return 0, fmt.Errorf("insert daily trade: %w", err)
		}
	}
	for _, row := range report.BuildTradeAttribution(record.Paper.TradesList, func(t paper.Trade) string { return t.Code }) {
		if err := insertAttribution(ctx, tx, runID, "code", row); err != nil {
			return 0, err
		}
	}
	for _, row := range report.BuildTradeAttribution(record.Paper.TradesList, func(t paper.Trade) string { return t.ExitReason }) {
		if err := insertAttribution(ctx, tx, runID, "exit_reason", row); err != nil {
			return 0, err
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("commit sqlite tx: %w", err)
	}
	return runID, nil
}

func insertAttribution(ctx context.Context, tx *sql.Tx, runID int64, typ string, row report.AttributionRow) error {
	_, err := tx.ExecContext(ctx, `INSERT INTO daily_attributions (run_id, type, key, trades, wins, win_rate_pct, net_profit, avg_return_pct, max_profit, max_loss, avg_holding_days) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, runID, typ, row.Key, row.Trades, row.Wins, row.WinRatePct, row.NetProfit, row.AvgReturnPct, row.MaxProfit, row.MaxLoss, row.AvgHoldingDays)
	if err != nil {
		return fmt.Errorf("insert attribution: %w", err)
	}
	return nil
}

func queryEquity(ctx context.Context, db *sql.DB, runID int64) ([]paper.DailyEquity, error) {
	rows, err := db.QueryContext(ctx, `SELECT date, cash, market_value, equity, daily_return_pct, open_positions FROM daily_equity WHERE run_id = ? ORDER BY date`, runID)
	if err != nil {
		return nil, fmt.Errorf("query equity: %w", err)
	}
	defer rows.Close()
	out := make([]paper.DailyEquity, 0)
	for rows.Next() {
		var item paper.DailyEquity
		if err := rows.Scan(&item.Date, &item.Cash, &item.MarketValue, &item.Equity, &item.DailyReturnPct, &item.OpenPositions); err != nil {
			return nil, fmt.Errorf("scan equity: %w", err)
		}
		out = append(out, item)
	}
	return out, rows.Err()
}

func queryTrades(ctx context.Context, db *sql.DB, runID int64) ([]paper.Trade, error) {
	rows, err := db.QueryContext(ctx, `SELECT buy_date, sell_date, code, shares, buy_price, sell_price, holding_days, exit_reason, buy_cost, sell_proceeds, net_profit, net_return_pct, score, risk_level, reasons FROM daily_trades WHERE run_id = ? ORDER BY sell_date, rowid`, runID)
	if err != nil {
		return nil, fmt.Errorf("query trades: %w", err)
	}
	defer rows.Close()
	out := make([]paper.Trade, 0)
	for rows.Next() {
		var trade paper.Trade
		var reasons string
		if err := rows.Scan(&trade.BuyDate, &trade.SellDate, &trade.Code, &trade.Shares, &trade.BuyPrice, &trade.SellPrice, &trade.HoldingDays, &trade.ExitReason, &trade.BuyCost, &trade.SellProceeds, &trade.NetProfit, &trade.NetReturnPct, &trade.Score, &trade.RiskLevel, &reasons); err != nil {
			return nil, fmt.Errorf("scan trade: %w", err)
		}
		if reasons != "" {
			trade.Reasons = strings.Split(reasons, ";")
		}
		out = append(out, trade)
	}
	return out, rows.Err()
}

func queryAttribution(ctx context.Context, db *sql.DB, runID int64, typ string) ([]report.AttributionRow, error) {
	rows, err := db.QueryContext(ctx, `SELECT key, trades, wins, win_rate_pct, net_profit, avg_return_pct, max_profit, max_loss, avg_holding_days FROM daily_attributions WHERE run_id = ? AND type = ? ORDER BY net_profit ASC`, runID, typ)
	if err != nil {
		return nil, fmt.Errorf("query attribution: %w", err)
	}
	defer rows.Close()
	out := make([]report.AttributionRow, 0)
	for rows.Next() {
		var row report.AttributionRow
		if err := rows.Scan(&row.Key, &row.Trades, &row.Wins, &row.WinRatePct, &row.NetProfit, &row.AvgReturnPct, &row.MaxProfit, &row.MaxLoss, &row.AvgHoldingDays); err != nil {
			return nil, fmt.Errorf("scan attribution: %w", err)
		}
		out = append(out, row)
	}
	return out, rows.Err()
}
