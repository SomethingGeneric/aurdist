#!/usr/bin/env python3
"""
AUR Utility - A comprehensive tool for building and managing AUR packages.

This script can:
1. Build specific packages when given as arguments
2. Check all packages in packages/ directory and rebuild outdated ones when run with no arguments
3. Handle AUR dependencies automatically
4. Manage the pacman repository database
5. Sync packages to remote locations

Usage:
    python aurutil.py                    # Check and rebuild outdated packages
    python aurutil.py package-name       # Build specific package
    python aurutil.py -f package-name    # Force build specific package
    python aurutil.py --check-only       # Only check versions, don't build
"""

import subprocess
import sys
import os
import re
import json
import requests
import shutil
import argparse
import glob
from pathlib import Path
from datetime import datetime

# AUR RPC API endpoint
# Documentation: https://wiki.archlinux.org/title/AUR_web_interface#RPC_interface
AUR_RPC_URL = "https://aur.archlinux.org/rpc/?v=5&type=info&arg[]="

def run_command(command, check=True, capture_output=True, cwd=None):
    """Run a shell command and return the output."""
    if capture_output:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True, cwd=cwd)
        if check and result.returncode != 0:
            print(f"Error running command '{command}': {result.stderr}")
            if check:
                sys.exit(result.returncode)
        return result.stdout.strip(), result.stderr.strip()
    else:
        result = subprocess.run(command, shell=True, cwd=cwd)
        if check and result.returncode != 0:
            print(f"Error running command '{command}'")
            if check:
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

def get_aur_version(package_name):
    """Get the latest version of a package from the AUR."""
    package_info = get_aur_package_info(package_name)
    if package_info:
        return package_info.get('Version', '0')
    return '0'

def get_local_version(package_name):
    """Get the version of the locally built package."""
    packages_dir = Path("packages")
    if not packages_dir.exists():
        return '0'
    
    # Look for package files matching the pattern
    pattern = f"{package_name}-*.pkg.tar.zst"
    pkg_files = list(packages_dir.glob(pattern))
    
    if not pkg_files:
        return '0'
    
    # Get the most recent file
    pkg_file = max(pkg_files, key=os.path.getctime)
    
    # Extract version from filename
    # Pattern: package-name-version-release-arch.pkg.tar.zst
    match = re.match(rf"{re.escape(package_name)}(?:-[a-zA-Z0-9]+)?-(.+)-x86_64\.pkg\.tar\.zst", pkg_file.name)
    if match:
        return match.group(1)
    
    return '0'

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
    
    # Analyze dependency status
    analysis = analyze_dependency_status(deps)
    
    print(f"\nDependency Analysis for {package_name}:")
    print(f"  Total dependencies: {analysis['total_count']}")
    print(f"  Available in official repos: {len(analysis['official_repos'])}")
    print(f"  Available in AUR: {len(analysis['aur_packages'])}")
    print(f"  Not found: {len(analysis['not_found'])}")
    
    if analysis['aur_packages']:
        print(f"  AUR packages: {', '.join(analysis['aur_packages'])}")
        print("  WARNING: This package depends on other AUR packages!")
    
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

def build_package_in_docker(package_name):
    """Build a package using Docker container."""
    print(f"Building package in Docker: {package_name}")
    
    # Ensure packages directory exists
    os.makedirs("packages", exist_ok=True)
    
    # Build the Docker image
    print("Building Docker image...")
    run_command("docker build -t aur-builder .")
    
    # Run the Docker container
    docker_cmd = f"docker run --rm -v {os.path.abspath('packages')}:/packages aur-builder {package_name}"
    run_command(docker_cmd)

def build_package_native(package_name):
    """Build a package natively (without Docker)."""
    print(f"Building package natively: {package_name}")
    
    # Ensure packages directory exists
    os.makedirs("packages", exist_ok=True)
    
    # Check and install dependencies
    check_and_install_dependencies(package_name)
    
    # Build the package
    print("Building the package...")
    run_command("makepkg -sf --noconfirm")
    
    # Copy the built package to the packages directory
    print("Copying built packages to packages/")
    run_command("cp *.pkg.tar.zst ../packages/")
    
    # Go back to parent directory
    os.chdir("..")

def update_repository():
    """Update the pacman repository database."""
    packages_dir = Path("packages")
    if not packages_dir.exists():
        print("No packages directory found")
        return
    
    print("Updating repository database...")
    os.chdir("packages")
    
    # Find all package files
    pkg_files = list(Path(".").glob("*.pkg.tar.zst"))
    
    if not pkg_files:
        print("No package files found")
        os.chdir("..")
        return
    
    # Update the repository database
    run_command("repo-add -n aurdist.db.tar.zst *.pkg.tar.zst")
    
    os.chdir("..")
    print("Repository database updated")

def sync_packages():
    """Sync packages to remote location if .where file exists."""
    where_file = Path(".where")
    if where_file.exists():
        with open(where_file, 'r') as f:
            remote_path = f.read().strip()
        
        if remote_path:
            print(f"Syncing packages to {remote_path}")
            run_command(f"sudo rsync -avc packages/ {remote_path}")
            print("Packages synced successfully")

def get_packages_from_targets():
    """Get list of packages from targets.txt file."""
    targets_file = Path("targets.txt")
    if not targets_file.exists():
        return []
    
    packages = []
    with open(targets_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                packages.append(line)
    
    return packages

def get_existing_packages():
    """Get list of packages that already exist in packages/ directory."""
    packages_dir = Path("packages")
    if not packages_dir.exists():
        return []
    
    packages = set()
    for pkg_file in packages_dir.glob("*.pkg.tar.zst"):
        # Extract package name from filename
        # Pattern: package-name-version-release-arch.pkg.tar.zst
        match = re.match(r"([^-]+(?:-[^-]+)*)-[^-]+-[^-]+-x86_64\.pkg\.tar\.zst", pkg_file.name)
        if match:
            packages.add(match.group(1))
    
    return list(packages)

def check_package_outdated(package_name):
    """Check if a package is outdated compared to AUR."""
    aur_version = get_aur_version(package_name)
    local_version = get_local_version(package_name)
    
    if local_version == '0':
        return True, f"Package not found locally (AUR: {aur_version})"
    
    if aur_version == '0':
        return False, f"Package not found in AUR (Local: {local_version})"
    
    # Simple version comparison (this could be improved with proper semver parsing)
    if aur_version != local_version:
        return True, f"Outdated (Local: {local_version}, AUR: {aur_version})"
    
    return False, f"Up to date (Version: {local_version})"

def main():
    parser = argparse.ArgumentParser(description='AUR Utility - Build and manage AUR packages')
    parser.add_argument('package', nargs='?', help='Package name to build')
    parser.add_argument('-f', '--force', action='store_true', help='Force build even if up to date')
    parser.add_argument('--check-only', action='store_true', help='Only check versions, don\'t build')
    parser.add_argument('--native', action='store_true', help='Build natively instead of using Docker')
    
    args = parser.parse_args()
    
    print(f"AUR Utility - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    
    if args.package:
        # Build specific package
        package_name = args.package
        print(f"Building package: {package_name}")
        
        if not args.check_only:
            if args.native:
                build_package_native(package_name)
            else:
                build_package_in_docker(package_name)
            
            # Update repository and sync
            update_repository()
            sync_packages()
        else:
            # Just check version
            is_outdated, status = check_package_outdated(package_name)
            print(f"Package {package_name}: {status}")
    
    else:
        # Check all packages and rebuild outdated ones
        print("Checking all packages for updates...")
        
        # Get packages from targets.txt or existing packages
        target_packages = get_packages_from_targets()
        if not target_packages:
            target_packages = get_existing_packages()
        
        if not target_packages:
            print("No packages found in targets.txt or packages/ directory")
            print("Usage: python aurutil.py <package-name>")
            print("Or create a 'targets.txt' file with package names")
            sys.exit(1)
        
        print(f"Found {len(target_packages)} packages to check")
        
        packages_to_build = []
        
        for package in target_packages:
            print(f"\nChecking {package}...")
            is_outdated, status = check_package_outdated(package)
            print(f"  {status}")
            
            if is_outdated or args.force:
                packages_to_build.append(package)
        
        if packages_to_build:
            print(f"\nBuilding {len(packages_to_build)} outdated packages...")
            for package in packages_to_build:
                print(f"\n{'='*20} Building {package} {'='*20}")
                if args.native:
                    build_package_native(package)
                else:
                    build_package_in_docker(package)
            
            # Update repository and sync
            update_repository()
            sync_packages()
        else:
            print("\nAll packages are up to date!")

if __name__ == "__main__":
    main()
