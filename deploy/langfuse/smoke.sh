#!/usr/bin/env bash
# Langfuse 端到端连通性自检（在部署服务器上运行）
# 走 v4 的 OTLP 入口 -> MinIO -> worker -> ClickHouse，检查 events_core 是否 +1
set -euo pipefail

source /opt/langfuse/.env
HOST=http://127.0.0.1:3000
TS=$(date +%s%N)
TRACE=$(openssl rand -hex 16)
SPAN=$(openssl rand -hex 8)

cat > /tmp/otel-smoke.json <<EOF
{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"smoke-test"}}]},"scopeSpans":[{"scope":{"name":"smoke"},"spans":[{"traceId":"$TRACE","spanId":"$SPAN","name":"smoke-span","kind":1,"startTimeUnixNano":"$TS","endTimeUnixNano":"$((TS+1000000))","attributes":[{"key":"langfuse.trace.name","value":{"stringValue":"smoke-$TS"}}]}]}]}]}
EOF

before=$(podman exec langfuse-clickhouse clickhouse-client --user clickhouse \
  --password "$CLICKHOUSE_PASSWORD" -q "select count() from default.events_core")

resp=$(curl -s -m 15 -u "$LANGFUSE_INIT_PROJECT_PUBLIC_KEY:$LANGFUSE_INIT_PROJECT_SECRET_KEY" \
  -X POST "$HOST/api/public/otel/v1/traces" -H 'Content-Type: application/json' \
  --data @/tmp/otel-smoke.json)

if grep -q '"validKey":true' <<<"$resp"; then
  echo "[ok] OTLP 上报被接受（已写入 S3 队列）"
else
  echo "[fail] OTLP 上报被拒: ${resp:0:300}"
  exit 1
fi

for _ in $(seq 1 15); do
  sleep 2
  after=$(podman exec langfuse-clickhouse clickhouse-client --user clickhouse \
    --password "$CLICKHOUSE_PASSWORD" -q "select count() from default.events_core")
  if [ "$after" -gt "$before" ]; then
    echo "[ok] ClickHouse events_core: $before -> $after（worker 消费成功）"
    echo "[ok] traceId=$TRACE  在 UI 的 Traces 里按时间排序应能看到 smoke-$TS"
    exit 0
  fi
done

echo "[warn] 10s 内 events_core 未增加（before=$before），检查 worker 日志: podman logs langfuse-worker"
exit 1
