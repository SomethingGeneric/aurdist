#!/usr/bin/env python3
"""
AUR Utility - A comprehensive tool for building and managing AUR packages.

This script can:
1. Build specific packages when given as arguments
2. Check all packages in packages/ directory and rebuild outdated ones when run with no arguments
3. Handle AUR dependencies automatically by pulling them natively with Pacman
4. Manage the pacman repository database
5. Sync packages to remote locations
6. Track and clean up packages installed during the build process

Usage:
    python aurutil.py                    # Check and rebuild outdated packages
    python aurutil.py package-name       # Build specific package
    python aurutil.py -f package-name    # Force build specific package
    python aurutil.py --check-only       # Only check versions, don't build
    python aurutil.py --debug package    # Build with detailed output (for debugging)
    python aurutil.py --no-cleanup       # Don't clean up packages after building
    python aurutil.py --cleanup-only     # Only clean up tracked packages and exit
    python aurutil.py --remote-dest user@host:path  # Check versions against remote SSH destination
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
import tempfile
import atexit
import tomllib
from pathlib import Path
from datetime import datetime

# AUR RPC API endpoint
# Documentation: https://wiki.archlinux.org/title/AUR_web_interface#RPC_interface
AUR_RPC_URL = "https://aur.archlinux.org/rpc/?v=5&type=info&arg[]="

# Global tracking for cleanup
cloned_directories = set()
build_failures = []
installed_packages = set()  # Track packages installed during build process
root_directory = None  # Track the root directory for AUR package building

def load_ssh_config():
    """Load SSH configuration from ssh.toml file."""
    ssh_config_file = Path("ssh.toml")
    if not ssh_config_file.exists():
        # Return default config if file doesn't exist
        return {
            'user': None,
            'port': None,
            'strict_host_key_checking': 'no',
            'connect_timeout': None,
            'server_alive_interval': None
        }
    
    try:
        with open(ssh_config_file, 'rb') as f:
            config = tomllib.load(f)
        
        ssh_section = config.get('ssh', {})
        return {
            'user': ssh_section.get('user'),
            'port': ssh_section.get('port'),
            'strict_host_key_checking': ssh_section.get('strict_host_key_checking', 'no'),
            'connect_timeout': ssh_section.get('connect_timeout'),
            'server_alive_interval': ssh_section.get('server_alive_interval')
        }
    except Exception as e:
        print(f"Warning: Error reading ssh.toml: {e}")
        print("Using default SSH configuration")
        return {
            'user': None,
            'port': None,
            'strict_host_key_checking': 'no',
            'connect_timeout': None,
            'server_alive_interval': None
        }

def build_ssh_command_args(ssh_config):
    """Build SSH command arguments from configuration."""
    args = []
    
    # Add port if specified
    if ssh_config.get('port'):
        args.extend(['-p', str(ssh_config['port'])])
    
    # Add StrictHostKeyChecking option
    args.extend(['-o', f"StrictHostKeyChecking={ssh_config.get('strict_host_key_checking', 'no')}"])
    
    # Add ConnectTimeout if specified
    if ssh_config.get('connect_timeout'):
        args.extend(['-o', f"ConnectTimeout={ssh_config['connect_timeout']}"])
    
    # Add ServerAliveInterval if specified  
    if ssh_config.get('server_alive_interval'):
        args.extend(['-o', f"ServerAliveInterval={ssh_config['server_alive_interval']}"])
    
    return args

def cleanup_cloned_directories():
    """Clean up all cloned AUR directories."""
    for directory in cloned_directories.copy():
        if os.path.exists(directory):
            try:
                print(f"Cleaning up directory: {directory}")
                shutil.rmtree(directory)
                cloned_directories.discard(directory)
            except Exception as e:
                print(f"Warning: Failed to clean up {directory}: {e}")

def cleanup_installed_packages():
    """Clean up packages installed during the build process."""
    if not installed_packages:
        return
    
    print(f"\nCleaning up {len(installed_packages)} packages installed during build...")
    
    # Convert set to list for easier handling
    packages_to_remove = list(installed_packages)
    
    # Remove packages in batches to avoid command line length limits
    batch_size = 50
    for i in range(0, len(packages_to_remove), batch_size):
        batch = packages_to_remove[i:i + batch_size]
        print(f"Removing packages batch {i//batch_size + 1}: {', '.join(batch[:5])}{'...' if len(batch) > 5 else ''}")
        
        try:
            # Use pacman to remove packages
            run_command(f"sudo pacman -R --noconfirm {' '.join(batch)}", check=False)
            print(f"Successfully removed {len(batch)} packages")
        except Exception as e:
            print(f"Warning: Failed to remove some packages: {e}")
            # Try removing packages individually
            for package in batch:
                try:
                    run_command(f"sudo pacman -R --noconfirm {package}", check=False)
                    print(f"Removed {package}")
                except Exception as e:
                    print(f"Warning: Failed to remove {package}: {e}")
    
    # Clear the tracking set
    installed_packages.clear()
    print("Package cleanup completed")

def track_package_installation(package_name):
    """Track a package that was installed during the build process."""
    installed_packages.add(package_name)

def set_root_directory():
    """Set the root directory for AUR package building."""
    global root_directory
    root_directory = os.getcwd()

def get_root_directory():
    """Get the root directory for AUR package building."""
    global root_directory
    if root_directory is None:
        root_directory = os.getcwd()
    return root_directory

def ensure_root_directory():
    """Ensure we're in the root directory for AUR package building."""
    root_dir = get_root_directory()
    if os.getcwd() != root_dir:
        os.chdir(root_dir)

def get_installed_packages():
    """Get list of currently installed packages from pacman."""
    stdout, stderr = run_command("pacman -Qq", check=False)
    if stdout:
        return set(stdout.split('\n'))
    return set()

def manual_cleanup():
    """Manually clean up packages that were tracked during build process."""
    if not installed_packages:
        print("No packages to clean up.")
        return
    
    print(f"Found {len(installed_packages)} tracked packages:")
    for pkg in sorted(installed_packages):
        print(f"  - {pkg}")
    
    response = input("\nDo you want to remove these packages? (y/N): ")
    if response.lower() in ['y', 'yes']:
        cleanup_installed_packages()
    else:
        print("Cleanup cancelled.")

def register_cleanup():
    """Register cleanup function to run on exit."""
    atexit.register(cleanup_cloned_directories)
    atexit.register(cleanup_installed_packages)

def report_build_failures():
    """Report any build failures that occurred."""
    if build_failures:
        print(f"\n{'='*60}")
        print(f"BUILD FAILURES REPORT ({len(build_failures)} failures)")
        print(f"{'='*60}")
        
        for i, failure in enumerate(build_failures, 1):
            print(f"\n{i}. Package: {failure['package']}")
            print(f"   Command: {failure['command']}")
            print(f"   Time: {failure['timestamp']}")
            print(f"   Error: {failure['error']}")
        
        print(f"\n{'='*60}")
        return True
    return False

def safe_clone_aur_package(package_name, debug=False):
    """Safely clone an AUR package, removing existing directory if it exists."""
    # Remove existing directory if it exists
    if os.path.exists(package_name):
        print(f"Removing existing directory: {package_name}")
        try:
            shutil.rmtree(package_name)
        except Exception as e:
            print(f"Warning: Failed to remove existing directory {package_name}: {e}")
    
    # Clone the repository
    clone_cmd = f"git clone https://aur.archlinux.org/{package_name}.git"
    run_command(clone_cmd, package_name=package_name, debug=debug)
    
    # Register for cleanup
    cloned_directories.add(package_name)
    
    return package_name

def run_command(command, check=True, capture_output=True, cwd=None, package_name=None, debug=False):
    """Run a shell command and return the output."""
    if debug and not capture_output:
        # In debug mode, show output in real-time
        print(f"DEBUG: Running command: {command}")
        if cwd:
            print(f"DEBUG: In directory: {cwd}")
        result = subprocess.run(command, shell=True, cwd=cwd)
        if result.returncode != 0:
            error_msg = f"Command failed: '{command}' (exit code: {result.returncode})"
            if cwd:
                error_msg += f" in directory: {cwd}"
            
            if package_name:
                build_failures.append({
                    'package': package_name,
                    'command': command,
                    'error': error_msg,
                    'timestamp': datetime.now().isoformat()
                })
                print(f"BUILD FAILURE for {package_name}: {error_msg}")
            else:
                print(f"Error running command '{command}'")
            
            if check:
                sys.exit(result.returncode)
        return "", ""
    elif capture_output:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True, cwd=cwd)
        if result.returncode != 0:
            error_msg = f"Command failed: '{command}' (exit code: {result.returncode})"
            if cwd:
                error_msg += f" in directory: {cwd}"
            if result.stderr:
                error_msg += f"\nStderr: {result.stderr}"
            if result.stdout:
                error_msg += f"\nStdout: {result.stdout}"
            
            if package_name:
                build_failures.append({
                    'package': package_name,
                    'command': command,
                    'error': error_msg,
                    'timestamp': datetime.now().isoformat()
                })
                print(f"BUILD FAILURE for {package_name}: {error_msg}")
            else:
                print(f"Error running command '{command}': {result.stderr}")
            
            if check:
                sys.exit(result.returncode)
        return result.stdout.strip(), result.stderr.strip()
    else:
        result = subprocess.run(command, shell=True, cwd=cwd)
        if result.returncode != 0:
            error_msg = f"Command failed: '{command}' (exit code: {result.returncode})"
            if cwd:
                error_msg += f" in directory: {cwd}"
            
            if package_name:
                build_failures.append({
                    'package': package_name,
                    'command': command,
                    'error': error_msg,
                    'timestamp': datetime.now().isoformat()
                })
                print(f"BUILD FAILURE for {package_name}: {error_msg}")
            else:
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
    match = re.match(rf"{re.escape(package_name)}(?:-[a-zA-Z0-9]+)?-(.+)-[^-]+\.pkg\.tar\.zst", pkg_file.name)
    if match:
        return match.group(1)
    
    return '0'

def get_remote_version(package_name, remote_dest):
    """Get the version of a package from a remote SSH destination."""
    if not remote_dest:
        return '0'
    
    try:
        # Load SSH configuration
        ssh_config = load_ssh_config()
        
        # Use configured remote destination if available, otherwise use provided remote_dest
        if ssh_config.get('user'):
            remote_dest = ssh_config['user']
        
        # Parse the remote destination format: user@host:path
        if ':' in remote_dest:
            ssh_target, remote_path = remote_dest.rsplit(':', 1)
        else:
            # Assume current directory if no path specified
            ssh_target = remote_dest
            remote_path = '.'
        
        # Build SSH command with configuration
        ssh_args = build_ssh_command_args(ssh_config)
        ssh_args_str = ' '.join(ssh_args) if ssh_args else '-o StrictHostKeyChecking=no'
        
        # Use SSH to list package files matching the pattern on the remote host
        pattern = f"{package_name}-*.pkg.tar.zst"
        ssh_command = f"ssh {ssh_args_str} {ssh_target} 'cd {remote_path} && ls -1t {pattern} 2>/dev/null | head -1'"
        
        stdout, stderr = run_command(ssh_command, check=False)
        
        if not stdout.strip():
            return '0'
        
        # Extract the most recent package filename
        pkg_filename = stdout.strip().split('\n')[0]
        if not pkg_filename:
            return '0'
        
        # Extract version from filename (same pattern as local version)
        # Pattern: package-name-version-release-arch.pkg.tar.zst
        match = re.match(rf"{re.escape(package_name)}(?:-[a-zA-Z0-9]+)?-(.+)-[^-]+\.pkg\.tar\.zst", pkg_filename)
        if match:
            return match.group(1)
            
    except Exception as e:
        print(f"Error checking remote version for {package_name}: {e}")
    
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
        # Use a more specific pattern that matches the exact dependency type
        pattern = rf"^{dep_type}=\s*\((.*?)\)"
        matches = re.findall(pattern, content, re.MULTILINE | re.DOTALL)
        if matches:
            # Split by newlines and clean up
            deps = []
            for match in matches:
                lines = match.split('\n')
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        if dep_type == 'optdepends':
                            # For optional dependencies, extract only the package name (before the colon)
                            # Format: "package: description" or just "package"
                            if ':' in line:
                                package_name = line.split(':')[0].strip()
                                # Remove quotes if present
                                package_name = package_name.strip('\'"')
                                if package_name:
                                    deps.append(package_name)
                            else:
                                # No description, just package name
                                package_name = line.strip('\'"')
                                if package_name:
                                    deps.append(package_name)
                        else:
                            # For regular dependencies, split by spaces and clean up
                            dep_list = re.findall(r"'([^']+)'|\"([^\"]+)\"|(\S+)", line)
                            for dep in dep_list:
                                dep_name = dep[0] or dep[1] or dep[2]
                                if dep_name:
                                    deps.append(dep_name)
            dependencies[dep_type] = deps
    
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

def install_aur_package(package_name, visited=None, debug=False):
    """Install an AUR package by cloning and building it with dependency resolution."""
    if visited is None:
        visited = set()
    
    if package_name in visited:
        print(f"Circular dependency detected for {package_name}, skipping")
        return
    
    print(f"Installing AUR package: {package_name}")
    
    # Check if package is already installed
    stdout, stderr = run_command(f"pacman -Q {package_name}", check=False)
    if stdout and package_name in stdout:
        print(f"Package {package_name} is already installed")
        return
    
    # Add to visited set to prevent circular dependencies
    visited.add(package_name)
    
    # Ensure we're in the root directory for AUR package building
    ensure_root_directory()
    
    try:
        # Clone the AUR repository safely
        safe_clone_aur_package(package_name, debug=debug)
        
        # Change to package directory and recursively handle dependencies
        os.chdir(package_name)
        
        # Check and install dependencies for this AUR package
        check_and_install_dependencies(package_name, visited, debug=debug)
        
        # Build the package
        print(f"Building AUR package: {package_name}")
        run_command("makepkg -si --noconfirm", package_name=package_name, debug=debug, capture_output=False)
        
        # Track the package we just installed
        track_package_installation(package_name)
        
        # Go back to root directory
        ensure_root_directory()
        
    except Exception as e:
        print(f"Error building AUR package {package_name}: {e}")
        # Ensure we go back to root directory even on error
        ensure_root_directory()
        raise
    finally:
        # Remove from visited set after processing
        visited.discard(package_name)

def check_and_install_dependencies(package_name, visited=None, debug=False):
    """Check and install all dependencies for a package."""
    if visited is None:
        visited = set()
    
    print(f"Checking dependencies for package: {package_name}")
    
    try:
        # Clone the package first to get PKGBUILD
        safe_clone_aur_package(package_name, debug=debug)
        
        os.chdir(package_name)
        pkgbuild_path = "PKGBUILD"
        
        if not os.path.exists(pkgbuild_path):
            error_msg = f"PKGBUILD not found for {package_name}"
            print(error_msg)
            build_failures.append({
                'package': package_name,
                'command': 'check PKGBUILD',
                'error': error_msg,
                'timestamp': datetime.now().isoformat()
            })
            raise FileNotFoundError(error_msg)
        
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
            run_command(f"sudo pacman -S --noconfirm {' '.join(analysis['official_repos'])}", package_name=package_name, debug=debug, capture_output=False)
            # Track the packages we just installed
            for pkg in analysis['official_repos']:
                track_package_installation(pkg)
        
        # Install AUR packages
        for dep in analysis['aur_packages']:
            print(f"Installing AUR package: {dep}")
            # Go back to root directory for AUR package installation
            ensure_root_directory()
            install_aur_package(dep, visited, debug=debug)
            # Track the AUR package we just installed
            track_package_installation(dep)
            # Return to package directory
            os.chdir(package_name)
            
    except Exception as e:
        print(f"Error checking dependencies for {package_name}: {e}")
        # Ensure we go back to root directory even on error
        ensure_root_directory()
        raise


def build_package_native(package_name, debug=False):
    """Build a package natively."""
    print(f"Building package natively: {package_name}")
    
    # Set root directory if not already set
    if root_directory is None:
        set_root_directory()
    
    # Ensure packages directory exists
    os.makedirs("packages", exist_ok=True)
    
    try:
        # Check and install dependencies
        check_and_install_dependencies(package_name, debug=debug)
        
        # Build the package
        print("Building the package...")
        run_command("makepkg -sf --noconfirm", package_name=package_name, debug=debug, capture_output=False)
        
        # Copy the built package to the packages directory
        print("Copying built packages to packages/")
        run_command("cp *.pkg.tar.zst ../packages/", package_name=package_name, debug=debug)
        
        # Go back to root directory
        ensure_root_directory()
        
    except Exception as e:
        print(f"Error building package {package_name}: {e}")
        # Ensure we go back to root directory even on error
        ensure_root_directory()
        raise

def update_repository():
    """Update the pacman repository database."""
    packages_dir = Path("packages")
    if not packages_dir.exists():
        print("No packages directory found")
        return
    
    print("Updating repository database...")
    # Store current directory
    original_cwd = os.getcwd()
    
    try:
        os.chdir("packages")
        
        # Find all package files
        pkg_files = list(Path(".").glob("*.pkg.tar.zst"))
        
        if not pkg_files:
            print("No package files found")
            return
        
        # Update the repository database
        run_command("repo-add -vn aurdist.db.tar.zst *.pkg.tar.zst")
        
        print("Repository database updated")
    finally:
        # Always return to original directory
        os.chdir(original_cwd)

def sync_packages():
    """Sync packages to remote location if .where file exists."""
    where_file = Path(".where")
    if where_file.exists():
        with open(where_file, 'r') as f:
            remote_path = f.read().strip()
        
        if remote_path:
            # Load SSH configuration
            ssh_config = load_ssh_config()
            
            # Use configured remote destination if available, otherwise use .where file
            if ssh_config.get('user'):
                remote_path = ssh_config['user']
            
            print(f"Syncing packages to {remote_path}")
            
            # Build SSH command with configuration for rsync
            ssh_args = build_ssh_command_args(ssh_config)
            ssh_args_str = ' '.join(ssh_args) if ssh_args else '-o StrictHostKeyChecking=no'
            
            run_command(f"sudo rsync -avc -e 'ssh {ssh_args_str}' packages/ {remote_path}")
            print("Packages synced successfully")

def sync_single_package(package_name):
    """Sync packages to remote location after building a single package for recursive dependencies."""
    where_file = Path(".where")
    if where_file.exists():
        with open(where_file, 'r') as f:
            remote_path = f.read().strip()
        
        if remote_path:
            # Load SSH configuration
            ssh_config = load_ssh_config()
            
            # Use configured remote destination if available, otherwise use .where file
            if ssh_config.get('user'):
                remote_path = ssh_config['user']
            
            print(f"Syncing {package_name} to {remote_path} for recursive dependency support")
            # Update repository database first
            update_repository()
            
            # Build SSH command with configuration for rsync
            ssh_args = build_ssh_command_args(ssh_config)
            ssh_args_str = ' '.join(ssh_args) if ssh_args else '-o StrictHostKeyChecking=no'
            
            # Then sync
            run_command(f"sudo rsync -avc -e 'ssh {ssh_args_str}' packages/ {remote_path}")
            print(f"Package {package_name} synced successfully")
            # Update pacman database to make the package available immediately
            run_command("sudo pacman -Sy", check=False)

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
        match = re.match(r"([^-]+(?:-[^-]+)*)-[^-]+-[^-]+-[^-]+\.pkg\.tar\.zst", pkg_file.name)
        if match:
            packages.add(match.group(1))
    
    return list(packages)

def check_package_outdated(package_name, remote_dest=None):
    """Check if a package is outdated compared to AUR."""
    aur_version = get_aur_version(package_name)
    
    # Use remote version checking if remote_dest is specified, otherwise use local
    if remote_dest:
        local_version = get_remote_version(package_name, remote_dest)
        location_desc = f"remote ({remote_dest})"
    else:
        local_version = get_local_version(package_name)
        location_desc = "locally"
    
    if local_version == '0':
        return True, f"Package not found {location_desc} (AUR: {aur_version})"
    
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
    parser.add_argument('--debug', action='store_true', help='Show detailed output from makepkg and pacman commands (useful for manual debugging)')
    parser.add_argument('--no-cleanup', action='store_true', help='Don\'t clean up packages installed during build process')
    parser.add_argument('--cleanup-only', action='store_true', help='Only clean up tracked packages and exit')
    parser.add_argument('--remote-dest', type=str, help='SSH destination to check for existing packages (user@host:path)')
    
    args = parser.parse_args()
    
    # Register cleanup function (unless --no-cleanup is specified)
    if not args.no_cleanup:
        register_cleanup()
    
    print(f"AUR Utility - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    
    # Show remote dest mode if enabled
    if args.remote_dest:
        print(f"Remote destination mode enabled: {args.remote_dest}")
        print("Package versions will be checked against remote SSH destination")
        print("=" * 50)
    
    # Set root directory for AUR package building
    set_root_directory()
    
    # Handle cleanup-only mode
    if args.cleanup_only:
        manual_cleanup()
        return
    
    try:
        if args.package:
            # Build specific package
            package_name = args.package
            print(f"Building package: {package_name}")
            
            if not args.check_only:
                try:
                    build_package_native(package_name, debug=args.debug)
                    
                    # Update repository and sync after individual package build
                    update_repository()
                    sync_packages()
                    # Also sync individually for recursive dependencies
                    sync_single_package(package_name)
                except Exception as e:
                    print(f"Failed to build {package_name}: {e}")
                    # Don't exit here, let the failure reporting handle it
            else:
                # Just check version
                is_outdated, status = check_package_outdated(package_name, args.remote_dest)
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
                is_outdated, status = check_package_outdated(package, args.remote_dest)
                print(f"  {status}")
                
                if is_outdated or args.force:
                    packages_to_build.append(package)
            
            if packages_to_build:
                print(f"\nBuilding {len(packages_to_build)} outdated packages...")
                for package in packages_to_build:
                    print(f"\n{'='*20} Building {package} {'='*20}")
                    try:
                        build_package_native(package, debug=args.debug)
                        # Sync after each package for recursive dependencies
                        sync_single_package(package)
                    except Exception as e:
                        print(f"Failed to build {package}: {e}")
                        # Continue with next package instead of exiting
                
                # Update repository and sync
                update_repository()
                sync_packages()
            else:
                print("\nAll packages are up to date!")
    
    finally:
        # Clean up any remaining directories
        cleanup_cloned_directories()
        
        # Clean up installed packages if not disabled
        if not args.no_cleanup:
            cleanup_installed_packages()
        
        # Report any build failures
        if report_build_failures():
            sys.exit(1)

if __name__ == "__main__":
    main()
