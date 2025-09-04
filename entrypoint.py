#!/usr/bin/env python3

import subprocess
import sys
import os
import re
import json
import requests
import shutil
from pathlib import Path

# AUR RPC API endpoint
# Documentation: https://wiki.archlinux.org/title/AUR_web_interface#RPC_interface
# 
# Available API endpoints:
# - info: Get package information (what we use)
# - search: Search for packages
# - msearch: Multi-criteria search
# - rpc: Raw RPC calls
#
# Example usage:
# - Single package: https://aur.archlinux.org/rpc/?v=5&type=info&arg[]=package-name
# - Multiple packages: https://aur.archlinux.org/rpc/?v=5&type=info&arg[]=pkg1&arg[]=pkg2
# - Search: https://aur.archlinux.org/rpc/?v=5&type=search&arg=search-term
AUR_RPC_URL = "https://aur.archlinux.org/rpc/?v=5&type=info&arg[]="

def run_command(command, check=True, capture_output=True):
    """Run a shell command and return the output."""
    if capture_output:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True)
        if check and result.returncode != 0:
            print(f"Error running command '{command}': {result.stderr}")
            sys.exit(result.returncode)
        return result.stdout.strip(), result.stderr.strip()
    else:
        result = subprocess.run(command, shell=True)
        if check and result.returncode != 0:
            print(f"Error running command '{command}'")
            sys.exit(result.returncode)
        return "", ""

def is_package_in_official_repos(package_name):
    """Check if a package is in the official repositories using pacman."""
    stdout, stderr = run_command(f"pacman -Si {package_name}", check=False)
    return stdout and "Repository" in stdout

def is_package_in_aur(package_name):
    """Check if a package exists in the AUR using the RPC interface."""
    try:
        response = requests.get(f"{AUR_RPC_URL}{package_name}", timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get("resultcount", 0) > 0
    except requests.RequestException as e:
        print(f"Error checking AUR for {package_name}: {e}")
    return False

def get_aur_package_info(package_name):
    """Get detailed information about an AUR package."""
    try:
        response = requests.get(f"{AUR_RPC_URL}{package_name}", timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("resultcount", 0) > 0:
                return data["results"][0]
    except requests.RequestException as e:
        print(f"Error getting AUR info for {package_name}: {e}")
    return None

def get_package_dependencies_from_aur(package_name):
    """Get dependencies directly from AUR package info."""
    package_info = get_aur_package_info(package_name)
    if package_info:
        dependencies = {
            'depends': package_info.get('Depends', []),
            'makedepends': package_info.get('MakeDepends', []),
            'checkdepends': package_info.get('CheckDepends', []),
            'optdepends': package_info.get('OptDepends', [])
        }
        return dependencies
    return None

def analyze_dependency_status(dependencies):
    """Analyze dependencies and categorize them by availability."""
    analysis = {
        'official_repos': [],
        'aur_packages': [],
        'not_found': [],
        'total_count': 0
    }
    
    all_deps = []
    for dep_type, dep_list in dependencies.items():
        if dep_type != 'optdepends':  # Skip optional dependencies
            all_deps.extend(dep_list)
    
    analysis['total_count'] = len(all_deps)
    
    for dep in all_deps:
        dep = dep.strip()
        if not dep:
            continue
            
        if is_package_in_official_repos(dep):
            analysis['official_repos'].append(dep)
        elif is_package_in_aur(dep):
            analysis['aur_packages'].append(dep)
        else:
            analysis['not_found'].append(dep)
    
    return analysis

def parse_pkgbuild_dependencies(pkgbuild_path):
    """Parse PKGBUILD file to extract dependencies."""
    dependencies = {
        'depends': [],
        'makedepends': [],
        'checkdepends': [],
        'optdepends': []
    }
    
    if not os.path.exists(pkgbuild_path):
        return dependencies
    
    with open(pkgbuild_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Extract dependencies using regex
    for dep_type in dependencies.keys():
        pattern = rf"{dep_type}=\((.*?)\)"
        matches = re.findall(pattern, content, re.DOTALL)
        if matches:
            # Split by newlines and clean up
            deps = []
            for match in matches:
                lines = match.split('\n')
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # Remove quotes and split by spaces
                        deps.extend(re.findall(r"'([^']+)'|\"([^\"]+)\"|(\S+)", line))
            # Flatten the list and clean up
            dependencies[dep_type] = [dep[0] or dep[1] or dep[2] for dep in deps if any(dep)]
    
    return dependencies

def install_aur_package(package_name):
    """Install an AUR package by cloning and building it."""
    print(f"Installing AUR package: {package_name}")
    
    # Clone the AUR repository
    clone_cmd = f"git clone https://aur.archlinux.org/{package_name}.git"
    run_command(clone_cmd)
    
    # Change to package directory and build
    os.chdir(package_name)
    run_command("makepkg -si --noconfirm")
    
    # Go back to parent directory
    os.chdir("..")

def check_and_install_dependencies(package_name):
    """Check and install all dependencies for a package."""
    print(f"Checking dependencies for package: {package_name}")
    
    # Clone the package first to get PKGBUILD
    clone_cmd = f"git clone https://aur.archlinux.org/{package_name}.git"
    run_command(clone_cmd)
    
    os.chdir(package_name)
    pkgbuild_path = "PKGBUILD"
    
    if not os.path.exists(pkgbuild_path):
        print(f"PKGBUILD not found for {package_name}")
        sys.exit(1)
    
    # Parse dependencies from PKGBUILD
    deps = parse_pkgbuild_dependencies(pkgbuild_path)
    
    # Also try to get dependencies from AUR API for comparison
    aur_deps = get_package_dependencies_from_aur(package_name)
    
    print(f"PKGBUILD dependencies: {deps}")
    if aur_deps:
        print(f"AUR API dependencies: {aur_deps}")
    
    # Analyze dependency status
    analysis = analyze_dependency_status(deps)
    
    print(f"\nDependency Analysis for {package_name}:")
    print(f"  Total dependencies: {analysis['total_count']}")
    print(f"  Available in official repos: {len(analysis['official_repos'])}")
    print(f"  Available in AUR: {len(analysis['aur_packages'])}")
    print(f"  Not found: {len(analysis['not_found'])}")
    
    if analysis['official_repos']:
        print(f"  Official repo packages: {', '.join(analysis['official_repos'])}")
    
    if analysis['aur_packages']:
        print(f"  AUR packages: {', '.join(analysis['aur_packages'])}")
        print("  WARNING: This package depends on other AUR packages!")
        print("  These will need to be built and installed first.")
    
    if analysis['not_found']:
        print(f"  Missing packages: {', '.join(analysis['not_found'])}")
        print("  WARNING: Some dependencies could not be found!")
    
    # Install dependencies
    print(f"\nInstalling dependencies...")
    
    # Install from official repos first
    if analysis['official_repos']:
        print(f"Installing from official repos: {', '.join(analysis['official_repos'])}")
        run_command(f"sudo pacman -S --noconfirm {' '.join(analysis['official_repos'])}")
    
    # Install AUR packages
    for dep in analysis['aur_packages']:
        print(f"Installing AUR package: {dep}")
        os.chdir("..")  # Go back to parent directory
        install_aur_package(dep)
        os.chdir(package_name)  # Back to package directory

def main():
    if len(sys.argv) != 2:
        print("Usage: entrypoint.py <package-name>")
        sys.exit(1)
    
    package_name = sys.argv[1]
    print(f"Building AUR package: {package_name}")
    
    # Check and install dependencies
    check_and_install_dependencies(package_name)
    
    # Build the package
    print("Building the package...")
    run_command("makepkg -sf --noconfirm")
    
    # Copy the built package to the shared volume
    print("Copying built packages to /packages/")
    run_command("cp *.pkg.tar.zst /packages/")
    
    print(f"Successfully built {package_name}")

if __name__ == "__main__":
    main()
