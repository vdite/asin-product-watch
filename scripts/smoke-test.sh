#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
image_name="asin-product-watch:smoke-test"
container_name="asin-product-watch-smoke-$$"
smoke_data="$(mktemp -d "${TMPDIR:-/tmp}/asin-product-watch-smoke.XXXXXX")"

cleanup() {
  docker rm -f "${container_name}" >/dev/null 2>&1 || true
  if [[ "${smoke_data}" == *asin-product-watch-smoke.* && -d "${smoke_data}" ]]; then
    rm -rf -- "${smoke_data}"
  fi
}
trap cleanup EXIT

cd "${project_dir}"

echo "[1/5] Image bauen"
docker build -t "${image_name}" .

echo "[2/5] Temporären Container starten"
docker run --detach \
  --name "${container_name}" \
  --publish 127.0.0.1::8080 \
  --volume "${smoke_data}:/data" \
  "${image_name}" >/dev/null

echo "[3/5] Auf Healthcheck warten"
health=""
for _ in {1..40}; do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${container_name}")"
  if [[ "${health}" == "healthy" ]]; then
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${container_name}")" != "true" ]]; then
    docker logs "${container_name}"
    echo "Container wurde unerwartet beendet." >&2
    exit 1
  fi
  sleep 1
done

if [[ "${health}" != "healthy" ]]; then
  docker logs "${container_name}"
  echo "Healthcheck wurde nicht rechtzeitig erfolgreich (Status: ${health})." >&2
  exit 1
fi

host_port="$(docker inspect --format '{{(index (index .NetworkSettings.Ports "8080/tcp") 0).HostPort}}' "${container_name}")"
base_url="http://127.0.0.1:${host_port}"

echo "[4/5] Health-Endpunkt und Weboberfläche prüfen"
health_json="$(curl --fail --silent --show-error "${base_url}/healthz")"
page_html="$(curl --fail --silent --show-error "${base_url}/")"

if [[ "${health_json}" != *'"status": "ok"'* ]]; then
  echo "Unerwartete Health-Antwort: ${health_json}" >&2
  exit 1
fi
if [[ "${page_html}" != *'<title>ASIN Product Watch</title>'* ]]; then
  echo "Weboberfläche enthält nicht den erwarteten Seitentitel." >&2
  exit 1
fi

echo "[5/5] Persistente Dateien prüfen"
if [[ ! -s "${smoke_data}/config.json" || ! -s "${smoke_data}/state.json" ]]; then
  echo "config.json oder state.json wurde nicht korrekt angelegt." >&2
  exit 1
fi

echo "OK: Image, Container, Healthcheck, Weboberfläche und /data funktionieren."
