#!/usr/bin/env bash
for pkg in $(cat targets.txt); do 
    [[ -z $(ls packages/$pkg* 2>/dev/null) ]] && echo "build $pkg" && ./build.sh $pkg
done
