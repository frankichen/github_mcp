package appconfig

import (
	"encoding/json"
	"fmt"
	"os"
)

type Config struct {
	Data      DataConfig      `json:"data"`
	Provider  ProviderConfig  `json:"provider"`
	Strategy  StrategyConfig  `json:"strategy"`
	Report    ReportConfig    `json:"report"`
	Database  DatabaseConfig  `json:"database"`
	Dashboard DashboardConfig `json:"dashboard"`
	Runtime   RuntimeConfig   `json:"runtime"`
	Notify    NotifyConfig    `json:"notify"`
}

type DataConfig struct {
	Source            string   `json:"source"`
	Universe          string   `json:"universe"`
	Codes             []string `json:"codes"`
	CodesFile         string   `json:"codes_file"`
	StartDate         string   `json:"start_date"`
	EndDate           string   `json:"end_date"`
	BarsFile          string   `json:"bars_file"`
	UniverseLimit     int      `json:"universe_limit"`
	MinUniversePrice  float64  `json:"min_universe_price"`
	MaxUniversePrice  float64  `json:"max_universe_price"`
	MinUniverseAmount float64  `json:"min_universe_amount"`
}

type ProviderConfig struct {
	PythonPath                    string `json:"python_path"`
	AkshareScript                 string `json:"akshare_script"`
	AkshareCache                  string `json:"akshare_cache"`
	AkshareLookbackDays           int    `json:"akshare_lookback_days"`
	AkshareRequestDelayMS         int    `json:"akshare_request_delay_ms"`
	AkshareMaxConsecutiveFailures int    `json:"akshare_max_consecutive_failures"`
	AkshareFallbackToBarsFile     bool   `json:"akshare_fallback_to_bars_file"`
	RefreshCache                  bool   `json:"refresh_cache"`
	TushareToken                  string `json:"tushare_token"`
}

type StrategyConfig struct {
	Cash                      float64 `json:"cash"`
	TopN                      int     `json:"top"`
	MinScore                  float64 `json:"min_score"`
	MaxPositionRatio          float64 `json:"max_position_ratio"`
	AllowMultipleBuys         bool    `json:"allow_multiple_buys"`
	StrategyProfile           string  `json:"strategy_profile"`
	StrictEntry               bool    `json:"strict_entry"`
	PaperSellPrice            string  `json:"paper_sell_price"`
	MinTradeAmount            float64 `json:"min_trade_amount"`
	MinHoldDays               int     `json:"min_hold_days"`
	MaxHoldDays               int     `json:"max_hold_days"`
	StopLossPct               float64 `json:"stop_loss_pct"`
	TakeProfitPct             float64 `json:"take_profit_pct"`
	CooldownDays              int     `json:"cooldown_days"`
	StopLossCooldownDays      int     `json:"stop_loss_cooldown_days"`
	PoorPerformerFilter       *bool   `json:"poor_performer_filter"`
	RepeatedStopLossFilter    *bool   `json:"repeated_stop_loss_filter"`
	PoorPerformerMinTrades    int     `json:"poor_performer_min_trades"`
	PoorPerformerMaxNetProfit float64 `json:"poor_performer_max_net_profit"`
	SingleLossFilter          *bool   `json:"single_loss_filter"`
	SingleLossMaxNetProfit    float64 `json:"single_loss_max_net_profit"`
	SingleLossMaxReturnPct    float64 `json:"single_loss_max_return_pct"`
	LargeLossFilter           *bool   `json:"large_loss_filter"`
	LargeLossMaxReturnPct     float64 `json:"large_loss_max_return_pct"`
	LargeLossCooldownDays     int     `json:"large_loss_cooldown_days"`
	LossStreakPause           *bool   `json:"loss_streak_pause"`
	LossStreakThreshold       int     `json:"loss_streak_threshold"`
	LossStreakPauseDays       int     `json:"loss_streak_pause_days"`
}

type ReportConfig struct {
	ReportDir     string `json:"report_dir"`
	ReportPrefix  string `json:"report_prefix"`
	TradeDate     string `json:"trade_date"`
	PublicBaseURL string `json:"public_base_url"`
	CacheBust     *bool  `json:"cache_bust,omitempty"`
	LatestFile    string `json:"latest_file"`
}

type DatabaseConfig struct {
	Enabled bool   `json:"enabled"`
	Path    string `json:"path"`
}

type DashboardConfig struct {
	Address  string `json:"address"`
	Username string `json:"username"`
	Password string `json:"password"`
}

type RuntimeConfig struct {
	LockFile string `json:"lock_file"`
}

type NotifyConfig struct {
	DingTalk DingTalkConfig `json:"dingtalk"`
	Webhook  WebhookConfig  `json:"webhook"`
}

type DingTalkConfig struct {
	Enabled bool   `json:"enabled"`
	Webhook string `json:"webhook"`
	Secret  string `json:"secret"`
}

type WebhookConfig struct {
	Enabled bool   `json:"enabled"`
	URL     string `json:"url"`
	Secret  string `json:"secret"`
}

func Load(path string) (Config, error) {
	if path == "" {
		return Config{}, nil
	}
	content, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("read config: %w", err)
	}
	var cfg Config
	if err := json.Unmarshal(content, &cfg); err != nil {
		return Config{}, fmt.Errorf("parse config json: %w", err)
	}
	return cfg, nil
}

func JoinCodes(codes []string) string {
	if len(codes) == 0 {
		return ""
	}
	out := codes[0]
	for _, code := range codes[1:] {
		out += "," + code
	}
	return out
}
