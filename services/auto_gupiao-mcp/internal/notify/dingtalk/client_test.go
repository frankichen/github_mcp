package dingtalk

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestSendMarkdownWithoutSecret(t *testing.T) {
	var gotPath string
	var gotQuery string
	var gotPayload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		if err := json.NewDecoder(r.Body).Decode(&gotPayload); err != nil {
			t.Fatalf("decode payload: %v", err)
		}
		_, _ = w.Write([]byte("{\"errcode\":0,\"errmsg\":\"ok\"}"))
	}))
	defer server.Close()

	client := NewClient(server.URL+"/robot/send?token=abc", "")
	if err := client.SendMarkdown(context.Background(), MarkdownMessage{Title: "日报", Text: "## hello"}); err != nil {
		t.Fatalf("SendMarkdown failed: %v", err)
	}
	if gotPath != "/robot/send" || gotQuery != "token=abc" {
		t.Fatalf("unexpected url path/query: %s?%s", gotPath, gotQuery)
	}
	if gotPayload["msgtype"] != "markdown" {
		t.Fatalf("unexpected payload: %+v", gotPayload)
	}
}

func TestSignedWebhookWithSecret(t *testing.T) {
	client := NewClient("https://example.com/robot/send?token=abc", "SECxxx")
	client.Now = func() time.Time { return time.UnixMilli(1700000000123) }
	signed := client.signedWebhook()
	if !strings.Contains(signed, "token=abc") || !strings.Contains(signed, "timestamp=1700000000123") || !strings.Contains(signed, "sign=") {
		t.Fatalf("unexpected signed url: %s", signed)
	}
	if strings.Contains(signed, "SECxxx") {
		t.Fatalf("signed url should not leak secret: %s", signed)
	}
}

func TestSendMarkdownAPIError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("{\"errcode\":310000,\"errmsg\":\"keywords not in content\"}"))
	}))
	defer server.Close()

	client := NewClient(server.URL+"/robot/send?token=abc", "")
	err := client.SendMarkdown(context.Background(), MarkdownMessage{Title: "日报", Text: "hello"})
	if err == nil || !strings.Contains(err.Error(), "dingtalk api error") {
		t.Fatalf("expected api error, got %v", err)
	}
}
