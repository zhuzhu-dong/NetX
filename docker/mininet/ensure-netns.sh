#!/bin/bash
# ============================================================
# ensure-netns.sh — make Mininet network namespaces visible
#                   to docker exec by setting up rshared mount.
# ============================================================
set -e

echo "[mininet-init] Setting up /var/run/netns for docker exec access..."

# Create netns directory if missing
mkdir -p /var/run/netns

# Make the mount rshared so netns created by Mininet child
# processes are visible to docker exec (which runs in a
# separate mount namespace).
mount --make-rshared /var/run/netns 2>/dev/null || true

# On container restart, clean up any stale netns entries
# left over from a previous run (they are invalid after restart).
for ns in /var/run/netns/*; do
    if [ -f "$ns" ]; then
        umount "$ns" 2>/dev/null || true
        rm -f "$ns"
    fi
done

echo "[mininet-init] Netns ready. Starting Mininet topology..."
exec "$@"
