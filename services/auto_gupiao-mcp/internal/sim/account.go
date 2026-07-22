package sim

import (
	"errors"
	"fmt"
	"math"
	"sort"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

type Side string

const (
	SideBuy  Side = "buy"
	SideSell Side = "sell"
)

type Order struct {
	Date   string  `json:"date"`
	Code   string  `json:"code"`
	Name   string  `json:"name"`
	Side   Side    `json:"side"`
	Price  float64 `json:"price"`
	Shares int     `json:"shares"`
}

type Fill struct {
	Date           string  `json:"date"`
	Code           string  `json:"code"`
	Name           string  `json:"name"`
	Side           Side    `json:"side"`
	Price          float64 `json:"price"`
	Shares         int     `json:"shares"`
	GrossAmount    float64 `json:"gross_amount"`
	Fees           float64 `json:"fees"`
	CashAmount     float64 `json:"cash_amount"`
	RealizedProfit float64 `json:"realized_profit"`
}

type Position struct {
	Code      string  `json:"code"`
	Name      string  `json:"name"`
	Shares    int     `json:"shares"`
	AvgCost   float64 `json:"avg_cost"`
	CostBasis float64 `json:"cost_basis"`
}

type PositionReport struct {
	Code                string  `json:"code"`
	Name                string  `json:"name"`
	Shares              int     `json:"shares"`
	AvgCost             float64 `json:"avg_cost"`
	CostBasis           float64 `json:"cost_basis"`
	LastPrice           float64 `json:"last_price"`
	MarketValue         float64 `json:"market_value"`
	UnrealizedProfit    float64 `json:"unrealized_profit"`
	UnrealizedReturnPct float64 `json:"unrealized_return_pct"`
}

type Report struct {
	InitialCash      float64          `json:"initial_cash"`
	Cash             float64          `json:"cash"`
	Equity           float64          `json:"equity"`
	RealizedProfit   float64          `json:"realized_profit"`
	UnrealizedProfit float64          `json:"unrealized_profit"`
	TotalReturnPct   float64          `json:"total_return_pct"`
	Positions        []PositionReport `json:"positions"`
	Fills            []Fill           `json:"fills"`
}

type Account struct {
	InitialCash    float64
	Cash           float64
	Cost           domain.CostModel
	Positions      map[string]Position
	Fills          []Fill
	RealizedProfit float64
}

func NewAccount(initialCash float64, cost domain.CostModel) *Account {
	if initialCash <= 0 {
		initialCash = 10000
	}
	if cost == (domain.CostModel{}) {
		cost = domain.DefaultCostModel()
	}
	return &Account{
		InitialCash: initialCash,
		Cash:        initialCash,
		Cost:        cost,
		Positions:   make(map[string]Position),
	}
}

func (a *Account) BuyCandidate(candidate domain.Candidate) (Fill, error) {
	return a.Buy(Order{
		Date:   candidate.Snapshot.Date,
		Code:   candidate.Snapshot.Code,
		Name:   candidate.Snapshot.Name,
		Side:   SideBuy,
		Price:  candidate.Snapshot.Close,
		Shares: candidate.SuggestedShares,
	})
}

func (a *Account) Buy(order Order) (Fill, error) {
	if err := validateOrder(order); err != nil {
		return Fill{}, err
	}
	gross := float64(order.Shares) * order.Price
	totalCost := a.Cost.EstimateBuy(gross)
	if totalCost > a.Cash {
		return Fill{}, fmt.Errorf("insufficient cash: need %.2f, have %.2f", totalCost, a.Cash)
	}
	pos := a.Positions[order.Code]
	newShares := pos.Shares + order.Shares
	newCostBasis := pos.CostBasis + totalCost
	pos.Code = order.Code
	pos.Name = order.Name
	pos.Shares = newShares
	pos.CostBasis = newCostBasis
	pos.AvgCost = newCostBasis / float64(newShares)
	a.Positions[order.Code] = pos
	a.Cash -= totalCost
	fill := Fill{
		Date:        order.Date,
		Code:        order.Code,
		Name:        order.Name,
		Side:        SideBuy,
		Price:       round(order.Price, 4),
		Shares:      order.Shares,
		GrossAmount: round(gross, 2),
		Fees:        round(totalCost-gross, 2),
		CashAmount:  round(-totalCost, 2),
	}
	a.Fills = append(a.Fills, fill)
	return fill, nil
}

func (a *Account) Sell(order Order) (Fill, error) {
	if err := validateOrder(order); err != nil {
		return Fill{}, err
	}
	pos, ok := a.Positions[order.Code]
	if !ok || pos.Shares <= 0 {
		return Fill{}, fmt.Errorf("no position for %s", order.Code)
	}
	if order.Shares > pos.Shares {
		return Fill{}, fmt.Errorf("sell shares exceed position: sell %d, hold %d", order.Shares, pos.Shares)
	}
	gross := float64(order.Shares) * order.Price
	fees := a.Cost.EstimateSell(gross)
	proceeds := gross - fees
	closedCostBasis := pos.AvgCost * float64(order.Shares)
	realized := proceeds - closedCostBasis
	a.Cash += proceeds
	a.RealizedProfit += realized
	pos.Shares -= order.Shares
	pos.CostBasis -= closedCostBasis
	if pos.Shares == 0 {
		delete(a.Positions, order.Code)
	} else {
		pos.AvgCost = pos.CostBasis / float64(pos.Shares)
		a.Positions[order.Code] = pos
	}
	fill := Fill{
		Date:           order.Date,
		Code:           order.Code,
		Name:           firstNonEmpty(order.Name, pos.Name),
		Side:           SideSell,
		Price:          round(order.Price, 4),
		Shares:         order.Shares,
		GrossAmount:    round(gross, 2),
		Fees:           round(fees, 2),
		CashAmount:     round(proceeds, 2),
		RealizedProfit: round(realized, 2),
	}
	a.Fills = append(a.Fills, fill)
	return fill, nil
}

func (a *Account) SellAll(snapshots []domain.StockSnapshot) []error {
	priceByCode := make(map[string]domain.StockSnapshot, len(snapshots))
	for _, snapshot := range snapshots {
		if snapshot.Code != "" && snapshot.Close > 0 {
			priceByCode[snapshot.Code] = snapshot
		}
	}
	codes := make([]string, 0, len(a.Positions))
	for code := range a.Positions {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	var errs []error
	for _, code := range codes {
		pos := a.Positions[code]
		snapshot, ok := priceByCode[code]
		if !ok {
			errs = append(errs, fmt.Errorf("missing sell price for %s", code))
			continue
		}
		_, err := a.Sell(Order{
			Date:   snapshot.Date,
			Code:   code,
			Name:   firstNonEmpty(snapshot.Name, pos.Name),
			Side:   SideSell,
			Price:  snapshot.Close,
			Shares: pos.Shares,
		})
		if err != nil {
			errs = append(errs, err)
		}
	}
	return errs
}

func (a *Account) Report(snapshots []domain.StockSnapshot) Report {
	priceByCode := make(map[string]domain.StockSnapshot, len(snapshots))
	for _, snapshot := range snapshots {
		if snapshot.Code != "" && snapshot.Close > 0 {
			priceByCode[snapshot.Code] = snapshot
		}
	}
	positions := make([]PositionReport, 0, len(a.Positions))
	unrealized := 0.0
	marketValue := 0.0
	codes := make([]string, 0, len(a.Positions))
	for code := range a.Positions {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	for _, code := range codes {
		pos := a.Positions[code]
		lastPrice := pos.AvgCost
		if snapshot, ok := priceByCode[code]; ok {
			lastPrice = snapshot.Close
		}
		value := float64(pos.Shares) * lastPrice
		profit := value - pos.CostBasis
		returnPct := 0.0
		if pos.CostBasis > 0 {
			returnPct = profit / pos.CostBasis * 100
		}
		marketValue += value
		unrealized += profit
		positions = append(positions, PositionReport{
			Code:                pos.Code,
			Name:                pos.Name,
			Shares:              pos.Shares,
			AvgCost:             round(pos.AvgCost, 4),
			CostBasis:           round(pos.CostBasis, 2),
			LastPrice:           round(lastPrice, 4),
			MarketValue:         round(value, 2),
			UnrealizedProfit:    round(profit, 2),
			UnrealizedReturnPct: round(returnPct, 4),
		})
	}
	equity := a.Cash + marketValue
	return Report{
		InitialCash:      round(a.InitialCash, 2),
		Cash:             round(a.Cash, 2),
		Equity:           round(equity, 2),
		RealizedProfit:   round(a.RealizedProfit, 2),
		UnrealizedProfit: round(unrealized, 2),
		TotalReturnPct:   round((equity-a.InitialCash)/a.InitialCash*100, 4),
		Positions:        positions,
		Fills:            a.Fills,
	}
}

func validateOrder(order Order) error {
	if order.Code == "" {
		return errors.New("empty order code")
	}
	if order.Price <= 0 {
		return errors.New("order price must be positive")
	}
	if order.Shares <= 0 {
		return errors.New("order shares must be positive")
	}
	if order.Shares%domain.LotSize != 0 {
		return fmt.Errorf("order shares must be lot size multiple: %d", domain.LotSize)
	}
	return nil
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

func round(v float64, places int) float64 {
	if math.IsInf(v, 0) || math.IsNaN(v) {
		return v
	}
	p := math.Pow10(places)
	return math.Round(v*p) / p
}
