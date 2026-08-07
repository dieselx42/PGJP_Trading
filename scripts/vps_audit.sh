#!/usr/bin/env bash
# =============================================================================
# VPS audit -- READ ONLY
#
# Run this ON srv1792440.hstgr.cloud and send me the output:
#
#     bash scripts/vps_audit.sh > vps-audit.txt 2>&1
#
# This script CHANGES NOTHING. It does not stop, start, remove, or reconfigure
# anything. It does not touch Traefik. It does not touch the firewall. Every
# command in it is a query.
#
# It is safe to read the output before sending: it deliberately does not print
# file contents that could contain secrets (no .env dumps, no key material, no
# Docker `inspect` of environment variables). Container names, image names, and
# port bindings are printed because those are what the audit is for.
# =============================================================================

set -uo pipefail

section() {
    printf '\n================================================================\n'
    printf '== %s\n' "$1"
    printf '================================================================\n'
}

# Run a command if it exists, otherwise say so rather than emitting a confusing
# "command not found" that looks like a failure.
try() {
    local name="$1"; shift
    if command -v "$name" >/dev/null 2>&1; then
        "$name" "$@" 2>&1
    else
        echo "(not installed: $name)"
    fi
}

printf 'sol-futures-trading-bot :: VPS audit\n'
printf 'generated: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'NOTE: this script is read-only and changes nothing.\n'

# -----------------------------------------------------------------------------
section "IDENTITY AND HOST"
echo "hostname:        $(hostname -f 2>/dev/null || hostname)"
echo "current user:    $(id -un) (uid=$(id -u))"
echo "uptime:          $(uptime -p 2>/dev/null || uptime)"
echo
echo "--- OS release ---"
cat /etc/os-release 2>/dev/null || echo "(no /etc/os-release)"
echo
echo "--- kernel ---"
uname -a

# -----------------------------------------------------------------------------
section "HARDWARE AND CAPACITY"
echo "--- CPU ---"
echo "architecture:    $(uname -m)"
echo "cpu count:       $(nproc 2>/dev/null || echo unknown)"
lscpu 2>/dev/null | grep -E '^(Model name|Vendor ID|CPU\(s\)|Thread|Core)' || true
echo
echo "--- memory ---"
free -h 2>/dev/null || echo "(free unavailable)"
echo
echo "--- disk ---"
df -hT 2>/dev/null | grep -vE '^(tmpfs|devtmpfs|overlay)' || df -h
echo
echo "--- disk used by docker ---"
try docker system df

# -----------------------------------------------------------------------------
section "TOOLCHAIN VERSIONS"
echo "docker:          $(try docker --version)"
echo "docker compose:  $(docker compose version 2>&1 || echo '(v2 plugin not installed)')"
echo "docker-compose:  $(try docker-compose --version)"
echo "git:             $(try git --version)"
echo "python3:         $(try python3 --version)"
echo "python3.12:      $(try python3.12 --version)"
echo "make:            $(try make --version | head -1)"

# -----------------------------------------------------------------------------
section "DOCKER :: RUNNING CONTAINERS"
try docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

section "DOCKER :: ALL CONTAINERS (INCLUDING STOPPED)"
try docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

section "DOCKER :: IMAGES"
try docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}'

section "DOCKER :: NETWORKS"
try docker network ls
echo
echo "--- network details (subnets only) ---"
if command -v docker >/dev/null 2>&1; then
    for net in $(docker network ls --format '{{.Name}}' 2>/dev/null); do
        printf '%s: %s\n' "$net" \
            "$(docker network inspect "$net" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null)"
    done
fi

section "DOCKER :: VOLUMES"
try docker volume ls

section "DOCKER :: PUBLISHED PORTS (what is reachable from outside a container)"
if command -v docker >/dev/null 2>&1; then
    docker ps --format '{{.Names}}' 2>/dev/null | while read -r name; do
        ports=$(docker port "$name" 2>/dev/null)
        if [ -n "$ports" ]; then
            printf '\n%s:\n%s\n' "$name" "$ports"
        fi
    done
    echo
    echo "(a container with no output above publishes nothing)"
fi

# -----------------------------------------------------------------------------
section "TRAEFIK (LEAVE ALONE -- REPORTING ONLY)"
if command -v docker >/dev/null 2>&1; then
    traefik=$(docker ps -a --filter 'name=traefik' --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null)
    if [ -n "$traefik" ]; then
        echo "$traefik"
        echo
        echo "--- traefik published ports ---"
        docker ps --filter 'name=traefik' --format '{{.Names}}: {{.Ports}}' 2>/dev/null
        echo
        echo "--- traefik mounts (paths only) ---"
        for name in $(docker ps -a --filter 'name=traefik' --format '{{.Names}}' 2>/dev/null); do
            docker inspect "$name" --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{.Mode}}){{"\n"}}{{end}}' 2>/dev/null
        done
        echo
        echo "--- traefik restart policy ---"
        for name in $(docker ps -a --filter 'name=traefik' --format '{{.Names}}' 2>/dev/null); do
            docker inspect "$name" --format '{{.Name}}: {{.HostConfig.RestartPolicy.Name}}' 2>/dev/null
        done
    else
        echo "no container matching 'traefik' found"
    fi
fi
echo
echo "--- traefik-looking directories ---"
ls -la /opt 2>/dev/null | grep -i traefik || echo "(none under /opt)"
find / -maxdepth 4 -iname '*traefik*' -not -path '/proc/*' -not -path '/sys/*' \
     -not -path '/var/lib/docker/*' 2>/dev/null | head -20

# -----------------------------------------------------------------------------
section "OPENCLAW REMNANTS (REPORT ONLY -- NOTHING IS DELETED)"
found_any=0
if command -v docker >/dev/null 2>&1; then
    echo "--- containers ---"
    out=$(docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null | grep -i -E 'openclaw|open-claw|claw' )
    if [ -n "$out" ]; then echo "$out"; found_any=1; else echo "(none)"; fi

    echo
    echo "--- images ---"
    out=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -i -E 'openclaw|open-claw|claw')
    if [ -n "$out" ]; then echo "$out"; found_any=1; else echo "(none)"; fi

    echo
    echo "--- volumes ---"
    out=$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -i -E 'openclaw|open-claw|claw')
    if [ -n "$out" ]; then echo "$out"; found_any=1; else echo "(none)"; fi

    echo
    echo "--- networks ---"
    out=$(docker network ls --format '{{.Name}}' 2>/dev/null | grep -i -E 'openclaw|open-claw|claw')
    if [ -n "$out" ]; then echo "$out"; found_any=1; else echo "(none)"; fi
fi
echo
echo "--- directories ---"
out=$(find / -maxdepth 4 -iname '*openclaw*' -not -path '/proc/*' -not -path '/sys/*' \
      -not -path '/var/lib/docker/*' 2>/dev/null | head -20)
if [ -n "$out" ]; then echo "$out"; found_any=1; else echo "(none)"; fi
echo
echo "--- systemd units ---"
out=$(systemctl list-unit-files 2>/dev/null | grep -i -E 'openclaw|claw')
if [ -n "$out" ]; then echo "$out"; found_any=1; else echo "(none)"; fi
echo
if [ "$found_any" -eq 0 ]; then
    echo "RESULT: no OpenClaw remnants detected."
else
    echo "RESULT: possible OpenClaw remnants listed above. Nothing was removed."
    echo "        Review each item before deleting; some may hold unrelated data."
fi

# -----------------------------------------------------------------------------
section "NETWORK :: LISTENING SOCKETS"
echo "--- all listening TCP/UDP ---"
if command -v ss >/dev/null 2>&1; then
    ss -tulpn 2>/dev/null
else
    try netstat -tulpn
fi
echo
echo "--- listening on a NON-loopback address (i.e. potentially public) ---"
if command -v ss >/dev/null 2>&1; then
    ss -tlpn 2>/dev/null | awk 'NR==1 || ($4 !~ /^(127\.|\[::1\])/)'
fi
echo
echo "--- IB Gateway ports (4001/4002) MUST be absent or loopback-only ---"
if command -v ss >/dev/null 2>&1; then
    ss -tlpn 2>/dev/null | grep -E ':(4001|4002)\b' || echo "(nothing listening on 4001/4002 -- expected at this stage)"
fi

section "NETWORK :: FIREWALL"
echo "--- ufw ---"
try ufw status verbose
echo
echo "--- firewalld ---"
try firewall-cmd --list-all
echo
echo "--- nftables ruleset (summary) ---"
try nft list ruleset | head -60
echo
echo "--- iptables filter table ---"
try iptables -S | head -60
echo
echo "--- iptables nat table (Docker publishes ports here) ---"
try iptables -t nat -S | head -60

# -----------------------------------------------------------------------------
section "SSH CONFIGURATION"
echo "--- effective sshd settings (key ones) ---"
if command -v sshd >/dev/null 2>&1; then
    sshd -T 2>/dev/null | grep -iE '^(port|permitrootlogin|passwordauthentication|pubkeyauthentication|permitemptypasswords|x11forwarding|allowusers|allowgroups|maxauthtries|clientaliveinterval)' \
      || echo "(sshd -T unavailable; are you root?)"
else
    grep -vE '^\s*#|^\s*$' /etc/ssh/sshd_config 2>/dev/null | grep -iE 'port|permitrootlogin|passwordauthentication|pubkeyauthentication' \
      || echo "(cannot read sshd config)"
fi
echo
echo "--- authorized_keys (fingerprints only, no key material) ---"
for keyfile in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
    [ -f "$keyfile" ] || continue
    echo "$keyfile:"
    ssh-keygen -l -f "$keyfile" 2>/dev/null | sed 's/^/  /' || echo "  (unreadable)"
done
echo
echo "--- recent failed SSH auth attempts (count) ---"
{ grep -c 'Failed password' /var/log/auth.log 2>/dev/null \
  || journalctl -u ssh -u sshd --since '7 days ago' 2>/dev/null | grep -c 'Failed password' \
  || echo "unknown"; } | tail -1

# -----------------------------------------------------------------------------
section "DEPLOYMENT CREDENTIALS ALREADY PRESENT"
echo "--- SSH keys on this host (public/private presence only) ---"
for d in /root/.ssh /home/*/.ssh; do
    [ -d "$d" ] || continue
    echo "$d:"
    ls -la "$d" 2>/dev/null | awk '{print "  " $1, $3, $4, $9}'
done
echo
echo "--- deploy-key-looking entries in authorized_keys comments ---"
grep -ohE '(deploy|github|gh-actions|ci)[^ ]*$' /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys 2>/dev/null \
  | sort -u || echo "(none obvious)"
echo
echo "--- git global config (user/remote helpers only) ---"
try git config --global --list
echo
echo "--- existing checkouts under /opt ---"
ls -la /opt 2>/dev/null || echo "(no /opt)"

# -----------------------------------------------------------------------------
section "RUNNING SERVICES"
echo "--- systemd services (running) ---"
try systemctl list-units --type=service --state=running --no-pager --no-legend | head -40
echo
echo "--- top processes by memory ---"
ps aux --sort=-%mem 2>/dev/null | head -12

# -----------------------------------------------------------------------------
section "EXISTING TRADING BOT INSTALLATION (if any)"
for path in /opt/sol-futures-trading-bot /opt/PGJP_Trading /srv/sol-futures-trading-bot; do
    if [ -d "$path" ]; then
        echo "$path exists:"
        ls -la "$path" | sed 's/^/  /'
        if [ -f "$path/.env" ]; then
            echo "  .env permissions: $(stat -c '%a %U:%G' "$path/.env" 2>/dev/null)"
            echo "  .env safety variables (values shown are safety flags, not secrets):"
            grep -E '^(TRADING_MODE|ALLOW_ORDER_TRANSMIT|LIVE_TRADING_ENABLED|KILL_SWITCH|SOL_FUTURES_PERMISSION_READY|MAX_)' \
                "$path/.env" 2>/dev/null | sed 's/^/    /'
        fi
    fi
done
echo "(nothing above means the bot is not installed yet, which is expected)"

# -----------------------------------------------------------------------------
section "AUDIT COMPLETE"
echo "Nothing on this system was modified by this script."
echo "Send the whole file back; it contains no secrets or key material."
