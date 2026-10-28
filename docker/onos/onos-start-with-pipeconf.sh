#!/bin/bash
set -uo pipefail

READY_FILE=/tmp/idn-onos-ready
ONOS_PID=""
BOOTSTRAP_OK=true
ATOMIX_PARTITIONS_DIR=/root/onos/apache-karaf-4.2.9/data/db/partitions

# Atomix 3.1 persists its single-node member ID in the local Raft partitions.
# An ungraceful WSL/Docker stop can leave their lock markers and metadata with
# an old container address. The next *pre-JVM* start then either fails Raft
# storage locking or remains PASSIVE; AtomixManager retries after its first
# partially started instance already owns 9876. Archive only the two local
# test-mode Raft partitions before ONOS starts. Never touch Karaf cache,
# application data, logs, snapshots outside these partitions, or volumes.
recover_stale_atomix_locks() {
    local recovery_dir="$ATOMIX_PARTITIONS_DIR/.idn-stale-partition-backups"
    local recovery_stamp
    local lock_file
    local current_member_id
    local lock_member_id
    local partition_dir
    local reset_required=false
    recovery_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    current_member_id="$(hostname -i | awk '{print $1}')"

    for lock_file in \
        "$ATOMIX_PARTITIONS_DIR/system/partitions/1/.system-partition-1.lock" \
        "$ATOMIX_PARTITIONS_DIR/data/partitions/1/.raft-partition-1.lock"; do
        if [ -f "$lock_file" ]; then
            # RaftStorage deliberately accepts a marker owned by the same
            # member ID. The binary marker starts with a three-byte header;
            # only a different/empty owner proves an ungraceful restart with
            # incompatible persisted single-node state.
            lock_member_id="$(dd if="$lock_file" bs=1 skip=3 count=128 2>/dev/null | tr -d '\000')"
            if [ -z "$current_member_id" ] || [ -z "$lock_member_id" ] \
                || [ "$lock_member_id" != "$current_member_id" ]; then
                reset_required=true
                echo "[atomix-preflight] Incompatible Raft marker owner '$lock_member_id' for current member '$current_member_id': $lock_file"
            else
                echo "[atomix-preflight] Reusing compatible Raft marker for member '$current_member_id': $lock_file"
            fi
        fi
    done

    # Used only for the already-proven stale state currently running while a
    # graceful container restart removes its marker before this preflight.
    if [ -f "$ATOMIX_PARTITIONS_DIR/.idn-reset-atomix-state" ]; then
        reset_required=true
    fi

    if [ "$reset_required" != true ]; then
        return 0
    fi

    recovery_dir="$recovery_dir/$recovery_stamp"
    if ! mkdir -p "$recovery_dir"; then
        echo "[atomix-preflight] ERROR: could not create Raft recovery directory." >&2
        return 1
    fi
    for partition_dir in system data; do
        if [ -d "$ATOMIX_PARTITIONS_DIR/$partition_dir" ]; then
            if ! mv "$ATOMIX_PARTITIONS_DIR/$partition_dir" "$recovery_dir/$partition_dir"; then
                echo "[atomix-preflight] ERROR: could not preserve stale Raft partition: $partition_dir" >&2
                return 1
            fi
        fi
        if ! mkdir -p "$ATOMIX_PARTITIONS_DIR/$partition_dir"; then
            echo "[atomix-preflight] ERROR: could not recreate Raft partition directory: $partition_dir" >&2
            return 1
        fi
    done
    rm -f "$ATOMIX_PARTITIONS_DIR/.idn-reset-atomix-state"
    echo "[atomix-preflight] Archived stale single-node Raft partitions to: $recovery_dir"
}

shutdown_onos() {
    trap - TERM INT EXIT
    rm -f "$READY_FILE"
    if [ -n "$ONOS_PID" ] && kill -0 "$ONOS_PID" 2>/dev/null; then
        # onos-service launches a shell wrapper which in turn owns the Karaf
        # JVM. Signalling only the wrapper lets it exit while leaving Java to
        # Docker's forced cgroup teardown, skipping DS deactivate()/Atomix
        # unlock. Target the one Karaf JVM directly, with the wrapper as the
        # conservative fallback if startup never reached Java.
        ONOS_JAVA_PID="$(ps -eo pid=,args= | awk '/org\.apache\.karaf\.main\.Main/ {print $1; exit}')"
        if [ -n "$ONOS_JAVA_PID" ] && kill -0 "$ONOS_JAVA_PID" 2>/dev/null; then
            echo "[pipeconf-init] Forwarding graceful shutdown to Karaf JVM (pid=$ONOS_JAVA_PID)..."
            kill -TERM "$ONOS_JAVA_PID" 2>/dev/null || true
        else
            echo "[pipeconf-init] Karaf JVM not found; forwarding shutdown to wrapper (pid=$ONOS_PID)..."
            kill -TERM "$ONOS_PID" 2>/dev/null || true
        fi
        wait "$ONOS_PID" 2>/dev/null || true
    fi
}

trap shutdown_onos TERM INT EXIT
rm -f "$READY_FILE"

if ! recover_stale_atomix_locks; then
    echo "[atomix-preflight] Refusing to start ONOS because stale lock preservation failed." >&2
    exit 1
fi

echo "[pipeconf-init] Starting ONOS..."
/root/onos/bin/onos-service &
ONOS_PID=$!

KARAF_CLIENT="$(find /root/onos -path '*/apache-karaf-*/bin/client' -type f | head -n 1)"
run_karaf() {
    if [ -x "$KARAF_CLIENT" ]; then
        printf '%s\n' "$1" | "$KARAF_CLIENT" -b > /dev/null 2>&1 || true
    else
        echo "[pipeconf-init] Karaf client not found; skipped: $1"
    fi
}

wait_rest_ready() {
    echo "[pipeconf-init] Waiting for ONOS REST..."
    for i in $(seq 1 120); do
        if curl -fs -u onos:rocks -o /dev/null --max-time 3 \
            http://localhost:8181/onos/v1/applications 2>/dev/null; then
            echo "[pipeconf-init] ONOS REST ready."
            return 0
        fi
        sleep 2
    done
    echo "[pipeconf-init] WARNING: ONOS REST not ready before timeout."
    return 1
}

wait_core_bundles() {
    echo "[pipeconf-init] Waiting for ONOS core bundles..."
    for i in $(seq 1 180); do
        if [ -x "$KARAF_CLIENT" ]; then
            bundles="$(printf '%s\n' "bundle:list" | "$KARAF_CLIENT" -b 2>/dev/null || true)"
            if echo "$bundles" | grep -q "Active.*onos-core-primitives" \
               && echo "$bundles" | grep -q "Active.*onos-core-dist" \
               && echo "$bundles" | grep -q "Active.*onos-core-net"; then
                echo "[pipeconf-init] ONOS core bundles ready."
                return 0
            fi
        fi
        sleep 2
    done
    echo "[pipeconf-init] WARNING: ONOS core bundles not ready before timeout."
    return 1
}

activate_app() {
    curl -fsS -u onos:rocks -X POST --max-time 10 \
        "http://localhost:8181/onos/v1/applications/$1/active" \
        > /dev/null
}

app_is_active() {
    curl -fsS -u onos:rocks --max-time 5 \
        "http://localhost:8181/onos/v1/applications/$1" 2>/dev/null \
        | grep -Eq '"state"[[:space:]]*:[[:space:]]*"ACTIVE"'
}

wait_app_installed() {
    app="$1"
    for i in $(seq 1 90); do
        if curl -fs -u onos:rocks --max-time 3 \
            http://localhost:8181/onos/v1/applications 2>/dev/null \
            | grep -q "\"name\":\"$app\""; then
            return 0
        fi
        sleep 2
    done
    echo "[pipeconf-init] WARNING: app not found before activation: $app"
    return 1
}

activate_when_installed() {
    app="$1"
    if ! wait_app_installed "$app"; then
        return 1
    fi
    if app_is_active "$app"; then
        echo "[pipeconf-init] App already active: $app"
        return 0
    fi
    echo "[pipeconf-init] Activating app once: $app"
    # ONOS can return a transient non-2xx while the asynchronous activation
    # has already been accepted.  Do not terminate the controller process;
    # observe the state before deciding the one activation attempt failed.
    if ! activate_app "$app"; then
        echo "[pipeconf-init] Activation request returned non-2xx; waiting for state: $app"
    fi
    for i in $(seq 1 60); do
        if app_is_active "$app"; then
            echo "[pipeconf-init] App active: $app"
            return 0
        fi
        sleep 2
    done
    echo "[pipeconf-init] ERROR: app did not become active: $app"
    return 1
}

activate_required_app() {
    if ! activate_when_installed "$1"; then
        BOOTSTRAP_OK=false
    fi
}

if ! wait_rest_ready || ! wait_core_bundles; then
    echo "[pipeconf-init] ERROR: ONOS core did not become ready; leaving the process running for diagnostics."
    BOOTSTRAP_OK=false
fi

echo "[pipeconf-init] Activating P4Runtime and BMv2 apps..."
activate_required_app "org.onosproject.protocols.grpc"
activate_required_app "org.onosproject.protocols.p4runtime"
activate_required_app "org.onosproject.p4runtime"
activate_required_app "org.onosproject.drivers"
activate_required_app "org.onosproject.drivers.p4runtime"
activate_required_app "org.onosproject.drivers.bmv2"

echo "[pipeconf-init] Waiting for P4Runtime model bundle..."
for i in $(seq 1 90); do
    if [ -x "$KARAF_CLIENT" ] \
       && printf '%s\n' "bundle:list" | "$KARAF_CLIENT" -b 2>/dev/null \
          | grep -q "Active.*onos-protocols-p4runtime-model"; then
        echo "[pipeconf-init] P4Runtime model ready."
        break
    fi
    sleep 2
done

echo "[pipeconf-init] Waiting for ONOS services after feature refresh..."
if ! wait_rest_ready || ! wait_core_bundles; then
    BOOTSTRAP_OK=false
fi

echo "[pipeconf-init] Activating topology discovery apps..."
activate_required_app "org.onosproject.hostprovider"
activate_required_app "org.onosproject.lldpprovider"
activate_required_app "org.onosproject.proxyarp"
activate_required_app "org.onosproject.fwd"

echo "[pipeconf-init] Installing pipeconf bundle..."
if [ -x "$KARAF_CLIENT" ] && ! printf '%s\n' "bundle:list" | "$KARAF_CLIENT" -b | grep -q "ONOS IDN Basic Pipeconf"; then
    run_karaf "bundle:install -s file:/tmp/idn-pipeconf.oar"
fi
sleep 2
BUNDLE_ID=""
PIPECONF_ACTIVE=false
if [ -x "$KARAF_CLIENT" ]; then
    BUNDLE_ID="$(printf '%s\n' "bundle:list" | "$KARAF_CLIENT" -b | awk '/ONOS IDN Basic Pipeconf/ {print $1; exit}')"
fi
for i in $(seq 1 36); do
    if [ ! -x "$KARAF_CLIENT" ]; then
        break
    fi

    if printf '%s\n' "bundle:list" | "$KARAF_CLIENT" -b | grep -q "Active.*ONOS IDN Basic Pipeconf"; then
        PIPECONF_ACTIVE=true
        break
    fi

    if [ -z "$BUNDLE_ID" ]; then
        BUNDLE_ID="$(printf '%s\n' "bundle:list" | "$KARAF_CLIENT" -b | awk '/ONOS IDN Basic Pipeconf/ {print $1; exit}')"
    fi

    if [ -n "$BUNDLE_ID" ]; then
        run_karaf "bundle:start $BUNDLE_ID"
    fi
    sleep 5
done

if [ "$PIPECONF_ACTIVE" != true ]; then
    echo "[pipeconf-init] ERROR: IDN pipeconf bundle did not become active."
    BOOTSTRAP_OK=false
fi

if [ "$BOOTSTRAP_OK" = true ]; then
    touch "$READY_FILE"
    echo "[pipeconf-init] Done; readiness marker created."
else
    echo "[pipeconf-init] Bootstrap incomplete; readiness marker withheld."
fi

wait "$ONOS_PID"
