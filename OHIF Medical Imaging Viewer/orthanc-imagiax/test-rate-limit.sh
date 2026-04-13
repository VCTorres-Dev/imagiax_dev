#!/bin/sh
# Rate limiting verification script for nginx-rate-limiting change

echo ""
echo "=== TASK 4.1: Auth rate limit test (5r/m + burst=3 = 8 requests before 429) ==="
count=1
while [ $count -le 10 ]; do
  code=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "https://imagiax.dev/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"email":"test@test.com","password":"test"}')
  printf "  Request %2d: HTTP %s\n" "$count" "$code"
  count=$((count + 1))
done

echo ""
echo "=== TASK 4.1: 429 response body check ==="
body=$(curl -sk -X POST "https://imagiax.dev/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"test"}' \
  -w "\n---HTTP:%{http_code}---" \
  -D /tmp/rate_headers.txt)
echo "  Body: $body"
echo "  Headers:"
grep -i "retry-after\|content-type\|x-content-type" /tmp/rate_headers.txt || echo "  (check manually)"

echo ""
echo "=== TASK 4.2: PACS rate limit (60r/s + burst=50 — should allow normal access) ==="
code=$(curl -sk -o /dev/null -w "%{http_code}" "https://imagiax.dev/pacs/")
echo "  Single PACS request: HTTP $code (expect 401 or 302, NOT 429)"

echo ""
echo "=== TASK 4.3: Payment webhook — must NOT be rate limited ==="
count=1
all_ok=true
while [ $count -le 5 ]; do
  code=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "https://imagiax.dev/pagos/webhook" \
    -H "Content-Type: application/json" \
    -d '{"test":"webhook"}')
  printf "  Webhook %2d: HTTP %s\n" "$count" "$code"
  if [ "$code" = "429" ]; then
    all_ok=false
  fi
  count=$((count + 1))
done
if $all_ok; then
  echo "  PASS: No 429s on webhook"
else
  echo "  FAIL: Got 429 on webhook!"
fi

echo ""
echo "=== TASK 4.3: Health checks — must NOT be rate limited ==="
count=1
while [ $count -le 5 ]; do
  code=$(curl -sk -o /dev/null -w "%{http_code}" "https://imagiax.dev/salud")
  printf "  Health check %2d: HTTP %s\n" "$count" "$code"
  count=$((count + 1))
done

echo ""
echo "=== nginx -t (zones loaded correctly) ==="
docker exec nginx-imagiax nginx -t

echo ""
echo "=== Rate zones loaded in nginx ==="
docker exec nginx-imagiax cat /etc/nginx/conf.d/00-rate-zones.conf

echo ""
echo "=== Done ==="
