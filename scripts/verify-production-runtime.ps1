param(
    [string]$HostName = "root@imagiax.dev",
    [string]$SshKey = "$env:USERPROFILE\.ssh\do_imagiax",
    [string]$Container = "fastapi-imagiax"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $SshKey)) {
    throw "SSH key not found: $SshKey"
}

$remoteScript = @"
set -eu

CONTAINER="$Container"

echo "--- docker ps ---"
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

echo "--- fastapi inspect ---"
docker inspect --format 'image={{.Image}} created={{.Created}} cmd={{json .Config.Cmd}}' "$Container" 2>/dev/null || true

echo "--- python module resolution ---"
docker exec "$Container" sh -lc "python - <<'PY'
import importlib.util

for module in ['api.main', 'src.api.main', 'app.main', 'servidor_api_orthanc_v1']:
    try:
        spec = importlib.util.find_spec(module)
        origin = spec.origin if spec else 'MISSING'
        print(module + ' => ' + origin)
    except Exception as exc:
        print(module + ' => ERROR: ' + type(exc).__name__ + ': ' + str(exc))
PY" 2>/dev/null || true

echo "--- internal health ---"
docker exec "$Container" sh -lc 'curl -s -o /dev/null -w "salud=%{http_code}\n" http://localhost:8010/salud || true'
docker exec "$Container" sh -lc 'curl -s -o /dev/null -w "ping=%{http_code}\n" http://localhost:8010/ping || true'
docker exec "$Container" sh -lc 'curl -s -o /dev/null -w "api_salud=%{http_code}\n" http://localhost:8010/api/salud || true'

echo "--- public health ---"
curl -sk -o /dev/null -w 'public_salud=%{http_code}\n' https://imagiax.dev/salud || true
curl -sk -o /dev/null -w 'public_root=%{http_code}\n' https://imagiax.dev/ || true
"@

$sshArgs = @(
    "-o", "ConnectTimeout=15",
    "-o", "StrictHostKeyChecking=no",
    "-i", $SshKey,
    $HostName,
    "bash -s"
)

$remoteScript | & ssh @sshArgs
