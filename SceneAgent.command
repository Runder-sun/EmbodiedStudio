#!/bin/zsh
cd "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ ! -d node_modules/three || ! -d node_modules/@modelcontextprotocol/sdk || ! -d node_modules/zod ]]; then
  npm ci || exit 1
fi
port=$(node --input-type=module -e 'import { config } from "./src/config.mjs"; console.log(config.port)') || exit 1
address="http://127.0.0.1:$port"
if curl -fsS "$address/api/state" >/dev/null 2>&1; then
  open "$address"
  exit 0
fi
node src/server.mjs &
studio_pid=$!
trap 'kill -TERM "$studio_pid" 2>/dev/null' EXIT INT TERM
for attempt in {1..30}; do
  if curl -fsS "$address/api/state" >/dev/null 2>&1; then
    open "$address"
    break
  fi
  sleep .2
done
wait "$studio_pid"
