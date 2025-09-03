#!/bin/bash

set -e

# --- Configuration ---
# Create a directory to store the built packages
mkdir -p packages

# --- Functions ---

# Function to get the latest version of a package from the AUR
get_aur_version() {
    local package_name=$1
    curl -s "https://aur.archlinux.org/rpc/?v=5&type=info&arg=${package_name}" | jq -r '.results[0].Version'
}

# Function to get the version of the locally built package
get_local_version() {
    local package_name=$1
    local pkg_file=$(find packages -name "${package_name}-*.pkg.tar.zst" -o -name "${package_name}-*-*.pkg.tar.zst" | head -n 1)
    if [ -n "$pkg_file" ]; then
        basename "$pkg_file" | sed -E "s/${package_name}(-[a-zA-Z0-9]+)?-(.+)-x86_64\.pkg\.tar\.zst/\2/"
    else
        echo "0"
    fi
}

# Function to build a single package
build_package() {
    local package_name=$1
    local force_build=$2

    echo "Building package: $package_name"

    local aur_version=$(get_aur_version "$package_name")
    local local_version=$(get_local_version "$package_name")

    echo "AUR version: $aur_version"
    echo "Local version: $local_version"

    if [ "$force_build" = "true" ] || [ "$aur_version" != "$local_version" ]; then
        echo "New version available or force build requested. Building..."
        # Run the Docker container
        docker run --rm -v "$(pwd)/packages:/packages" aur-builder "$package_name"
    else
        echo "Package is up to date. Skipping build."
    fi
}

# --- Main Script ---

FORCE_BUILD=false
while getopts ":f" opt; do
  case ${opt} in
    f )
      FORCE_BUILD=true
      ;;
    ? )
      echo "Invalid option: $OPTARG" 1>&2
      ;;
  esac
done
shift $((OPTIND -1))

# Build the Docker image
docker build -t aur-builder .

if [ -z "$1" ]; then
    if [ -f "targets.txt" ]; then
        echo "No package name provided, building all packages from targets.txt"
        while IFS= read -r package; do
            if [ -n "$package" ]; then
                build_package "$package" "$FORCE_BUILD"
            fi
        done < "targets.txt"
    else
        echo "Usage: $0 [-f] <package-name>"
        echo "Or create a 'targets.txt' file with a list of packages to build."
        exit 1
    fi
else
    build_package "$1" "$FORCE_BUILD"
fi

## TODO: i suppose some folks might not have their packages repo on an Arch box
# Run a container to create/update the package repository
#docker run --rm -v "$(pwd)/packages:/packages" -w /packages aur-builder repo-add aurdist.db.tar.zst *.pkg.tar.zst

pushd packages
repo-add -n aurdist.db.tar.zst *.pkg.tar.zst
popd

echo "Packages built and repository updated in the 'packages' directory."

[[ -f .where ]] && sudo rsync -avc packages/ $(cat .where) && echo "Rsync'd to nginx directory"
