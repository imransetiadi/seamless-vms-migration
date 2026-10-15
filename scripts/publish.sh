#!/usr/bin/env bash
# Modified by the Seamless Migrate project, 2026.

# Exit immediately if a command exits with a non-zero status, if a variable is
# unset, and if a command in a pipeline fails.
set -euo pipefail

# --- Functions ---

# Print an error message and exit.
# @param message The error message to print.
error() {
    echo "Error: $1" >&2
    exit 1
}

# Check if a command exists.
# @param cmd The command to check.
check_command() {
    command -v "$1" >/dev/null 2>&1 || error "$1 is not installed. Please install it to continue."
}

# --- Initial Setup and Dependencies Check ---

check_command "ansible-galaxy"
check_command "curl"
check_command "yq" # Using yq as a more standard and powerful YAML processor

# --- Argument Parsing ---

GALAXY_API_KEY="${ANSIBLE_GALAXY_API_KEY:-}" # Use environment variable if set

while (( "$#" )); do
    case "$1" in
        -k|--galaxy-key)
            if [[ -z "${2:-}" ]]; then
                error "A value is required for the --galaxy-key option."
            fi
            echo "Warning: -k puts the Galaxy API key on the command line, where ps and the shell" \
                "history show it. Next time, export ANSIBLE_GALAXY_API_KEY instead:" \
                "read -rs ANSIBLE_GALAXY_API_KEY && export ANSIBLE_GALAXY_API_KEY" >&2
            GALAXY_API_KEY="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $(basename "$0") [-k <galaxy_key>]"
            echo "Build the collection and publish it to Galaxy if the version is new."
            echo "Give the Galaxy API key in the ANSIBLE_GALAXY_API_KEY environment variable"
            echo "(read -rs ANSIBLE_GALAXY_API_KEY && export ANSIBLE_GALAXY_API_KEY): -k puts it on"
            echo "the command line, where ps and the shell history show it."
            exit 0
            ;;
        *) # preserve positional arguments
            # If you need to handle other positional arguments, they can be added to an array
            # For this script's purpose, we'll treat them as an error.
            error "Unsupported argument: $1. Use --help for usage information."
            ;;
    esac
done

if [[ -z "$GALAXY_API_KEY" ]]; then
    error "Ansible Galaxy API key not provided. Use the -k flag or the ANSIBLE_GALAXY_API_KEY environment variable."
fi

# --- Main Logic ---

GALAXY_YML="galaxy.yml"

if [[ ! -f "$GALAXY_YML" ]]; then
    error "The '$GALAXY_YML' file was not found in the current directory."
fi

# Read metadata from galaxy.yml
# Note: For yq v4+, the syntax is as shown. For older versions, it might be different.
NAMESPACE=$(yq e '.namespace' "$GALAXY_YML")
NAME=$(yq e '.name' "$GALAXY_YML")
CURRENT_VERSION=$(yq e '.version' "$GALAXY_YML")

echo "--- Collection Details ---"
echo "Namespace: $NAMESPACE"
echo "Name:      $NAME"
echo "Version:   $CURRENT_VERSION"
echo "--------------------------"

# Check if the current version is already published
echo "Checking if version $CURRENT_VERSION is already published on Ansible Galaxy..."

# The API returns a 404 status code if the collection or version doesn't exist.
# We check the HTTP status code to see if the version is published.
http_status=$(curl -s -o /dev/null -w "%{http_code}" "https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/index/$NAMESPACE/$NAME/versions/$CURRENT_VERSION/")

if [[ "$http_status" -eq 200 ]]; then
    echo "Version $CURRENT_VERSION is already published."
    echo "To publish a new version, please update '$GALAXY_YML' and run this script again."
    exit 0
elif [[ "$http_status" -eq 404 ]]; then
    echo "This version is not yet published. Proceeding with publishing..."

    COLLECTION_TARBALL="${NAMESPACE}-${NAME}-${CURRENT_VERSION}.tar.gz"

    # Build the tarball of this checkout even when one is there: the documented release builds
    # before its pull request and publishes after checking out the merge commit, and a tarball
    # built before galaxy.yml mirrored the .gitignore files may hold local secrets
    # (Security.md R-26).
    rm -f "$COLLECTION_TARBALL"
    make build
    [[ -f "$COLLECTION_TARBALL" ]] || error "make build did not write $COLLECTION_TARBALL."

    echo "Publishing $COLLECTION_TARBALL to Ansible Galaxy..."
    # The key reaches ansible-galaxy through its environment, as the token of a Galaxy server the
    # environment defines, never through its command line, which ps shows every user of the host.
    ANSIBLE_GALAXY_SERVER_LIST=release_galaxy \
        ANSIBLE_GALAXY_SERVER_RELEASE_GALAXY_URL=https://galaxy.ansible.com/ \
        ANSIBLE_GALAXY_SERVER_RELEASE_GALAXY_TOKEN="$GALAXY_API_KEY" \
        ansible-galaxy collection publish "$COLLECTION_TARBALL"
    echo "Successfully published version $CURRENT_VERSION!"
else
    error "Failed to check collection version on Ansible Galaxy. Received HTTP status: $http_status"
fi
