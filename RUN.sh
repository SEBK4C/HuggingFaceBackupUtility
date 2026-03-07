#!/usr/bin/env bash
# ============================================================================
# run.sh — HF Dual-Core Downloader & Gitea Mirror
# Version: 1.0.0
# OS Support: Linux (x86_64, aarch64) & macOS (arm64, x86_64)
# Constraints: ONLY `curl` and `uv`. NO `apt`, `brew`, or system Python.
# ============================================================================
set -euo pipefail

# ── Project Config ──────────────────────────────────────────────────────────
PROJECT_NAME="hf-mirror"
PYTHON_VERSION="3.12"
ENV_FILE=".env"
SRC_DIR="src"
GITEA_VERSION="1.25.4"
GIT_LFS_VERSION="3.5.1"
GITEA_DATA_DIR="gitea-data"
BIN_DIR="bin"
RUN_DIR="run"
VENV_DIR=".venv"
LOCAL_BIN="$PWD/.bin"

# Python dependencies — synced on every boot
DEPENDENCIES=(
    "typer[all]"
    "rich"
    "gradio"
    "pydantic"
    "pydantic-settings"
    "python-dotenv"
    "httpx[http2]"
    "anyio"
    "aiosqlite"
    "huggingface-hub"
    "pytest"
    "pytest-asyncio"
)

# Environment keys
REQUIRED_KEYS=(
    "HF_TOKEN|Hugging Face API token (hf_...)"
)

OPTIONAL_KEYS=(
    "TIER1_PATH|./downloads"
    "TIER2_PATH|"
    "TIER_THRESHOLD_PERCENT|10"
    "GITEA_PORT|3000"
    "GITEA_ADMIN_USER|hfmirror"
    "GITEA_ADMIN_PASSWORD|"
    "GRADIO_PORT|7860"
    "GRADIO_SHARE|false"
    "HF_CONCURRENT_DOWNLOADS|4"
    "HF_CHUNK_SIZE_MB|64"
    "HF_RETRY_ATTEMPTS|5"
    "HF_RETRY_BACKOFF_BASE|2"
    "LOG_LEVEL|INFO"
)

# ── Utilities ───────────────────────────────────────────────────────────────
info()  { printf '\033[1;34m[info]\033[0m  %s\n' "$*"; }
ok()    { printf '\033[1;32m[ok]\033[0m    %s\n' "$*"; }
warn()  { printf '\033[1;33m[warn]\033[0m  %s\n' "$*"; }
err()   { printf '\033[1;31m[err]\033[0m   %s\n' "$*" >&2; }

command_exists() { command -v "$1" &>/dev/null; }

# ── Process Management ──────────────────────────────────────────────────────
PID_GITEA="$RUN_DIR/gitea.pid"
PID_GRADIO="$RUN_DIR/gradio.pid"

cleanup() {
    local exit_code=$?
    for pidfile in "$PID_GITEA" "$PID_GRADIO"; do
        if [ -f "$pidfile" ]; then
            local pid
            pid=$(<"$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
                # Give it 5s to die gracefully before SIGKILL
                for _ in $(seq 1 10); do
                    kill -0 "$pid" 2>/dev/null || break
                    sleep 0.5
                done
                kill -9 "$pid" 2>/dev/null || true
            fi
            rm -f "$pidfile"
        fi
    done
    exit "$exit_code"
}
trap cleanup SIGINT SIGTERM EXIT

clean_stale_pids() {
    for pidfile in "$PID_GITEA" "$PID_GRADIO"; do
        if [ -f "$pidfile" ]; then
            local pid
            pid=$(<"$pidfile")
            if ! kill -0 "$pid" 2>/dev/null; then
                warn "Stale PID file: $pidfile (process $pid dead). Removing."
                rm -f "$pidfile"
            fi
        fi
    done
}

# ── Step 1: UV Bootstrap ───────────────────────────────────────────────────
if ! command_exists uv; then
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    if ! command_exists uv; then
        err "uv installation failed."
        exit 1
    fi
    ok "uv installed."
fi

# ── Step 1.5: Git LFS Bootstrap (cross-platform, rootless via curl) ────────
mkdir -p "$LOCAL_BIN"
export PATH="$LOCAL_BIN:$PATH"

grep -qxF '.bin/' .gitignore 2>/dev/null || echo '.bin/' >> .gitignore

if ! command_exists git-lfs; then
    info "git-lfs not found. Bootstrapping via curl into local project space..."
    LFS_OS=$(uname -s | tr '[:upper:]' '[:lower:]')
    LFS_ARCH=$(uname -m)
    [ "$LFS_ARCH" = "x86_64" ]  && LFS_ARCH="amd64"
    [ "$LFS_ARCH" = "aarch64" ] && LFS_ARCH="arm64"

    LFS_BASE_URL="https://github.com/git-lfs/git-lfs/releases/download/v${GIT_LFS_VERSION}/git-lfs-${LFS_OS}-${LFS_ARCH}-v${GIT_LFS_VERSION}"

    if [ "$LFS_OS" = "darwin" ]; then
        curl -LsSf "${LFS_BASE_URL}.zip" -o lfs_temp.zip
        unzip -q -j lfs_temp.zip "git-lfs-${GIT_LFS_VERSION}/git-lfs" -d "$LOCAL_BIN"
        rm lfs_temp.zip
    else
        curl -LsSf "${LFS_BASE_URL}.tar.gz" -o lfs_temp.tar.gz
        tar -xzf lfs_temp.tar.gz -C "$LOCAL_BIN" --strip-components=1 "git-lfs-${GIT_LFS_VERSION}/git-lfs"
        rm lfs_temp.tar.gz
    fi

    chmod +x "$LOCAL_BIN/git-lfs"
    git lfs install --skip-repo >/dev/null 2>&1
    ok "git-lfs installed locally to $LOCAL_BIN."
else
    ok "git-lfs is already available."
fi

# ── Step 2: System Prerequisites ───────────────────────────────────────────
REQUIRED_TOOLS=("curl" "git" "tar")
MISSING=()

for tool in "${REQUIRED_TOOLS[@]}"; do
    command_exists "$tool" || MISSING+=("$tool")
done

if [ ${#MISSING[@]} -gt 0 ]; then
    err "Missing required tools: ${MISSING[*]}"
    OS_TYPE="$(uname -s)"
    case "$OS_TYPE" in
        Linux)  info "Try: sudo apt-get install ${MISSING[*]}" ;;
        Darwin) info "Try: brew install ${MISSING[*]}" ;;
    esac
    exit 1
fi

# ── Step 3: Python & Virtual Environment ───────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    info "Installing Python $PYTHON_VERSION via uv..."
    uv python install "$PYTHON_VERSION"
    info "Creating virtual environment..."
    uv venv --python "$PYTHON_VERSION" "$VENV_DIR"
    ok "Environment ready."
fi

info "Syncing dependencies..."
uv pip install --quiet --python "$VENV_DIR/bin/python" "${DEPENDENCIES[@]}"
ok "Dependencies synced."

# ── Step 4: Gitea Binary (Platform-Aware Download + SHA256 Verify) ─────────
mkdir -p "$BIN_DIR" "$RUN_DIR"

if [ ! -x "$BIN_DIR/gitea" ]; then
    OS_TYPE="$(uname -s | tr '[:upper:]' '[:lower:]')"
    ARCH="$(uname -m)"

    # Gitea asset names: darwin uses "darwin-10.12-{arch}", linux uses "linux-{arch}"
    case "${OS_TYPE}-${ARCH}" in
        linux-x86_64)    GITEA_ASSET="gitea-${GITEA_VERSION}-linux-amd64" ;;
        linux-aarch64)   GITEA_ASSET="gitea-${GITEA_VERSION}-linux-arm64" ;;
        darwin-arm64)    GITEA_ASSET="gitea-${GITEA_VERSION}-darwin-10.12-arm64" ;;
        darwin-x86_64)   GITEA_ASSET="gitea-${GITEA_VERSION}-darwin-10.12-amd64" ;;
        *)
            err "Unsupported platform: ${OS_TYPE}-${ARCH}"
            exit 1
            ;;
    esac

    GITEA_BASE="https://github.com/go-gitea/gitea/releases/download/v${GITEA_VERSION}"
    GITEA_URL="${GITEA_BASE}/${GITEA_ASSET}"
    GITEA_SHA_URL="${GITEA_URL}.sha256"

    info "Downloading Gitea ${GITEA_VERSION} for ${OS_TYPE}/${ARCH}..."
    if ! curl -fSL --progress-bar -o "$BIN_DIR/gitea" "$GITEA_URL"; then
        err "Failed to download Gitea from $GITEA_URL"
        exit 1
    fi
    chmod +x "$BIN_DIR/gitea"

    # Verify checksum
    info "Verifying SHA256..."
    EXPECTED_SHA=$(curl -fsSL "$GITEA_SHA_URL" | awk '{print $1}')
    if command_exists sha256sum; then
        ACTUAL_SHA=$(sha256sum "$BIN_DIR/gitea" | awk '{print $1}')
    else
        ACTUAL_SHA=$(shasum -a 256 "$BIN_DIR/gitea" | awk '{print $1}')
    fi

    if [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
        err "Gitea checksum mismatch!"
        err "Expected: $EXPECTED_SHA"
        err "Got:      $ACTUAL_SHA"
        rm -f "$BIN_DIR/gitea"
        exit 1
    fi
    ok "Gitea ${GITEA_VERSION} verified and installed."
fi

# ── Step 5: .env Provisioning ──────────────────────────────────────────────
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Required keys — prompt interactively (skip if not a terminal)
for entry in "${REQUIRED_KEYS[@]}"; do
    [[ "$entry" == \#* ]] && continue
    KEY_NAME="${entry%%|*}"
    KEY_DESC="${entry##*|}"

    if ! grep -q "^${KEY_NAME}=" "$ENV_FILE" 2>/dev/null; then
        if [ -t 0 ]; then
            warn "Missing: ${KEY_NAME} — ${KEY_DESC}"
            read -r -p "     Enter value (or Enter to skip): " USER_INPUT || true
            if [ -n "${USER_INPUT:-}" ]; then
                CLEAN_INPUT=$(echo "$USER_INPUT" | tr -d '"`\n\r')
                echo "${KEY_NAME}=\"${CLEAN_INPUT}\"" >> "$ENV_FILE"
                ok "${KEY_NAME} saved."
            else
                warn "Skipped ${KEY_NAME}. Set it in .env or run ./run.sh setup"
            fi
        else
            warn "Missing: ${KEY_NAME} — set it in .env or run ./run.sh setup"
        fi
    fi
done

# Optional keys — write defaults silently
for entry in "${OPTIONAL_KEYS[@]}"; do
    KEY_NAME="${entry%%|*}"
    KEY_DEFAULT="${entry##*|}"
    if ! grep -q "^${KEY_NAME}=" "$ENV_FILE" 2>/dev/null; then
        echo "${KEY_NAME}=\"${KEY_DEFAULT}\"" >> "$ENV_FILE"
    fi
done

# Auto-generate Gitea admin password if blank
GITEA_PASS=$(grep "^GITEA_ADMIN_PASSWORD=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"')
if [ -z "$GITEA_PASS" ]; then
    GENERATED_PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 24)
    sed -i.bak "s|^GITEA_ADMIN_PASSWORD=.*|GITEA_ADMIN_PASSWORD=\"${GENERATED_PASS}\"|" "$ENV_FILE"
    rm -f "${ENV_FILE}.bak"
    ok "Generated Gitea admin password."
fi

# ── Step 6: .gitignore (append-only, idempotent) ──────────────────────────
GITIGNORE_ENTRIES=(
    ".env" ".venv/" "gitea-data/" "bin/" ".bin/" "run/"
    "downloads/" "*.log" "__pycache__/" ".pytest_cache/"
)
touch .gitignore
for entry in "${GITIGNORE_ENTRIES[@]}"; do
    grep -qxF "$entry" .gitignore 2>/dev/null || echo "$entry" >> .gitignore
done

# ── Step 7: Directory Structure ────────────────────────────────────────────
mkdir -p "$SRC_DIR" "$GITEA_DATA_DIR" "$RUN_DIR" "tests" "downloads"

# ── Step 8: Execution Routing ──────────────────────────────────────────────
ok "Bootstrap complete."

clean_stale_pids

ensure_gitea_initialized() {
    # Generate app.ini if it doesn't exist (first-run provisioning)
    local ini_path="$GITEA_DATA_DIR/app.ini"
    if [ ! -f "$ini_path" ]; then
        info "First-run: generating Gitea configuration..."
        mkdir -p "$GITEA_DATA_DIR/repositories" "$GITEA_DATA_DIR/lfs" "$GITEA_DATA_DIR/log"

        GITEA_PORT=$(grep '^GITEA_PORT=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
        GITEA_PORT="${GITEA_PORT:-3000}"
        local base_url="http://localhost:${GITEA_PORT}"
        local data_abs
        data_abs="$(cd "$GITEA_DATA_DIR" && pwd)"

        # Generate LFS JWT secret
        local lfs_jwt_secret
        lfs_jwt_secret=$("$BIN_DIR/gitea" generate secret JWT_SECRET 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9_-' | head -c 43)

        cat > "$ini_path" <<EOINI
[database]
DB_TYPE  = sqlite3
PATH     = ${data_abs}/gitea.db

[server]
HTTP_PORT        = ${GITEA_PORT}
ROOT_URL         = ${base_url}
LFS_START_SERVER = true
LFS_JWT_SECRET   = ${lfs_jwt_secret}

[lfs]
PATH = ${data_abs}/lfs

[repository]
ROOT = ${data_abs}/repositories

[security]
INSTALL_LOCK = true

[service]
DISABLE_REGISTRATION = true

[log]
ROOT_PATH = ${data_abs}/log
MODE      = file
LEVEL     = Warn
EOINI
        ok "Generated $ini_path"
    fi

}

create_gitea_admin() {
    # Create admin user after Gitea is running (requires DB to be initialized)
    local admin_marker="$GITEA_DATA_DIR/.admin_created"
    if [ -f "$admin_marker" ]; then
        return
    fi

    local admin_user admin_pass
    admin_user=$(grep '^GITEA_ADMIN_USER=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
    admin_user="${admin_user:-hfmirror}"
    admin_pass=$(grep '^GITEA_ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
    if [ -n "$admin_pass" ]; then
        info "Creating Gitea admin user '${admin_user}'..."
        GITEA_WORK_DIR="$(pwd)/$GITEA_DATA_DIR" "$BIN_DIR/gitea" admin user create \
            --username "$admin_user" \
            --password "$admin_pass" \
            --email "admin@localhost" \
            --admin \
            --config "$(pwd)/$GITEA_DATA_DIR/app.ini" 2>&1 || true

        # Generate API token for the admin user
        GITEA_PORT=$(grep '^GITEA_PORT=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
        GITEA_PORT="${GITEA_PORT:-3000}"
        info "Generating Gitea API token..."
        TOKEN_RESPONSE=$(curl -sf -X POST \
            "http://localhost:${GITEA_PORT}/api/v1/users/${admin_user}/tokens" \
            -u "${admin_user}:${admin_pass}" \
            -H "Content-Type: application/json" \
            -d '{"name":"hfmirror-auto","scopes":["all"]}' 2>&1) || true

        if [ -n "$TOKEN_RESPONSE" ]; then
            API_TOKEN=$(echo "$TOKEN_RESPONSE" | grep -o '"sha1":"[^"]*"' | cut -d'"' -f4)
            if [ -n "$API_TOKEN" ]; then
                # Save token to .env
                if grep -q '^GITEA_API_TOKEN=' "$ENV_FILE" 2>/dev/null; then
                    sed -i.bak "s|^GITEA_API_TOKEN=.*|GITEA_API_TOKEN=\"${API_TOKEN}\"|" "$ENV_FILE"
                    rm -f "${ENV_FILE}.bak"
                else
                    echo "GITEA_API_TOKEN=\"${API_TOKEN}\"" >> "$ENV_FILE"
                fi
                ok "Gitea API token saved to .env"
            fi
        fi

        touch "$admin_marker"
        ok "Gitea admin user ready."
    else
        warn "No GITEA_ADMIN_PASSWORD set — skipping admin user creation."
    fi
}

start_gitea() {
    if [ -f "$PID_GITEA" ]; then
        info "Gitea already running (PID $(< "$PID_GITEA"))."
        return
    fi

    GITEA_PORT=$(grep '^GITEA_PORT=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
    GITEA_PORT="${GITEA_PORT:-3000}"

    info "Starting Gitea on port ${GITEA_PORT}..."
    GITEA_WORK_DIR="$(pwd)/$GITEA_DATA_DIR" "$BIN_DIR/gitea" web \
        --config "$(pwd)/$GITEA_DATA_DIR/app.ini" \
        --custom-path "$(pwd)/$GITEA_DATA_DIR" \
        &>/dev/null &
    echo $! > "$PID_GITEA"

    # Wait for Gitea to be ready
    local attempts=0
    local max_attempts=30
    while [ $attempts -lt $max_attempts ]; do
        if curl -sf "http://localhost:${GITEA_PORT}/api/v1/version" &>/dev/null; then
            ok "Gitea is ready."
            create_gitea_admin
            return
        fi
        ((attempts = attempts + 1))
        sleep 1
    done
    err "Gitea failed to start within ${max_attempts}s."
    exit 1
}

# --- Interactive Menu ---
show_menu() {
    while true; do
        echo ""
        echo "╔══════════════════════════════════════════╗"
        echo "║        HFMirror — Main Menu              ║"
        echo "╠══════════════════════════════════════════╣"
        echo "║  1)  Web UI        Start Web UI + Gitea  ║"
        echo "║  2)  CLI           Enter CLI command      ║"
        echo "║  3)  Setup         First-run wizard       ║"
        echo "║  4)  Doctor        System health checks   ║"
        echo "║  5)  Test          Run test suite          ║"
        echo "║  6)  Stop          Stop services           ║"
        echo "║  7)  Help          Show CLI usage          ║"
        echo "║  0)  Exit                                  ║"
        echo "╚══════════════════════════════════════════╝"
        echo ""
        printf "Select [0-7]: "
        read -r choice || true
        echo ""
        case "${choice:-}" in
            1) run_web_fg; continue ;;
            2)
                echo "Enter CLI command (e.g. clone org/model, list, status, diff org/model):"
                printf "> "
                read -r cli_args || true
                if [ -n "${cli_args:-}" ]; then
                    # shellcheck disable=SC2086
                    run_cli_fg ${cli_args:-}
                else
                    warn "No command entered."
                fi
                echo ""
                echo "Press Enter to return to menu..."
                read -r _ || true
                continue
                ;;
            3) run_setup_fg
                echo ""
                echo "Press Enter to return to menu..."
                read -r _ || true
                continue
                ;;
            4) run_doctor_fg
                echo ""
                echo "Press Enter to return to menu..."
                read -r _ || true
                continue
                ;;
            5) run_test_fg
                echo ""
                echo "Press Enter to return to menu..."
                read -r _ || true
                continue
                ;;
            6) do_stop ;;
            7) show_help
                echo ""
                echo "Press Enter to return to menu..."
                read -r _ || true
                continue
                ;;
            0) echo "Bye."; exit 0 ;;
            *) warn "Invalid choice: ${choice:-}" ;;
        esac
    done
}

show_help() {
    echo ""
    echo "Usage: ./run.sh [command] [args...]"
    echo ""
    echo "Commands:"
    echo "  web              Start Web UI + Gitea"
    echo "  cli [args]       Run CLI commands"
    echo "  setup            Interactive first-run wizard"
    echo "  doctor           System health checks"
    echo "  test [args]      Run test suite"
    echo "  stop             Stop backgrounded services"
    echo "  help             Show this help"
    echo ""
    echo "If no command is given, an interactive menu is shown."
    echo ""
    echo "Examples:"
    echo "  ./run.sh web"
    echo "  ./run.sh cli clone meta-llama/Llama-3.1-70B"
    echo "  ./run.sh cli list"
    echo "  ./run.sh cli diff meta-llama/Llama-3.1-70B"
    echo "  ./run.sh cli update --all"
    echo "  ./run.sh cli prune org/model --dry-run"
    echo "  ./run.sh cli migrate org/model --to tier2"
    echo "  ./run.sh test --coverage"
}

# --- Foreground runners (return to caller, used by menu) ---
run_web_fg() {
    ensure_gitea_initialized
    start_gitea
    info "Starting Web UI (Ctrl+C to stop and return to menu)..."
    "$VENV_DIR/bin/python" main.py web || true
}

run_cli_fg() {
    # Start Gitea for commands that need it
    case "${1:-}" in
        clone|update|diff|push|doctor|open)
            ensure_gitea_initialized
            start_gitea
            ;;
    esac
    "$VENV_DIR/bin/python" main.py cli "$@" || true
}

run_setup_fg() {
    info "Re-running setup wizard..."
    for entry in "${REQUIRED_KEYS[@]}"; do
        [[ "$entry" == \#* ]] && continue
        KEY_NAME="${entry%%|*}"
        KEY_DESC="${entry##*|}"
        warn "${KEY_NAME} — ${KEY_DESC}"
        CURRENT=$(grep "^${KEY_NAME}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')
        if [ -n "$CURRENT" ]; then
            MASKED="${CURRENT:0:4}****"
            info "Current: $MASKED"
        fi
        read -r -p "     New value (Enter to keep): " USER_INPUT || true
        if [ -n "${USER_INPUT:-}" ]; then
            CLEAN_INPUT=$(echo "$USER_INPUT" | tr -d '"`\n\r')
            grep -v "^${KEY_NAME}=" "$ENV_FILE" > "${ENV_FILE}.tmp" || true
            mv "${ENV_FILE}.tmp" "$ENV_FILE"
            echo "${KEY_NAME}=\"${CLEAN_INPUT}\"" >> "$ENV_FILE"
            ok "${KEY_NAME} updated."
        fi
    done
    ok "Setup complete."
}

run_doctor_fg() {
    ensure_gitea_initialized
    start_gitea
    "$VENV_DIR/bin/python" main.py cli doctor || true
}

run_test_fg() {
    info "Running tests..."
    "$VENV_DIR/bin/python" main.py test || true
}

# --- exec runners (used by direct CLI dispatch, replaces shell) ---
do_web() {
    ensure_gitea_initialized
    start_gitea
    info "Starting Web UI..."
    exec "$VENV_DIR/bin/python" main.py web
}

do_cli() {
    case "${1:-}" in
        clone|update|diff|push|doctor|open)
            ensure_gitea_initialized
            start_gitea
            ;;
    esac
    exec "$VENV_DIR/bin/python" main.py cli "$@"
}

do_setup() {
    run_setup_fg
}

do_doctor() {
    ensure_gitea_initialized
    start_gitea
    exec "$VENV_DIR/bin/python" main.py cli doctor
}

do_test() {
    info "Running tests..."
    exec "$VENV_DIR/bin/python" main.py test
}

do_stop() {
    info "Stopping services..."
    # cleanup() runs via trap
    exit 0
}

# --- Main Dispatch ---
MODE="${1:-}"

if [ -z "$MODE" ]; then
    show_menu
    exit 0
fi

shift || true

case "$MODE" in
    web)                do_web ;;
    cli)                do_cli "$@" ;;
    setup)              do_setup ;;
    doctor)             do_doctor ;;
    test)               do_test "$@" ;;
    stop)               do_stop ;;
    help|--help|-h)     show_help ;;
    1)                  do_web ;;
    2)                  do_cli "$@" ;;
    3)                  do_setup ;;
    4)                  do_doctor ;;
    5)                  do_test "$@" ;;
    6)                  do_stop ;;
    7)                  show_help ;;
    *)
        err "Unknown command: $MODE"
        info "Run './run.sh help' or './run.sh' for interactive menu."
        exit 1
        ;;
esac
