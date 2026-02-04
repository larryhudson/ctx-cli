#!/usr/bin/env bash
# sandbox.sh - Create and run ctx in a Debian Bookworm container
#
# Usage:
#   ./scripts/sandbox.sh build     # Build the container image
#   ./scripts/sandbox.sh start     # Start the container (runs in background)
#   ./scripts/sandbox.sh search    # Run a search (pass args after)
#   ./scripts/sandbox.sh info      # Show database info
#   ./scripts/sandbox.sh stop      # Stop and remove the container
#
# The container mounts your local chroma_data for searching.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
IMAGE_NAME="ctx-sandbox"
CONTAINER_NAME="ctx-sandbox"

# Default chroma data location (can be overridden with CTX_CHROMA_DATA)
CHROMA_DATA="${CTX_CHROMA_DATA:-$HOME/.local/share/ctx/chroma_data}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_docker() {
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed. Please install Docker first."
        exit 1
    fi

    if ! docker info &> /dev/null; then
        log_error "Docker daemon is not running. Please start Docker."
        exit 1
    fi
}

check_chroma_data() {
    if [[ ! -d "$CHROMA_DATA" ]]; then
        log_error "ChromaDB data directory not found: $CHROMA_DATA"
        log_error "Run 'ctx ingest' first to create the database, or set CTX_CHROMA_DATA to point to your data."
        exit 1
    fi
}

is_container_running() {
    docker ps --filter "name=^${CONTAINER_NAME}$" --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"
}

container_exists() {
    docker ps -a --filter "name=^${CONTAINER_NAME}$" --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"
}

ensure_container_running() {
    if ! is_container_running; then
        if container_exists; then
            log_info "Starting existing container..."
            docker start "$CONTAINER_NAME" > /dev/null
        else
            log_error "Container not running. Run '$0 start' first."
            exit 1
        fi
    fi
}

build_image() {
    log_info "Building Docker image: $IMAGE_NAME"

    # Create a temporary Dockerfile
    local dockerfile=$(mktemp)
    cat > "$dockerfile" << 'DOCKERFILE'
FROM debian:bookworm-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Add uv to PATH
ENV PATH="/root/.local/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ src/

# Install Python 3.12 and project dependencies
RUN uv python install 3.12 && uv sync --no-dev --python 3.12

# Create data directories
RUN mkdir -p /root/.local/share/ctx /root/.config/ctx

# Set the chroma data path via environment variable
ENV CTX_DATABASE__PATH=/root/.local/share/ctx/chroma_data

# Keep container running
CMD ["sleep", "infinity"]
DOCKERFILE

    # Build the image
    docker build -t "$IMAGE_NAME" -f "$dockerfile" "$PROJECT_DIR"

    rm "$dockerfile"
    log_info "Image built successfully"
}

start_container() {
    check_chroma_data

    if is_container_running; then
        log_info "Container already running"
        return
    fi

    if container_exists; then
        log_info "Starting existing container..."
        docker start "$CONTAINER_NAME" > /dev/null
    else
        log_info "Creating and starting container..."
        docker run -d \
            --name "$CONTAINER_NAME" \
            -v "$CHROMA_DATA:/root/.local/share/ctx/chroma_data" \
            "$IMAGE_NAME" > /dev/null
    fi

    log_info "Container started. ChromaDB data from: $CHROMA_DATA"
}

stop_container() {
    if is_container_running; then
        log_info "Stopping container..."
        docker stop "$CONTAINER_NAME" > /dev/null
    fi

    if container_exists; then
        log_info "Removing container..."
        docker rm "$CONTAINER_NAME" > /dev/null
    fi

    log_info "Container stopped and removed"
}

run_in_container() {
    ensure_container_running

    # Use -it only if we have a TTY
    local docker_flags=""
    if [[ -t 0 ]]; then
        docker_flags="-it"
    fi

    docker exec $docker_flags "$CONTAINER_NAME" "$@"
}

run_shell() {
    ensure_container_running
    log_info "Opening shell in container..."
    docker exec -it "$CONTAINER_NAME" bash
}

run_search() {
    if [[ $# -eq 0 ]]; then
        log_error "No search query provided"
        echo "Usage: $0 search <query> [options]"
        exit 1
    fi

    run_in_container uv run ctx search "$@"
}

run_info() {
    run_in_container uv run ctx info
}

run_get() {
    if [[ $# -eq 0 ]]; then
        log_error "No document ID provided"
        echo "Usage: $0 get <document_id>"
        exit 1
    fi

    run_in_container uv run ctx get "$@"
}

show_status() {
    if is_container_running; then
        log_info "Container is running"
        echo "  ChromaDB data: $CHROMA_DATA"
    elif container_exists; then
        log_warn "Container exists but is stopped. Run '$0 start' to start it."
    else
        log_warn "Container does not exist. Run '$0 start' to create it."
    fi
}

show_usage() {
    cat << EOF
ctx sandbox - Run ctx CLI in a Debian Bookworm container

Usage: $0 <command> [args]

Commands:
    build           Build the Docker image
    start           Start the container (creates if needed)
    stop            Stop and remove the container
    status          Show container status
    shell           Open a bash shell in the container
    search <query>  Search the database
    info            Show database statistics
    get <id>        Get a document by ID
    help            Show this help message

Environment:
    CTX_CHROMA_DATA    Path to ChromaDB data directory
                       (default: ~/.local/share/ctx/chroma_data)

Examples:
    $0 build                                    # Build the image
    $0 start                                    # Start the container
    $0 search "deployment issues"               # Search
    $0 search "auth bug" --source linear        # Search with filter
    $0 info                                     # Show stats
    $0 stop                                     # Stop when done
EOF
}

# Main entry point
main() {
    check_docker

    local cmd="${1:-help}"
    shift || true

    case "$cmd" in
        build)
            build_image
            ;;
        start)
            start_container
            ;;
        stop)
            stop_container
            ;;
        status)
            show_status
            ;;
        shell)
            run_shell
            ;;
        search)
            run_search "$@"
            ;;
        info)
            run_info
            ;;
        get)
            run_get "$@"
            ;;
        help|--help|-h)
            show_usage
            ;;
        *)
            log_error "Unknown command: $cmd"
            show_usage
            exit 1
            ;;
    esac
}

main "$@"
