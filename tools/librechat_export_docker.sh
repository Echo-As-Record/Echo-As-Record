#!/usr/bin/env bash
# Optional Linux/Docker wrapper for the single-account, read-only exporter.
# Requires explicit EAR_EXPORT_ENV_FILE, EAR_EXPORT_APP_CONTAINER and EAR_EXPORT_NETWORK.
# The env file is private and supplies the single account ID and read-only MongoDB URI.
# The existing app image is reused (no pull); source volumes are mounted read-only.
# Request (stdin): ear-librechat-export-request/1 JSON. Result (stdout): NDJSON.
set -euo pipefail
umask 077

ENV_FILE="${EAR_EXPORT_ENV_FILE:?set EAR_EXPORT_ENV_FILE explicitly}"
APP_CONTAINER="${EAR_EXPORT_APP_CONTAINER:?set EAR_EXPORT_APP_CONTAINER explicitly}"
NETWORK="${EAR_EXPORT_NETWORK:?set EAR_EXPORT_NETWORK explicitly}"
RUN_UID="${EAR_EXPORT_UID:-65534}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORTER="$SCRIPT_DIR/librechat_export.mjs"

die() { echo "librechat_export_docker: $*" >&2; exit 2; }

extra=()
while [ $# -gt 0 ]; do
  case "$1" in
    --pause-ms|--batch-size|--max-file-bytes|--overlap-seconds)
      [ $# -ge 2 ] && [[ "$2" =~ ^[0-9]{1,10}$ ]] || die "bad value for $1"
      extra+=("$1" "$2"); shift 2 ;;
    *) die "option not allowed" ;;
  esac
done

[ -f "$EXPORTER" ] || die "librechat_export.mjs must sit next to this script"
[ -f "$ENV_FILE" ] && [ ! -L "$ENV_FILE" ] || die "env file missing or a symlink"
mode="$(stat -c '%a' "$ENV_FILE")"; owner="$(stat -c '%u' "$ENV_FILE")"
case "$mode" in 400|600) ;; *) die "env file must be mode 0600 or 0400" ;; esac
[[ "$RUN_UID" =~ ^[1-9][0-9]{0,8}$ ]] || die "export user must be non-root"
[ "$owner" = "$(id -u)" ] || die "env file must belong to the invoking user"
grep -qE '^EAR_EXPORT_MONGO_URI=.+' "$ENV_FILE" || die "EAR_EXPORT_MONGO_URI missing in env file"
grep -qE '^EAR_EXPORT_USER_ID=[0-9a-f]{24}$' "$ENV_FILE" || die "EAR_EXPORT_USER_ID must be 24 hex digits"

image="$(docker container inspect -f '{{.Image}}' "$APP_CONTAINER")" || die "cannot inspect $APP_CONTAINER"
volume_of() {
  docker container inspect -f '{{range .Mounts}}{{if eq .Destination "'"$1"'"}}{{.Type}} {{.Name}}{{end}}{{end}}' \
    "$APP_CONTAINER"
}
uploads="$(volume_of /app/uploads)"; images="$(volume_of /app/client/public/images)"
[[ "$uploads" == "volume "* && "$images" == "volume "* ]] || die "expected named volumes for uploads and images"

exec docker run --rm -i --pull=never \
  --name "ear-librechat-export-$$" \
  --network "$NETWORK" \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 64 --memory 192m --memory-swap 192m --cpus 0.5 \
  --user "$RUN_UID:$RUN_UID" -e HOME=/tmp \
  --env-file "$ENV_FILE" \
  -v "$EXPORTER:/export/librechat_export.mjs:ro" \
  -v "${uploads#volume }:/app/uploads:ro" \
  -v "${images#volume }:/app/client/public/images:ro" \
  --entrypoint node "$image" /export/librechat_export.mjs --request-stdin ${extra[@]+"${extra[@]}"}
