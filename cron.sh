#!/bin/bash

set -e

if [ ! -f "targets.txt" ]; then
    echo "targets.txt not found."
    exit 1
fi

while IFS= read -r package; do
    if [ -n "$package" ]; then
        ./build.sh "$package"
    fi
done < "targets.txt"
