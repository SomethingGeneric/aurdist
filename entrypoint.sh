#!/bin/bash

set -e

# Clone the AUR repository
git clone "https://aur.archlinux.org/$1.git"

cd "$1"

# Build the package
makepkg -sf --noconfirm

# Copy the built package to the shared volume
cp *.pkg.tar.zst /packages/
