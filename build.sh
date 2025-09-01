#!/bin/bash

set -e

PACKAGE_NAME=$1

if [ -z "$PACKAGE_NAME" ]; then
    echo "Usage: $0 <package-name>"
    exit 1
fi

# Create a directory to store the built packages
mkdir -p packages

# Build the Docker image
docker build -t aur-builder .

# Run the Docker container
docker run --rm -v "$(pwd)/packages:/packages" aur-builder "$PACKAGE_NAME"

# Run a container to create/update the package repository
docker run --rm -v "$(pwd)/packages:/packages" -w /packages aur-builder repo-add aurdist.db.tar.gz *.pkg.tar.zst

echo "Package built and repository updated in the 'packages' directory."
