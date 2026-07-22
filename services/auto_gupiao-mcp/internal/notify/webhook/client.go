package webhook

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

const (
	HeaderTimestamp      = "X-Auto-Gupiao-Timestamp"
	HeaderSignature      = "X-Auto-Gupiao-Signature"
	HeaderEvent          = "X-Auto-Gupiao-Event"
	HeaderIdempotencyKey = "X-Auto-Gupiao-Idempotency-Key"
)

type Client struct {
	URL    string
	Secret string
	HTTP   *http.Client
	Now    func() time.Time
}

type Payload struct {
	Source         string         `json:"source"`
	Event          string         `json:"event"`
	IdempotencyKey string         `json:"idempotency_key"`
	RunID          int64          `json:"run_id"`
	TradeDate      string         `json:"trade_date"`
	GeneratedAt    string         `json:"generated_at"`
	Bars           BarsPayload    `json:"bars"`
	Summary        SummaryPayload `json:"summary"`
	Reports        ReportsPayload `json:"reports"`
}

type BarsPayload struct {
	StartDate string   `json:"start_date"`
	EndDate   string   `json:"end_date"`
	Codes     []string `json:"codes"`
	Rows      int      `json:"rows"`
}

type SummaryPayload struct {
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
}

type ReportsPayload struct {
	MarkdownURL string `json:"markdown_url"`
	TradesURL   string `json:"trades_url"`
	EquityURL   string `json:"equity_url"`
}

type Response struct {
	StatusCode int    `json:"status_code"`
	Body       string `json:"body"`
}

func NewClient(url string, secret string) *Client {
	return &Client{URL: url, Secret: secret, HTTP: &http.Client{Timeout: 10 * time.Second}, Now: time.Now}
}

func (c *Client) Send(ctx context.Context, payload Payload) (Response, error) {
	if strings.TrimSpace(c.URL) == "" {
		return Response{}, fmt.Errorf("webhook url is empty")
	}
	if payload.Event == "" {
		return Response{}, fmt.Errorf("webhook event is empty")
	}
	if payload.IdempotencyKey == "" {
		return Response{}, fmt.Errorf("webhook idempotency key is empty")
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return Response{}, fmt.Errorf("marshal webhook payload: %w", err)
	}
	now := time.Now
	if c.Now != nil {
		now = c.Now
	}
	timestamp := fmt.Sprintf("%d", now().Unix())
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.URL, bytes.NewReader(body))
	if err != nil {
		return Response{}, fmt.Errorf("create webhook request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	req.Header.Set(HeaderTimestamp, timestamp)
	req.Header.Set(HeaderEvent, payload.Event)
	req.Header.Set(HeaderIdempotencyKey, payload.IdempotencyKey)
	if strings.TrimSpace(c.Secret) != "" {
		req.Header.Set(HeaderSignature, Sign(c.Secret, timestamp, body))
	}
	httpClient := c.HTTP
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 10 * time.Second}
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return Response{}, fmt.Errorf("send webhook request: %w", err)
	}
	defer resp.Body.Close()
	content, err := io.ReadAll(resp.Body)
	if err != nil {
		return Response{}, fmt.Errorf("read webhook response: %w", err)
	}
	result := Response{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(content))}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return result, fmt.Errorf("webhook http status %d: %s", resp.StatusCode, result.Body)
	}
	return result, nil
}

func Sign(secret string, timestamp string, body []byte) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(timestamp))
	mac.Write([]byte("\n"))
	mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil))
}
