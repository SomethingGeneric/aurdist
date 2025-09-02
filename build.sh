#!/bin/bash

set -e

# Create a directory to store the built packages
mkdir -p packages

# Build the Docker image
docker build -t aur-builder .

# Function to build a single package
build_package() {
    local package_name=$1
    echo "Building package: $package_name"
    # Run the Docker container
    docker run --rm -v "$(pwd)/packages:/packages" aur-builder "$package_name"
}

if [ -z "$1" ]; then
    if [ -f "targets.txt" ]; then
        echo "No package name provided, building all packages from targets.txt"
        while IFS= read -r package; do
            if [ -n "$package" ]; then
                build_package "$package"
            fi
        done < "targets.txt"
    else
        echo "Usage: $0 <package-name>"
        echo "Or create a 'targets.txt' file with a list of packages to build."
        exit 1
    fi
else
    build_package "$1"
fi

## TODO: i suppose some folks might not have their packages repo on an Arch box
# Run a container to create/update the package repository
#docker run --rm -v "$(pwd)/packages:/packages" -w /packages aur-builder repo-add aurdist.db.tar.zst *.pkg.tar.zst

pushd packages
repo-add -n aurdist.db.tar.zst *.pkg.tar.zst
popd

echo "Packages built and repository updated in the 'packages' directory."

[[ -f .where ]] && sudo rsync -avc packages/ $(cat .where) && echo "Rsync'd to nginx directory"