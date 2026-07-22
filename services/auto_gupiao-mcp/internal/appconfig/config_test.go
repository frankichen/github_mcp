package appconfig

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadConfig(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "server.json")
	content := `{
  "data": {
    "source": "akshare",
    "codes": ["000001", "600000"],
    "start_date": "20250101",
    "end_date": "20250518",
    "bars_file": "data/bars/batch.csv"
  },
  "provider": {
    "python_path": "python",
    "akshare_cache": "data/cache/akshare",
    "akshare_lookback_days": 120
  },
  "strategy": {
    "cash": 10000,
    "top": 3,
    "min_score": 45,
    "max_position_ratio": 0.3,
    "allow_multiple_buys": true,
    "paper_sell_price": "open"
  },
  "report": {
    "report_dir": "reports",
    "report_prefix": "daily_test",
    "trade_date": "20260519",
    "public_base_url": "https://example.com/reports"
  },
  "notify": {
    "dingtalk": {
      "enabled": false,
      "webhook": "",
      "secret": ""
    }
  }
}`
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	if cfg.Data.Source != "akshare" || len(cfg.Data.Codes) != 2 || cfg.Strategy.TopN != 3 || !cfg.Strategy.AllowMultipleBuys {
		t.Fatalf("unexpected config: %+v", cfg)
	}
	if cfg.Report.PublicBaseURL != "https://example.com/reports" {
		t.Fatalf("unexpected report public base url: %s", cfg.Report.PublicBaseURL)
	}
	if JoinCodes(cfg.Data.Codes) != "000001,600000" {
		t.Fatalf("unexpected joined codes: %s", JoinCodes(cfg.Data.Codes))
	}
}

func TestLoadEmptyPath(t *testing.T) {
	cfg, err := Load("")
	if err != nil {
		t.Fatalf("Load empty failed: %v", err)
	}
	if cfg.Data.Source != "" || len(cfg.Data.Codes) != 0 || cfg.Strategy.Cash != 0 || cfg.Notify.DingTalk.Enabled {
		t.Fatalf("unexpected config: %+v", cfg)
	}
}
