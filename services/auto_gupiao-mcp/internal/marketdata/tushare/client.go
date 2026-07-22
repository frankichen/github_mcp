package tushare

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/domain"
	"github.com/frankichen/auto_gupiao/internal/indicator"
)

const (
	DefaultBaseURL                       = "https://api.tushare.pro"
	DefaultIndicatorLookbackCalendarDays = 140
)

type Client struct {
	Token                         string
	BaseURL                       string
	HTTPClient                    *http.Client
	IndicatorLookbackCalendarDays int
}

type Option func(*Client)

func WithBaseURL(baseURL string) Option {
	return func(c *Client) {
		if baseURL != "" {
			c.BaseURL = strings.TrimRight(baseURL, "/")
		}
	}
}

func WithHTTPClient(httpClient *http.Client) Option {
	return func(c *Client) {
		if httpClient != nil {
			c.HTTPClient = httpClient
		}
	}
}

func WithIndicatorLookbackCalendarDays(days int) Option {
	return func(c *Client) {
		if days > 0 {
			c.IndicatorLookbackCalendarDays = days
		}
	}
}

func NewClient(token string, opts ...Option) *Client {
	client := &Client{
		Token:   token,
		BaseURL: DefaultBaseURL,
		HTTPClient: &http.Client{
			Timeout: 15 * time.Second,
		},
		IndicatorLookbackCalendarDays: DefaultIndicatorLookbackCalendarDays,
	}
	for _, opt := range opts {
		opt(client)
	}
	return client
}

type requestBody struct {
	APIName string         `json:"api_name"`
	Token   string         `json:"token"`
	Params  map[string]any `json:"params"`
	Fields  string         `json:"fields"`
}

type responseBody struct {
	Code int    `json:"code"`
	Msg  string `json:"msg"`
	Data struct {
		Fields []string `json:"fields"`
		Items  [][]any  `json:"items"`
	} `json:"data"`
}

func (c *Client) ListStocks(ctx context.Context) ([]domain.StockBasic, error) {
	fields := []string{"ts_code", "symbol", "name", "area", "industry", "market", "list_date", "list_status", "is_hs"}
	rows, err := c.call(ctx, "stock_basic", map[string]any{"list_status": "L"}, fields)
	if err != nil {
		return nil, err
	}
	stocks := make([]domain.StockBasic, 0, len(rows))
	for _, row := range rows {
		stocks = append(stocks, domain.StockBasic{
			Code:       row.string("ts_code"),
			Symbol:     row.string("symbol"),
			Name:       row.string("name"),
			Area:       row.string("area"),
			Industry:   row.string("industry"),
			Market:     row.string("market"),
			ListDate:   row.string("list_date"),
			ListStatus: row.string("list_status"),
			IsHS:       row.string("is_hs"),
		})
	}
	return stocks, nil
}

func (c *Client) DailyBars(ctx context.Context, code string, startDate string, endDate string) ([]domain.DailyBar, error) {
	fields := []string{"ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"}
	params := map[string]any{}
	if code != "" {
		params["ts_code"] = code
	}
	if startDate != "" {
		params["start_date"] = NormalizeDate(startDate)
	}
	if endDate != "" {
		params["end_date"] = NormalizeDate(endDate)
	}
	rows, err := c.call(ctx, "daily", params, fields)
	if err != nil {
		return nil, err
	}
	bars := make([]domain.DailyBar, 0, len(rows))
	for _, row := range rows {
		bars = append(bars, domain.DailyBar{
			Code:      row.string("ts_code"),
			Date:      row.string("trade_date"),
			Open:      row.float("open"),
			High:      row.float("high"),
			Low:       row.float("low"),
			Close:     row.float("close"),
			PrevClose: row.float("pre_close"),
			Change:    row.float("change"),
			ChangePct: row.float("pct_chg"),
			Volume:    row.float("vol"),
			Amount:    row.float("amount"),
		})
	}
	return bars, nil
}

func (c *Client) DailyBasics(ctx context.Context, tradeDate string) ([]domain.Fundamental, error) {
	fields := []string{"ts_code", "trade_date", "turnover_rate", "volume_ratio", "pe", "pb", "total_mv"}
	params := map[string]any{}
	if tradeDate != "" {
		params["trade_date"] = NormalizeDate(tradeDate)
	}
	rows, err := c.call(ctx, "daily_basic", params, fields)
	if err != nil {
		return nil, err
	}
	items := make([]domain.Fundamental, 0, len(rows))
	for _, row := range rows {
		items = append(items, domain.Fundamental{
			Code:         row.string("ts_code"),
			Date:         row.string("trade_date"),
			TurnoverRate: row.float("turnover_rate"),
			VolumeRatio:  row.float("volume_ratio"),
			PE:           row.float("pe"),
			PB:           row.float("pb"),
			MarketCap:    row.float("total_mv"),
		})
	}
	return items, nil
}

func (c *Client) TradeCalendar(ctx context.Context, exchange string, startDate string, endDate string) ([]domain.TradingDay, error) {
	fields := []string{"exchange", "cal_date", "is_open", "pretrade_date"}
	params := map[string]any{}
	if exchange != "" {
		params["exchange"] = strings.ToUpper(strings.TrimSpace(exchange))
	}
	if startDate != "" {
		params["start_date"] = NormalizeDate(startDate)
	}
	if endDate != "" {
		params["end_date"] = NormalizeDate(endDate)
	}
	rows, err := c.call(ctx, "trade_cal", params, fields)
	if err != nil {
		return nil, err
	}
	days := make([]domain.TradingDay, 0, len(rows))
	for _, row := range rows {
		days = append(days, domain.TradingDay{
			Exchange:     row.string("exchange"),
			Date:         row.string("cal_date"),
			IsOpen:       row.string("is_open") == "1",
			PreTradeDate: row.string("pretrade_date"),
		})
	}
	return days, nil
}

func (c *Client) ListSnapshots(ctx context.Context, tradeDate string) ([]domain.StockSnapshot, error) {
	day := NormalizeDate(tradeDate)
	stocks, err := c.ListStocks(ctx)
	if err != nil {
		return nil, fmt.Errorf("list stock basics: %w", err)
	}
	nameByCode := make(map[string]domain.StockBasic, len(stocks))
	for _, stock := range stocks {
		nameByCode[stock.Code] = stock
	}

	historyStart := LookbackStartDate(day, c.IndicatorLookbackCalendarDays)
	bars, err := c.DailyBars(ctx, "", historyStart, day)
	if err != nil {
		return nil, fmt.Errorf("list daily bars: %w", err)
	}
	currentBars := make([]domain.DailyBar, 0)
	for _, bar := range bars {
		if NormalizeDate(bar.Date) == day {
			currentBars = append(currentBars, bar)
		}
	}
	fundamentals, err := c.DailyBasics(ctx, day)
	if err != nil {
		return nil, fmt.Errorf("list daily basics: %w", err)
	}
	fundamentalByCode := make(map[string]domain.Fundamental, len(fundamentals))
	for _, fundamental := range fundamentals {
		fundamentalByCode[fundamental.Code] = fundamental
	}

	snapshots := make([]domain.StockSnapshot, 0, len(currentBars))
	for _, bar := range currentBars {
		stock := nameByCode[bar.Code]
		fundamental := fundamentalByCode[bar.Code]
		snapshots = append(snapshots, domain.StockSnapshot{
			Date:         bar.Date,
			Code:         bar.Code,
			Name:         stock.Name,
			Market:       stock.Market,
			Close:        bar.Close,
			PrevClose:    bar.PrevClose,
			High:         bar.High,
			Low:          bar.Low,
			ChangePct:    bar.ChangePct,
			TurnoverRate: fundamental.TurnoverRate,
			VolumeRatio:  fundamental.VolumeRatio,
			Amount:       bar.Amount,
			MarketCap:    fundamental.MarketCap,
			PE:           fundamental.PE,
			PB:           fundamental.PB,
		})
	}
	return indicator.EnrichSnapshots(snapshots, bars), nil
}

func (c *Client) call(ctx context.Context, apiName string, params map[string]any, fields []string) ([]row, error) {
	if c.Token == "" {
		return nil, errors.New("missing tushare token")
	}
	body := requestBody{
		APIName: apiName,
		Token:   c.Token,
		Params:  params,
		Fields:  strings.Join(fields, ","),
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL, bytes.NewReader(payload))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("post %s: %w", apiName, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return nil, fmt.Errorf("%s status %d: %s", apiName, resp.StatusCode, strings.TrimSpace(string(data)))
	}
	var decoded responseBody
	if err := json.NewDecoder(resp.Body).Decode(&decoded); err != nil {
		return nil, fmt.Errorf("decode %s response: %w", apiName, err)
	}
	if decoded.Code != 0 {
		return nil, fmt.Errorf("%s error %d: %s", apiName, decoded.Code, decoded.Msg)
	}
	rows := make([]row, 0, len(decoded.Data.Items))
	for _, item := range decoded.Data.Items {
		mapped := make(row, len(decoded.Data.Fields))
		for i, field := range decoded.Data.Fields {
			if i < len(item) {
				mapped[field] = item[i]
			}
		}
		rows = append(rows, mapped)
	}
	return rows, nil
}

func NormalizeDate(date string) string {
	return strings.ReplaceAll(strings.TrimSpace(date), "-", "")
}

func LookbackStartDate(endDate string, calendarDays int) string {
	if calendarDays <= 0 {
		calendarDays = DefaultIndicatorLookbackCalendarDays
	}
	parsed, err := time.Parse("20060102", NormalizeDate(endDate))
	if err != nil {
		return NormalizeDate(endDate)
	}
	return parsed.AddDate(0, 0, -calendarDays).Format("20060102")
}

type row map[string]any

func (r row) string(key string) string {
	value, ok := r[key]
	if !ok || value == nil {
		return ""
	}
	switch v := value.(type) {
	case string:
		return v
	case float64:
		if v == float64(int64(v)) {
			return strconv.FormatInt(int64(v), 10)
		}
		return strconv.FormatFloat(v, 'f', -1, 64)
	default:
		return fmt.Sprint(v)
	}
}

func (r row) float(key string) float64 {
	value, ok := r[key]
	if !ok || value == nil {
		return 0
	}
	switch v := value.(type) {
	case float64:
		return v
	case string:
		parsed, _ := strconv.ParseFloat(strings.TrimSpace(v), 64)
		return parsed
	default:
		parsed, _ := strconv.ParseFloat(fmt.Sprint(v), 64)
		return parsed
	}
}
