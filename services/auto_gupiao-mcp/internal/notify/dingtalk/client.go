package dingtalk

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Config struct {
	Enabled bool
	Webhook string
	Secret  string
}

type Client struct {
	Webhook string
	Secret  string
	HTTP    *http.Client
	Now     func() time.Time
}

type MarkdownMessage struct {
	Title string
	Text  string
}

type responseBody struct {
	ErrCode int    `json:"errcode"`
	ErrMsg  string `json:"errmsg"`
}

func NewClient(webhook string, secret string) *Client {
	return &Client{Webhook: webhook, Secret: secret, HTTP: &http.Client{Timeout: 10 * time.Second}, Now: time.Now}
}

func (c *Client) SendMarkdown(ctx context.Context, msg MarkdownMessage) error {
	if strings.TrimSpace(c.Webhook) == "" {
		return fmt.Errorf("dingtalk webhook is empty")
	}
	if strings.TrimSpace(msg.Title) == "" {
		msg.Title = "A股观察盘日报"
	}
	payload := map[string]any{
		"msgtype": "markdown",
		"markdown": map[string]string{
			"title": msg.Title,
			"text":  msg.Text,
		},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal dingtalk markdown: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.signedWebhook(), bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create dingtalk request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	httpClient := c.HTTP
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 10 * time.Second}
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("send dingtalk request: %w", err)
	}
	defer resp.Body.Close()
	content, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("read dingtalk response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("dingtalk http status %d: %s", resp.StatusCode, strings.TrimSpace(string(content)))
	}
	var result responseBody
	if err := json.Unmarshal(content, &result); err != nil {
		return fmt.Errorf("parse dingtalk response: %w", err)
	}
	if result.ErrCode != 0 {
		return fmt.Errorf("dingtalk api error %d: %s", result.ErrCode, result.ErrMsg)
	}
	return nil
}

func (c *Client) signedWebhook() string {
	if strings.TrimSpace(c.Secret) == "" {
		return c.Webhook
	}
	now := time.Now
	if c.Now != nil {
		now = c.Now
	}
	timestamp := now().UnixMilli()
	sign := Sign(timestamp, c.Secret)
	parsed, err := url.Parse(c.Webhook)
	if err != nil {
		return c.Webhook
	}
	query := parsed.Query()
	query.Set("timestamp", fmt.Sprintf("%d", timestamp))
	query.Set("sign", sign)
	parsed.RawQuery = query.Encode()
	return parsed.String()
}

func Sign(timestamp int64, secret string) string {
	stringToSign := fmt.Sprintf("%d\n%s", timestamp, secret)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(stringToSign))
	return base64.StdEncoding.EncodeToString(mac.Sum(nil))
}
