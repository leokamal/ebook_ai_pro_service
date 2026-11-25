#!/bin/sh
# Persistent disks (e.g. Render) are mounted root-owned: fix ownership, then drop privileges to `app`.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${CHROMA_PERSIST_DIRECTORY:-/data/chroma_pro}" "${HF_HOME:-/data/hf}"
    # cheap: only touch what is not already owned by app (a big model cache is not re-chowned on every start)
    find /data -maxdepth 3 ! -user app -exec chown -R app:app {} + 2>/dev/null || true
    exec gosu app "$@"
fi
exec "$@"
