package serverrunner

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/appconfig"
)

type LatestReport struct {
	RunID              int64    `json:"run_id"`
	TradeDate          string   `json:"trade_date"`
	GeneratedAt        string   `json:"generated_at"`
	DataStatus         string   `json:"data_status"`
	DataStatusReason   string   `json:"data_status_reason,omitempty"`
	RiskLevel          string   `json:"risk_level"`
	Conclusion         string   `json:"conclusion"`
	InitialCash        float64  `json:"initial_cash"`
	FinalEquity        float64  `json:"final_equity"`
	TotalReturnPct     float64  `json:"total_return_pct"`
	MaxDrawdownPct     float64  `json:"max_drawdown_pct"`
	Trades             int      `json:"trades"`
	WinRatePct         float64  `json:"win_rate_pct"`
	ProfitFactor       *float64 `json:"profit_factor"`
	MaxConsecutiveLoss int      `json:"max_consecutive_loss"`
	BarsStartDate      string   `json:"bars_start_date"`
	BarsEndDate        string   `json:"bars_end_date"`
	Codes              []string `json:"codes"`
	Rows               int      `json:"rows"`
	MarkdownURL        string   `json:"markdown_url"`
	TradesURL          string   `json:"trades_url"`
	EquityURL          string   `json:"equity_url"`
}

func writeLatestJSON(cfg appconfig.Config, result Result, generatedAt time.Time) error {
	path := strings.TrimSpace(cfg.Report.LatestFile)
	if path == "" {
		path = filepath.Join(cfg.Report.ReportDir, "latest.json")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create latest dir: %w", err)
	}
	payload := BuildWebhookPayload(result, cfg, generatedAt)
	latest := LatestReport{
		RunID:              payload.RunID,
		TradeDate:          payload.TradeDate,
		GeneratedAt:        payload.GeneratedAt,
		DataStatus:         result.DataStatus,
		DataStatusReason:   result.DataStatusReason,
		RiskLevel:          payload.Summary.RiskLevel,
		Conclusion:         payload.Summary.Conclusion,
		InitialCash:        payload.Summary.InitialCash,
		FinalEquity:        payload.Summary.FinalEquity,
		TotalReturnPct:     payload.Summary.TotalReturnPct,
		MaxDrawdownPct:     payload.Summary.MaxDrawdownPct,
		Trades:             payload.Summary.Trades,
		WinRatePct:         payload.Summary.WinRatePct,
		ProfitFactor:       payload.Summary.ProfitFactor,
		MaxConsecutiveLoss: payload.Summary.MaxConsecutiveLoss,
		BarsStartDate:      payload.Bars.StartDate,
		BarsEndDate:        payload.Bars.EndDate,
		Codes:              payload.Bars.Codes,
		Rows:               payload.Bars.Rows,
		MarkdownURL:        payload.Reports.MarkdownURL,
		TradesURL:          payload.Reports.TradesURL,
		EquityURL:          payload.Reports.EquityURL,
	}
	content, err := json.MarshalIndent(latest, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal latest json: %w", err)
	}
	content = append(content, '\n')
	if err := os.WriteFile(path, content, 0o644); err != nil {
		return fmt.Errorf("write latest json: %w", err)
	}
	return nil
}
