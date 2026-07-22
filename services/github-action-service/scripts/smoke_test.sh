#!/bin/bash
set -e

BASE_URL="https://github.555044.xyz"
API_KEY="${ACTION_API_KEY:-}"

echo "=== 1. Health Check ==="
curl -s "$BASE_URL/health" | python3 -m json.tool
echo ""

echo "=== 2. OpenAPI Schema ==="
curl -s -o /dev/null -w "HTTP %{http_code} Content-Type: %{content_type}\n" "$BASE_URL/actions-openapi.json"
echo ""

echo "=== 3. Auth Test (wrong key) ==="
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer wrong-key" \
    "$BASE_URL/api/v1/github/file?repository=owner/repo&path=README.md&ref=main")
if [ "$HTTP_CODE" = "401" ]; then
    echo "PASS: Wrong API key returned 401"
else
    echo "FAIL: Expected 401, got $HTTP_CODE"
fi
echo ""

echo "=== 4. Privacy Endpoint ==="
curl -s "$BASE_URL/privacy" | python3 -m json.tool
echo ""

echo "=== 5. Nginx Status ==="
ssh root@de "systemctl status nginx --no-pager" 2>/dev/null | head -5
echo ""

echo "=== 6. Docker Status ==="
ssh root@de "docker compose -f /opt/github-action-service/docker-compose.yml ps" 2>/dev/null
echo ""

echo "=== 7. Docker Logs ==="
ssh root@de "docker compose -f /opt/github-action-service/docker-compose.yml logs --tail=20" 2>/dev/null
echo ""

echo "=== Smoke test complete ==="
