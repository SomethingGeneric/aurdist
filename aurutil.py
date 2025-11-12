#!/usr/bin/env python3
"""
AUR Utility - A comprehensive tool for building and managing AUR packages.

This script can:
1. Build specific packages when given as arguments (AUR packages or generic git URLs)
2. Check all packages in packages/ directory and rebuild outdated ones when run with no arguments
3. Handle AUR dependencies automatically by pulling them natively with Pacman
4. Manage the pacman repository database
5. Sync packages to remote locations
6. Track and clean up packages installed during the build process
7. Support generic git URLs (HTTP/HTTPS/SSH) for custom PKGBUILD repositories

Usage:
    python aurutil.py                                          # Check and rebuild outdated packages
    python aurutil.py package-name                             # Build specific AUR package
    python aurutil.py https://github.com/user/repo.git         # Build from generic git URL
    python aurutil.py -f package-name                          # Force build specific package
    python aurutil.py --check-only                             # Only check versions, don't build
    python aurutil.py --debug package                          # Build with detailed output (for debugging)
    python aurutil.py --no-cleanup                             # Don't clean up packages after building
    python aurutil.py --cleanup-only                           # Only clean up tracked packages and exit
    python aurutil.py --remote-dest user@host:path             # Check versions against remote SSH destination
    python aurutil.py --cleanup-old-versions                   # Remove old versions, keep only latest of each package
    python aurutil.py --force-rebuild-all                      # Force rebuild all packages from targets.txt
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
import time
import shlex
from pathlib import Path
from datetime import datetime

# AUR RPC API endpoint
# Documentation: https://wiki.archlinux.org/title/AUR_web_interface#RPC_interface
AUR_RPC_URL = "https://aur.archlinux.org/rpc/?v=5&type=info&arg[]="

# Termbin URL for uploading logs
TERMBIN_URL = "termbin.com"
TERMBIN_PORT = 9999

# Logging levels
LOG_LEVEL_INFO = 0
LOG_LEVEL_DEBUG = 1

# Pre-compiled regex patterns for better performance
PKG_VERSION_REGEX = re.compile(r'^pkgver=[\'\"]?([^\'\"\n]+)[\'\"]?', re.MULTILINE)
PKG_FILENAME_REGEX = re.compile(r'^(.+?)-([^-]+)-([^-]+)-([^-]+)\.pkg\.tar\.zst$')
PKG_NAME_SIMPLE_REGEX = re.compile(r'([^-]+(?:-[^-]+)*)-[^-]+-[^-]+-[^-]+\.pkg\.tar\.zst')

# Global tracking for cleanup
cloned_directories = set()
build_failures = []
installed_packages = set()  # Track packages installed during build process
root_directory = None  # Track the root directory for AUR package building
aur_connectivity_errors = []  # Track AUR connectivity failures
current_log_level = LOG_LEVEL_INFO  # Default to minimal output
build_success_info = []  # Track successful builds for reporting
uptodate_package_info = []  # Track packages that are already up-to-date

# Caches for package queries to avoid redundant network/subprocess calls
_package_cache = {}  # Cache for package existence checks
_aur_version_cache = {}  # Cache for AUR version lookups

def log_info(message):
    """Print info level message (always shown)."""
    print(message)

def log_debug(message):
    """Print debug level message (only shown in debug mode)."""
    if current_log_level >= LOG_LEVEL_DEBUG:
        print(message)

def upload_to_termbin(content):
    """Upload content to termbin and return the URL.
    
    Args:
        content: Text content to upload
        
    Returns:
        URL string if successful, None otherwise
    """
    try:
        import socket
        
        # Connect to termbin
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((TERMBIN_URL, TERMBIN_PORT))
        
        # Send content
        sock.sendall(content.encode('utf-8'))
        
        # Receive URL
        url = sock.recv(1024).decode('utf-8').strip()
        sock.close()
        
        return url
    except Exception as e:
        log_debug(f"Failed to upload to termbin: {e}")
        return None

def aur_rpc_request_with_retry(url, max_retries=5, initial_backoff=1):
    """Make an AUR RPC request with exponential backoff retry logic.
    
    Args:
        url: The URL to request
        max_retries: Maximum number of retry attempts (default: 5)
        initial_backoff: Initial backoff time in seconds (default: 1)
    
    Returns:
        Response object if successful, None otherwise
    """
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                return response
            else:
                error_msg = f"AUR RPC returned status code {response.status_code}"
                if attempt < max_retries - 1:
                    backoff = initial_backoff * (2 ** attempt)
                    log_debug(f"  Attempt {attempt + 1}/{max_retries} failed: {error_msg}")
                    log_debug(f"  Retrying in {backoff} seconds...")
                    time.sleep(backoff)
                else:
                    log_debug(f"  All {max_retries} attempts failed: {error_msg}")
                    aur_connectivity_errors.append({
                        'url': url,
                        'error': error_msg,
                        'timestamp': datetime.now().isoformat()
                    })
        except requests.RequestException as e:
            error_msg = str(e)
            if attempt < max_retries - 1:
                backoff = initial_backoff * (2 ** attempt)
                log_debug(f"  Attempt {attempt + 1}/{max_retries} failed: {error_msg}")
                log_debug(f"  Retrying in {backoff} seconds...")
                time.sleep(backoff)
            else:
                log_debug(f"  All {max_retries} attempts failed: {error_msg}")
                aur_connectivity_errors.append({
                    'url': url,
                    'error': error_msg,
                    'timestamp': datetime.now().isoformat()
                })
    
    return None

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
                log_debug(f"Cleaning up directory: {directory}")
                shutil.rmtree(directory)
                cloned_directories.discard(directory)
            except Exception as e:
                log_debug(f"Warning: Failed to clean up {directory}: {e}")

def cleanup_installed_packages():
    """Clean up packages installed during the build process."""
    if not installed_packages:
        return
    
    log_debug(f"\nCleaning up {len(installed_packages)} packages installed during build...")
    
    # Convert set to list for easier handling
    packages_to_remove = list(installed_packages)
    
    # Remove packages in batches to avoid command line length limits
    batch_size = 50
    for i in range(0, len(packages_to_remove), batch_size):
        batch = packages_to_remove[i:i + batch_size]
        log_debug(f"Removing packages batch {i//batch_size + 1}: {', '.join(batch[:5])}{'...' if len(batch) > 5 else ''}")
        
        try:
            # Use pacman to remove packages
            run_command(f"sudo pacman -R --noconfirm {' '.join(batch)}", check=False)
            log_debug(f"Successfully removed {len(batch)} packages")
        except Exception as e:
            log_debug(f"Warning: Failed to remove some packages: {e}")
            # Try removing packages individually
            for package in batch:
                try:
                    run_command(f"sudo pacman -R --noconfirm {package}", check=False)
                    log_debug(f"Removed {package}")
                except Exception as e:
                    log_debug(f"Warning: Failed to remove {package}: {e}")
    
    # Clear the tracking set
    installed_packages.clear()
    log_debug("Package cleanup completed")

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

def report_aur_connectivity_errors():
    """Report any AUR connectivity errors that occurred."""
    if aur_connectivity_errors:
        print(f"\n{'='*60}")
        print(f"AUR CONNECTIVITY ERRORS ({len(aur_connectivity_errors)} errors)")
        print(f"{'='*60}")
        print("\nThe AUR RPC API could not be reached after multiple retry attempts.")
        print("This may indicate network connectivity issues or AUR service outages.")
        
        for i, error in enumerate(aur_connectivity_errors, 1):
            print(f"\n{i}. URL: {error['url']}")
            print(f"   Time: {error['timestamp']}")
            print(f"   Error: {error['error']}")
        
        print(f"\n{'='*60}")
        return True
    return False

def report_build_failures():
    """Report any build failures that occurred."""
    if build_failures:
        if current_log_level >= LOG_LEVEL_DEBUG:
            # Detailed output in debug mode
            log_debug(f"\n{'='*60}")
            log_debug(f"BUILD FAILURES REPORT ({len(build_failures)} failures)")
            log_debug(f"{'='*60}")
            
            for i, failure in enumerate(build_failures, 1):
                log_debug(f"\n{i}. Package: {failure['package']}")
                log_debug(f"   Command: {failure['command']}")
                log_debug(f"   Time: {failure['timestamp']}")
                log_debug(f"   Error: {failure['error']}")
            
            log_debug(f"\n{'='*60}")
        else:
            # Minimal output in info mode
            for failure in build_failures:
                # Try to upload log to termbin
                termbin_url = None
                if failure.get('log'):
                    termbin_url = upload_to_termbin(failure['log'])
                
                if termbin_url:
                    log_info(f"failed {failure['package']}, {termbin_url}")
                else:
                    log_info(f"failed {failure['package']}")
        
        return True
    return False

def report_build_successes():
    """Report any successful builds that occurred."""
    if build_success_info:
        for success in build_success_info:
            log_info(f"built {success['package']}, updated to {success['version']}")
        return True
    return False

def report_uptodate_packages():
    """Report packages that are already up-to-date."""
    if uptodate_package_info:
        for pkg_info in uptodate_package_info:
            log_info(f"uptodate {pkg_info['package']}, {pkg_info['version']}")
        return True
    return False

def is_git_url(target):
    """Check if a target is a git URL.
    
    Supported formats:
    - HTTP/HTTPS: http://host/path or https://host/path
    - SSH: git@host:path or ssh://user@host/path
    
    Note: Does not validate if the URL is actually a git repository,
    only checks if it matches common git URL patterns.
    """
    # Check for HTTP/HTTPS URLs
    if target.startswith('http://') or target.startswith('https://'):
        return True
    # Check for SSH URLs (git@host:path or ssh://...)
    if target.startswith('git@') or target.startswith('ssh://'):
        return True
    return False

def extract_package_name_from_git_url(git_url):
    """Extract package name from a git URL.
    
    Examples:
        https://github.com/user/repo.git -> repo
        https://github.com/user/pkgbuild.linux.git -> pkgbuild.linux
        git@github.com:user/repo.git -> repo
        ssh://git@github.com/user/project.git -> project
    
    Args:
        git_url: A git URL string
        
    Returns:
        The extracted package name (repository name without .git extension)
        
    Raises:
        ValueError: If the URL format is invalid or doesn't contain a repository name
    """
    # Remove .git suffix if present
    url = git_url
    if url.endswith('.git'):
        url = url[:-4]
    
    # Extract the last part of the path (repository name)
    if '/' in url:
        package_name = url.rstrip('/').split('/')[-1]
    else:
        # Handle edge case: SSH URLs like git@host:repo.git without path separator
        if ':' in url:
            package_name = url.split(':')[-1]
        else:
            raise ValueError(f"Cannot extract package name from URL: {git_url}")
    
    # Validate the package name doesn't contain protocol markers
    # Note: Package names can contain '@' (e.g., 'lib32-@foo'), so we only check for '://' 
    # which indicates the URL wasn't properly parsed
    if not package_name or '://' in package_name:
        raise ValueError(f"Invalid package name extracted from URL: {git_url}")
    
    return package_name

def safe_clone_aur_package(package_name, debug=False, git_url=None):
    """Safely clone an AUR package or generic git repository, removing existing directory if it exists.
    
    Args:
        package_name: Name of the package (used as directory name)
        debug: Enable debug output
        git_url: Optional git URL. If provided, clone from this URL instead of AUR
    """
    # Remove existing directory if it exists
    if os.path.exists(package_name):
        log_debug(f"Removing existing directory: {package_name}")
        try:
            shutil.rmtree(package_name)
        except Exception as e:
            log_debug(f"Warning: Failed to remove existing directory {package_name}: {e}")
    
    # Clone the repository
    if git_url:
        # Clone from provided git URL
        clone_cmd = f"git clone {git_url} {package_name}"
        log_debug(f"Cloning from generic git URL: {git_url}")
    else:
        # Clone from AUR
        clone_cmd = f"git clone https://aur.archlinux.org/{package_name}.git"
    
    run_command(clone_cmd, package_name=package_name, debug=debug)
    
    # Register for cleanup
    cloned_directories.add(package_name)
    
    return package_name

def run_command(command, check=True, capture_output=True, cwd=None, package_name=None, debug=False):
    """Run a shell command and return the output."""
    if debug and not capture_output:
        # In debug mode, show output in real-time
        log_debug(f"DEBUG: Running command: {command}")
        if cwd:
            log_debug(f"DEBUG: In directory: {cwd}")
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
                    'timestamp': datetime.now().isoformat(),
                    'log': ""
                })
                log_debug(f"BUILD FAILURE for {package_name}: {error_msg}")
            else:
                log_debug(f"Error running command '{command}'")
            
            if check:
                sys.exit(result.returncode)
        return "", ""
    elif capture_output:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True, cwd=cwd)
        if result.returncode != 0:
            error_msg = f"Command failed: '{command}' (exit code: {result.returncode})"
            full_log = f"Command: {command}\n"
            if cwd:
                error_msg += f" in directory: {cwd}"
                full_log += f"Directory: {cwd}\n"
            full_log += f"\n--- STDOUT ---\n{result.stdout}\n"
            full_log += f"\n--- STDERR ---\n{result.stderr}\n"
            
            if result.stderr:
                error_msg += f"\nStderr: {result.stderr}"
            if result.stdout:
                error_msg += f"\nStdout: {result.stdout}"
            
            if package_name:
                build_failures.append({
                    'package': package_name,
                    'command': command,
                    'error': error_msg,
                    'timestamp': datetime.now().isoformat(),
                    'log': full_log
                })
                log_debug(f"BUILD FAILURE for {package_name}: {error_msg}")
            else:
                log_debug(f"Error running command '{command}': {result.stderr}")
            
            if check:
                sys.exit(result.returncode)
        return result.stdout.strip(), result.stderr.strip()
    else:
        # When not capturing output, capture it temporarily for logging
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True, cwd=cwd)
        
        # Show output in debug mode
        if result.stdout:
            log_debug(result.stdout)
        if result.stderr:
            log_debug(result.stderr)
        
        if result.returncode != 0:
            error_msg = f"Command failed: '{command}' (exit code: {result.returncode})"
            full_log = f"Command: {command}\n"
            if cwd:
                error_msg += f" in directory: {cwd}"
                full_log += f"Directory: {cwd}\n"
            full_log += f"\n--- STDOUT ---\n{result.stdout}\n"
            full_log += f"\n--- STDERR ---\n{result.stderr}\n"
            
            if package_name:
                build_failures.append({
                    'package': package_name,
                    'command': command,
                    'error': error_msg,
                    'timestamp': datetime.now().isoformat(),
                    'log': full_log
                })
                log_debug(f"BUILD FAILURE for {package_name}: {error_msg}")
            else:
                log_debug(f"Error running command '{command}'")
            
            if check:
                sys.exit(result.returncode)
        return "", ""

def is_package_in_official_repos(package_name):
    """Check if a package is in the official repositories using pacman.
    
    Results are cached to avoid redundant subprocess calls.
    """
    cache_key = f"official:{package_name}"
    if cache_key in _package_cache:
        return _package_cache[cache_key]
    
    stdout, stderr = run_command(f"pacman -Si {package_name}", check=False)
    result = stdout and "Repository" in stdout
    _package_cache[cache_key] = result
    return result

def is_package_in_aur(package_name):
    """Check if a package exists in the AUR using the RPC interface.
    
    Results are cached to avoid redundant network calls.
    """
    cache_key = f"aur:{package_name}"
    if cache_key in _package_cache:
        return _package_cache[cache_key]
    
    response = aur_rpc_request_with_retry(f"{AUR_RPC_URL}{package_name}")
    if response:
        try:
            data = response.json()
            result = data.get("resultcount", 0) > 0
            _package_cache[cache_key] = result
            return result
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON response for {package_name}: {e}")
    
    _package_cache[cache_key] = False
    return False

def get_aur_package_info(package_name):
    """Get detailed information about an AUR package.
    
    Results are cached to avoid redundant network calls.
    """
    cache_key = f"info:{package_name}"
    if cache_key in _aur_version_cache:
        return _aur_version_cache[cache_key]
    
    response = aur_rpc_request_with_retry(f"{AUR_RPC_URL}{package_name}")
    if response:
        try:
            data = response.json()
            if data.get("resultcount", 0) > 0:
                result = data["results"][0]
                _aur_version_cache[cache_key] = result
                return result
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON response for {package_name}: {e}")
    
    _aur_version_cache[cache_key] = None
    return None

def get_aur_version(package_name):
    """Get the latest version of a package from the AUR.
    
    Uses cached package info to avoid redundant network calls.
    """
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
    
    # Get the most recent file by modification time (more reliable than creation time)
    pkg_file = max(pkg_files, key=lambda f: f.stat().st_mtime)
    
    # Extract version from filename using pre-compiled regex
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
    
    # Extract dependencies using optimized regex (avoid DOTALL for better performance)
    for dep_type in dependencies.keys():
        # Use a more specific pattern that matches the exact dependency type
        # Split into two steps for better performance
        pattern = rf"^{dep_type}=\s*\(((?:[^)]|\n)*?)\)"
        matches = re.findall(pattern, content, re.MULTILINE)
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
                                package_name = line.split(':', 1)[0].strip()
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

def parse_pkgbuild_version(pkgbuild_path):
    """Parse PKGBUILD file to extract version information.
    
    Args:
        pkgbuild_path: Path to the PKGBUILD file
        
    Returns:
        Version string from pkgver variable, or '0' if not found
    """
    if not os.path.exists(pkgbuild_path):
        return '0'
    
    try:
        with open(pkgbuild_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Extract pkgver using pre-compiled regex
        # Match: pkgver=value or pkgver='value' or pkgver="value"
        match = PKG_VERSION_REGEX.search(content)
        if match:
            version = match.group(1).strip()
            return version
        
        return '0'
    except Exception as e:
        print(f"Error parsing PKGBUILD for version: {e}")
        return '0'

def get_git_package_version(git_url, package_name, debug=False):
    """Get the version of a package from a git repository by cloning and parsing PKGBUILD.
    
    Args:
        git_url: The git URL to clone
        package_name: Name of the package (used as directory name)
        debug: Enable debug output
        
    Returns:
        Version string from the PKGBUILD, or '0' if not found
    """
    # Ensure we're in the root directory
    ensure_root_directory()
    
    temp_dir = None
    try:
        # Create a temporary directory for cloning
        temp_dir = f".git_version_check_{package_name}"
        
        # Remove existing directory if it exists
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        
        # Clone the repository
        clone_cmd = f"git clone --depth 1 {git_url} {temp_dir}"
        log_debug(f"Cloning {git_url} to check version...")
        run_command(clone_cmd, check=False, debug=debug)
        
        # Parse the PKGBUILD
        pkgbuild_path = os.path.join(temp_dir, "PKGBUILD")
        version = parse_pkgbuild_version(pkgbuild_path)
        
        log_debug(f"Version from git PKGBUILD: {version}")
        
        return version
        
    except Exception as e:
        log_debug(f"Error getting git package version: {e}")
        return '0'
    finally:
        # Clean up temporary directory
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                log_debug(f"Warning: Failed to clean up {temp_dir}: {e}")

def batch_check_aur_packages(package_names):
    """Check multiple packages in AUR at once using batch RPC request.
    
    This is much more efficient than checking packages one by one.
    Returns a set of package names that exist in AUR.
    """
    if not package_names:
        return set()
    
    # Check cache first
    uncached = []
    cached_results = set()
    for pkg in package_names:
        cache_key = f"aur:{pkg}"
        if cache_key in _package_cache:
            if _package_cache[cache_key]:
                cached_results.add(pkg)
        else:
            uncached.append(pkg)
    
    if not uncached:
        return cached_results
    
    # Build batch URL - AUR RPC supports multiple arg[] parameters
    url = "https://aur.archlinux.org/rpc/?v=5&type=info"
    for pkg in uncached:
        url += f"&arg[]={pkg}"
    
    response = aur_rpc_request_with_retry(url)
    aur_packages = set()
    
    if response:
        try:
            data = response.json()
            if data.get("resultcount", 0) > 0:
                for result in data["results"]:
                    pkg_name = result.get("Name")
                    if pkg_name:
                        aur_packages.add(pkg_name)
                        # Cache the positive result
                        _package_cache[f"aur:{pkg_name}"] = True
            
            # Cache negative results for packages not found
            for pkg in uncached:
                if pkg not in aur_packages:
                    _package_cache[f"aur:{pkg}"] = False
                    
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON response for batch query: {e}")
    
    return aur_packages | cached_results

def analyze_dependency_status(dependencies):
    """Analyze dependencies and categorize them by availability.
    
    Optimized to use batch queries instead of checking each package individually.
    """
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
    
    # Remove duplicates and empty strings
    all_deps = [dep.strip() for dep in all_deps if dep.strip()]
    analysis['total_count'] = len(all_deps)
    
    if not all_deps:
        return analysis
    
    # First, check which packages are NOT in official repos (since those are quick to check)
    # and collect candidates for AUR checking
    aur_candidates = []
    for dep in all_deps:
        if is_package_in_official_repos(dep):
            analysis['official_repos'].append(dep)
        else:
            aur_candidates.append(dep)
    
    # Batch check all AUR candidates at once
    aur_packages = batch_check_aur_packages(aur_candidates)
    
    # Categorize results
    for dep in aur_candidates:
        if dep in aur_packages:
            analysis['aur_packages'].append(dep)
        else:
            analysis['not_found'].append(dep)
    
    return analysis

def install_aur_package(package_name, visited=None, debug=False):
    """Install an AUR package by cloning and building it with dependency resolution."""
    if visited is None:
        visited = set()
    
    if package_name in visited:
        log_debug(f"Circular dependency detected for {package_name}, skipping")
        return
    
    log_debug(f"Installing AUR package: {package_name}")
    
    # Check if package is already installed
    stdout, stderr = run_command(f"pacman -Q {package_name}", check=False)
    if stdout and package_name in stdout:
        log_debug(f"Package {package_name} is already installed")
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
        log_debug(f"Building AUR package: {package_name}")
        run_command("makepkg -sir --noconfirm", package_name=package_name, debug=debug, capture_output=False)
        
        # Track the package we just installed
        track_package_installation(package_name)
        
        # Go back to root directory
        ensure_root_directory()
        
    except Exception as e:
        log_debug(f"Error building AUR package {package_name}: {e}")
        # Ensure we go back to root directory even on error
        ensure_root_directory()
        raise
    finally:
        # Remove from visited set after processing
        visited.discard(package_name)

def check_and_install_dependencies(package_name, visited=None, debug=False, git_url=None):
    """Check and install all dependencies for a package.
    
    Args:
        package_name: Name of the package
        visited: Set of already visited packages (for circular dependency detection)
        debug: Enable debug output
        git_url: Optional git URL. If provided, clone from this URL instead of AUR
    """
    if visited is None:
        visited = set()
    
    log_debug(f"Checking dependencies for package: {package_name}")
    
    try:
        # Clone the package first to get PKGBUILD
        safe_clone_aur_package(package_name, debug=debug, git_url=git_url)
        
        os.chdir(package_name)
        pkgbuild_path = "PKGBUILD"
        
        if not os.path.exists(pkgbuild_path):
            error_msg = f"PKGBUILD not found for {package_name}"
            log_debug(error_msg)
            build_failures.append({
                'package': package_name,
                'command': 'check PKGBUILD',
                'error': error_msg,
                'timestamp': datetime.now().isoformat(),
                'log': error_msg
            })
            raise FileNotFoundError(error_msg)
        
        # Parse dependencies from PKGBUILD
        deps = parse_pkgbuild_dependencies(pkgbuild_path)
        
        # Analyze dependency status
        analysis = analyze_dependency_status(deps)
        
        log_debug(f"\nDependency Analysis for {package_name}:")
        log_debug(f"  Total dependencies: {analysis['total_count']}")
        log_debug(f"  Available in official repos: {len(analysis['official_repos'])}")
        log_debug(f"  Available in AUR: {len(analysis['aur_packages'])}")
        log_debug(f"  Not found: {len(analysis['not_found'])}")
        
        if analysis['aur_packages']:
            log_debug(f"  AUR packages: {', '.join(analysis['aur_packages'])}")
            log_debug("  WARNING: This package depends on other AUR packages!")
        
        if analysis['not_found']:
            log_debug(f"  Missing packages: {', '.join(analysis['not_found'])}")
            log_debug("  WARNING: Some dependencies could not be found!")
        
        # Install dependencies
        log_debug(f"\nInstalling dependencies...")
        
        # Install from official repos first
        if analysis['official_repos']:
            log_debug(f"Installing from official repos: {', '.join(analysis['official_repos'])}")
            run_command(f"sudo pacman -S --noconfirm {' '.join(analysis['official_repos'])}", package_name=package_name, debug=debug, capture_output=False)
            # Track the packages we just installed
            for pkg in analysis['official_repos']:
                track_package_installation(pkg)
        
        # Install AUR packages
        for dep in analysis['aur_packages']:
            log_debug(f"Installing AUR package: {dep}")
            # Go back to root directory for AUR package installation
            ensure_root_directory()
            install_aur_package(dep, visited, debug=debug)
            # Track the AUR package we just installed
            track_package_installation(dep)
            # Return to package directory
            os.chdir(package_name)
            
    except Exception as e:
        log_debug(f"Error checking dependencies for {package_name}: {e}")
        # Ensure we go back to root directory even on error
        ensure_root_directory()
        raise


def build_package_native(package_name, debug=False, git_url=None):
    """Build a package natively.
    
    Args:
        package_name: Name of the package to build
        debug: Enable debug output
        git_url: Optional git URL. If provided, clone from this URL instead of AUR
    
    Returns:
        Version string of the built package, or None if build failed
    """
    if git_url:
        log_debug(f"Building package natively from git URL: {package_name} ({git_url})")
    else:
        log_debug(f"Building package natively: {package_name}")
    
    # Set root directory if not already set
    if root_directory is None:
        set_root_directory()
    
    # Ensure packages directory exists
    os.makedirs("packages", exist_ok=True)
    
    try:
        # Check and install dependencies
        check_and_install_dependencies(package_name, debug=debug, git_url=git_url)
        
        # Build the package
        log_debug("Building the package...")
        run_command("makepkg -sfr --noconfirm", package_name=package_name, debug=debug, capture_output=False)
        
        # Copy the built package to the packages directory
        log_debug("Copying built packages to packages/")
        run_command("cp *.pkg.tar.zst ../packages/", package_name=package_name, debug=debug)
        
        # Get the version of the built package
        if git_url:
            version = get_git_package_version(git_url, package_name, debug=debug)
        else:
            version = get_aur_version(package_name)
        
        # Go back to root directory
        ensure_root_directory()
        
        return version
        
    except Exception as e:
        log_debug(f"Error building package {package_name}: {e}")
        # Ensure we go back to root directory even on error
        ensure_root_directory()
        raise

def generate_index_html(pkg_files):
    """Generate index.html with list of available packages.
    
    Args:
        pkg_files: List of Path objects for package files
    """
    # Extract package information
    packages = []
    for pkg_file in pkg_files:
        # Pattern: package-name-version-release-arch.pkg.tar.zst (use pre-compiled regex)
        match = PKG_FILENAME_REGEX.match(pkg_file.name)
        if match:
            name = match.group(1)
            version = match.group(2)
            release = match.group(3)
            arch = match.group(4)
            size = pkg_file.stat().st_size
            # Convert size to human readable format
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_kb = size / 1024
                size_str = f"{size_kb:.1f} KB"
            elif size < 1024 * 1024 * 1024:
                size_mb = size / (1024 * 1024)
                size_str = f"{size_mb:.1f} MB"
            else:
                size_gb = size / (1024 * 1024 * 1024)
                size_str = f"{size_gb:.1f} GB"
            
            packages.append({
                'name': name,
                'version': version,
                'release': release,
                'arch': arch,
                'filename': pkg_file.name,
                'size': size_str
            })
    
    # Sort packages by name
    packages.sort(key=lambda x: x['name'])
    
    # Generate the package table HTML
    package_table_html = ''
    if packages:
        package_table_html += f'            <p class="package-count">Total packages: {len(packages)}</p>\n'
        package_table_html += '            <table class="package-table">\n'
        package_table_html += '                <thead>\n'
        package_table_html += '                    <tr>\n'
        package_table_html += '                        <th>Package Name</th>\n'
        package_table_html += '                        <th>Version</th>\n'
        package_table_html += '                        <th>Arch</th>\n'
        package_table_html += '                        <th>Size</th>\n'
        package_table_html += '                        <th>File</th>\n'
        package_table_html += '                    </tr>\n'
        package_table_html += '                </thead>\n'
        package_table_html += '                <tbody>\n'
        
        for pkg in packages:
            package_table_html += '                    <tr>\n'
            package_table_html += f'                        <td class="package-name">{pkg["name"]}</td>\n'
            package_table_html += f'                        <td>{pkg["version"]}-{pkg["release"]}</td>\n'
            package_table_html += f'                        <td>{pkg["arch"]}</td>\n'
            package_table_html += f'                        <td>{pkg["size"]}</td>\n'
            package_table_html += f'                        <td><a href="{pkg["filename"]}">{pkg["filename"]}</a></td>\n'
            package_table_html += '                    </tr>\n'
        
        package_table_html += '                </tbody>\n'
        package_table_html += '            </table>\n'
    else:
        package_table_html += '            <p>No packages available yet.</p>\n'
    
    # Read the HTML template file
    template_path = Path(get_root_directory()) / 'index.template.html'
    if not template_path.exists():
        log_debug(f"Warning: Template file not found at {template_path}, using fallback")
        # Fallback to writing directly if template doesn't exist
        html = package_table_html
    else:
        with open(template_path, 'r', encoding='utf-8') as f:
            template = f.read()
        
        # Replace the placeholder with the generated package table
        html = template.replace('{{PACKAGE_TABLE}}', package_table_html)
    
    # Write the HTML file
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html)
    
    log_debug(f"Generated index.html with {len(packages)} packages")

def update_repository():
    """Update the pacman repository database."""
    packages_dir = Path("packages")
    if not packages_dir.exists():
        log_debug("No packages directory found")
        return
    
    log_debug("Updating repository database...")
    # Store current directory
    original_cwd = os.getcwd()
    
    try:
        os.chdir("packages")
        
        # Find all package files
        pkg_files = list(Path(".").glob("*.pkg.tar.zst"))
        
        if not pkg_files:
            log_debug("No package files found")
            return
        
        # Update the repository database
        run_command("repo-add -vn aurdist.db.tar.zst *.pkg.tar.zst")
        
        # Generate index.html with package list
        generate_index_html(pkg_files)
        
        log_debug("Repository database updated")
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
            
            log_debug(f"Syncing packages to {remote_path}")
            
            # Build SSH command with configuration for rsync
            ssh_args = build_ssh_command_args(ssh_config)
            ssh_args_str = ' '.join(ssh_args) if ssh_args else '-o StrictHostKeyChecking=no'
            
            run_command(f"rsync -avc -e 'ssh {ssh_args_str}' packages/ {remote_path}")
            log_debug("Packages synced successfully")

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
            
            log_debug(f"Syncing {package_name} to {remote_path} for recursive dependency support")
            # Update repository database first
            update_repository()
            
            # Build SSH command with configuration for rsync
            ssh_args = build_ssh_command_args(ssh_config)
            ssh_args_str = ' '.join(ssh_args) if ssh_args else '-o StrictHostKeyChecking=no'
            
            # Then sync
            run_command(f"rsync -avc -e 'ssh {ssh_args_str}' packages/ {remote_path}")
            log_debug(f"Package {package_name} synced successfully")
            # Update pacman database to make the package available immediately
            run_command("sudo pacman -Sy", check=False)

def cleanup_old_package_versions_local():
    """Remove old versions of packages from local packages/ directory, keeping only the latest version of each."""
    packages_dir = Path("packages")
    if not packages_dir.exists():
        log_info("No packages directory found, nothing to clean up")
        return
    
    # Dictionary to store package versions: {package_name: [(version, release, arch, filepath), ...]}
    package_versions = {}
    
    # Scan all package files
    for pkg_file in packages_dir.glob("*.pkg.tar.zst"):
        # Extract package info from filename using pre-compiled regex
        # Pattern: package-name-version-release-arch.pkg.tar.zst
        # We need to handle package names that contain hyphens
        match = PKG_FILENAME_REGEX.match(pkg_file.name)
        if match:
            pkg_name = match.group(1)
            version = match.group(2)
            release = match.group(3)
            arch = match.group(4)
            
            if pkg_name not in package_versions:
                package_versions[pkg_name] = []
            
            package_versions[pkg_name].append((version, release, arch, pkg_file))
    
    # For each package, keep only the latest version
    removed_count = 0
    for pkg_name, versions in package_versions.items():
        if len(versions) <= 1:
            continue  # Only one version, nothing to remove
        
        # Sort by modification time (most recent first)
        versions.sort(key=lambda x: x[3].stat().st_mtime, reverse=True)
        
        # Keep the first (most recent) version, remove the rest
        latest = versions[0]
        old_versions = versions[1:]
        
        log_info(f"Package '{pkg_name}': keeping latest version {latest[1]}-{latest[2]}, removing {len(old_versions)} old version(s)")
        
        for old_version, old_release, old_arch, old_file in old_versions:
            log_debug(f"  Removing old version: {old_file.name}")
            try:
                old_file.unlink()
                removed_count += 1
                
                # Also remove signature file if it exists
                sig_file = old_file.with_suffix(old_file.suffix + '.sig')
                if sig_file.exists():
                    sig_file.unlink()
                    log_debug(f"  Removed signature: {sig_file.name}")
            except Exception as e:
                log_debug(f"  Error removing {old_file.name}: {e}")
    
    if removed_count > 0:
        log_info(f"Removed {removed_count} old package version(s) from local repository")
    else:
        log_info("No old package versions found to remove")

def cleanup_old_package_versions_remote():
    """Remove old versions of packages from remote repository via SSH, keeping only the latest version of each."""
    where_file = Path(".where")
    if not where_file.exists():
        log_info("No .where file found, skipping remote cleanup")
        return
    
    with open(where_file, 'r') as f:
        remote_path = f.read().strip()
    
    if not remote_path:
        log_info("Empty .where file, skipping remote cleanup")
        return
    
    try:
        # Load SSH configuration
        ssh_config = load_ssh_config()
        
        # Use configured remote destination if available
        if ssh_config.get('user'):
            remote_path = ssh_config['user']
        
        # Parse the remote destination format: user@host:path
        if ':' in remote_path:
            ssh_target, remote_dir = remote_path.rsplit(':', 1)
        else:
            ssh_target = remote_path
            remote_dir = '.'
        
        # Build SSH command with configuration
        ssh_args = build_ssh_command_args(ssh_config)
        ssh_args_str = ' '.join(ssh_args) if ssh_args else '-o StrictHostKeyChecking=no'
        
        log_info(f"Cleaning up old package versions on remote repository at {remote_path}")
        
        # Create a Python script to run remotely that will clean up old versions
        cleanup_script = '''
import os
import re
from pathlib import Path
from collections import defaultdict

# Dictionary to store package versions: {package_name: [(version, release, arch, filepath, mtime), ...]}
package_versions = defaultdict(list)

# Scan all package files
for pkg_file in Path(".").glob("*.pkg.tar.zst"):
    # Extract package info from filename
    match = re.match(r"^(.+?)-([^-]+)-([^-]+)-([^-]+)\\.pkg\\.tar\\.zst$", pkg_file.name)
    if match:
        pkg_name = match.group(1)
        version = match.group(2)
        release = match.group(3)
        arch = match.group(4)
        mtime = pkg_file.stat().st_mtime
        
        package_versions[pkg_name].append((version, release, arch, pkg_file, mtime))

# For each package, keep only the latest version
removed_files = []
for pkg_name, versions in package_versions.items():
    if len(versions) <= 1:
        continue  # Only one version, nothing to remove
    
    # Sort by modification time (most recent first)
    versions.sort(key=lambda x: x[4], reverse=True)
    
    # Keep the first (most recent) version, remove the rest
    old_versions = versions[1:]
    
    for old_version, old_release, old_arch, old_file, _ in old_versions:
        try:
            old_file.unlink()
            removed_files.append(old_file.name)
            
            # Also remove signature file if it exists
            sig_file = old_file.with_suffix(old_file.suffix + ".sig")
            if sig_file.exists():
                sig_file.unlink()
        except Exception as e:
            print(f"Error removing {old_file.name}: {e}", file=sys.stderr)

# Print removed files (one per line) for the calling script to count
for filename in removed_files:
    print(filename)
'''
        
        # Execute the cleanup script on the remote server
        ssh_command = f"ssh {ssh_args_str} {ssh_target} 'cd {remote_dir} && python3 -c {shlex.quote(cleanup_script)}'"
        
        stdout, stderr = run_command(ssh_command, check=False)
        
        if stdout.strip():
            removed_files = stdout.strip().split('\n')
            log_info(f"Removed {len(removed_files)} old package version(s) from remote repository")
            for filename in removed_files:
                log_debug(f"  Removed: {filename}")
        else:
            log_info("No old package versions found to remove from remote")
        
        if stderr:
            log_debug(f"Remote cleanup warnings: {stderr}")
        
        # Update the remote repository database after cleanup
        log_debug("Updating remote repository database...")
        ssh_command = f"ssh {ssh_args_str} {ssh_target} 'cd {remote_dir} && repo-add -R aurdist.db.tar.zst *.pkg.tar.zst 2>/dev/null || true'"
        run_command(ssh_command, check=False)
        log_debug("Remote repository database updated")
        
    except Exception as e:
        log_info(f"Error during remote cleanup: {e}")

def get_packages_from_targets():
    """Get list of packages from targets.txt file.
    
    Returns a list of tuples: (package_name, git_url)
    where git_url is None for AUR packages, or the URL for generic git repositories.
    """
    targets_file = Path("targets.txt")
    if not targets_file.exists():
        return []
    
    packages = []
    with open(targets_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                if is_git_url(line):
                    # Extract package name from git URL
                    package_name = extract_package_name_from_git_url(line)
                    packages.append((package_name, line))
                else:
                    # Regular AUR package name
                    packages.append((line, None))
    
    return packages

def remove_package_from_targets(package_name):
    """Remove a package from targets.txt file.
    
    Args:
        package_name: Name of the package to remove
        
    Returns:
        True if package was found and removed, False otherwise
    """
    targets_file = Path("targets.txt")
    if not targets_file.exists():
        return False
    
    # Read all lines
    with open(targets_file, 'r') as f:
        lines = f.readlines()
    
    # Filter out the package (keep lines that don't match)
    new_lines = []
    removed = False
    for line in lines:
        stripped = line.strip()
        # Skip the package line if it matches
        if stripped and not stripped.startswith('#'):
            # Check if it's an AUR package name or git URL that contains the package
            if stripped == package_name:
                removed = True
                log_info(f"Removing '{package_name}' from targets.txt")
                continue
            elif is_git_url(stripped):
                # For git URLs, extract package name and compare
                try:
                    url_package_name = extract_package_name_from_git_url(stripped)
                    if url_package_name == package_name:
                        removed = True
                        log_info(f"Removing '{stripped}' from targets.txt (package: {package_name})")
                        continue
                except ValueError:
                    pass  # Keep the line if we can't parse it
        
        new_lines.append(line)
    
    # Write back if we removed something
    if removed:
        with open(targets_file, 'w') as f:
            f.writelines(new_lines)
    
    return removed

def remove_package_from_remote(package_name):
    """Remove a package from remote repository via SSH.
    
    Args:
        package_name: Name of the package to remove from remote
        
    Returns:
        True if successful, False otherwise
    """
    where_file = Path(".where")
    if not where_file.exists():
        log_debug("No .where file found, skipping remote package removal")
        return False
    
    with open(where_file, 'r') as f:
        remote_path = f.read().strip()
    
    if not remote_path:
        log_debug("Empty .where file, skipping remote package removal")
        return False
    
    try:
        # Load SSH configuration
        ssh_config = load_ssh_config()
        
        # Use configured remote destination if available
        if ssh_config.get('user'):
            remote_path = ssh_config['user']
        
        # Parse the remote destination format: user@host:path
        if ':' in remote_path:
            ssh_target, remote_dir = remote_path.rsplit(':', 1)
        else:
            ssh_target = remote_path
            remote_dir = '.'
        
        # Build SSH command with configuration
        ssh_args = build_ssh_command_args(ssh_config)
        ssh_args_str = ' '.join(ssh_args) if ssh_args else '-o StrictHostKeyChecking=no'
        
        log_info(f"Removing package '{package_name}' from remote repository at {remote_path}")
        
        # Remove all package files matching the pattern
        pattern = f"{package_name}-*.pkg.tar.zst*"
        ssh_command = f"ssh {ssh_args_str} {ssh_target} 'cd {remote_dir} && rm -f {pattern}'"
        
        stdout, stderr = run_command(ssh_command, check=False)
        
        if stderr:
            log_debug(f"Warning during remote removal: {stderr}")
        
        log_info(f"Successfully removed '{package_name}' from remote repository")
        
        # Update the remote repository database
        ssh_command = f"ssh {ssh_args_str} {ssh_target} 'cd {remote_dir} && repo-add -R aurdist.db.tar.zst {pattern} 2>/dev/null || true'"
        run_command(ssh_command, check=False)
        
        return True
        
    except Exception as e:
        log_debug(f"Error removing package from remote: {e}")
        return False

def create_github_issue_for_removed_package(package_name, reason="Package no longer found in AUR"):
    """Create a GitHub issue to notify about a removed package.
    
    Args:
        package_name: Name of the removed package
        reason: Reason for removal
        
    Returns:
        True if issue was created successfully, False otherwise
    """
    try:
        # Check if we're in a GitHub Actions environment
        github_token = os.environ.get('GITHUB_TOKEN')
        github_repo = os.environ.get('GITHUB_REPOSITORY')
        
        if not github_token or not github_repo:
            log_debug("Not running in GitHub Actions or missing credentials, skipping issue creation")
            return False
        
        # Create issue using GitHub API
        issue_title = f"Security: Package '{package_name}' removed from AUR"
        issue_body = f"""## Package Removed from Repository

**Package Name:** `{package_name}`  
**Reason:** {reason}  
**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

### What happened?

This package was automatically removed from our AUR repository because it is no longer available in the AUR. This is a security measure to prevent potential malicious re-uploads.

### What should you do?

If you have this package installed on your system:
1. The package will no longer receive updates from our repository
2. Consider finding an alternative package or removing it
3. Check if the package has been renamed or moved to a different repository

### Technical Details

- The package was removed from `targets.txt`
- All package files were removed from the remote repository
- The repository database was updated

This is an automated security measure implemented to protect users from potentially malicious package re-uploads.
"""
        
        api_url = f"https://api.github.com/repos/{github_repo}/issues"
        headers = {
            'Authorization': f'token {github_token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        data = {
            'title': issue_title,
            'body': issue_body,
            'labels': ['security', 'automated']
        }
        
        response = requests.post(api_url, headers=headers, json=data, timeout=30)
        
        if response.status_code == 201:
            issue_url = response.json().get('html_url', 'unknown')
            log_info(f"Created GitHub issue for removed package: {issue_url}")
            return True
        else:
            log_debug(f"Failed to create GitHub issue: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        log_debug(f"Error creating GitHub issue: {e}")
        return False

def handle_missing_aur_packages():
    """Check for packages in targets.txt that are no longer in AUR and handle them.
    
    This function:
    1. Identifies AUR packages (not git URLs) in targets.txt
    2. Checks if they still exist in AUR
    3. For missing packages:
       - Removes them from the remote repository
       - Removes them from targets.txt
       - Creates a GitHub issue to notify users
       
    Returns:
        List of package names that were removed
    """
    removed_packages = []
    target_packages = get_packages_from_targets()
    
    if not target_packages:
        return removed_packages
    
    log_debug("\nChecking for missing AUR packages...")
    
    for package_name, git_url in target_packages:
        # Skip git URL packages - they're not from AUR
        if git_url:
            log_debug(f"Skipping {package_name} (git URL package)")
            continue
        
        # Check if package exists in AUR
        log_debug(f"Checking if '{package_name}' exists in AUR...")
        
        if not is_package_in_aur(package_name):
            log_info(f"⚠️  SECURITY: Package '{package_name}' not found in AUR - removing from repository")
            
            # Remove from remote repository
            remove_package_from_remote(package_name)
            
            # Remove from targets.txt
            remove_package_from_targets(package_name)
            
            # Create GitHub issue
            create_github_issue_for_removed_package(package_name)
            
            removed_packages.append(package_name)
        else:
            log_debug(f"Package '{package_name}' found in AUR - OK")
    
    if removed_packages:
        log_info(f"\n{'='*60}")
        log_info(f"SECURITY: Removed {len(removed_packages)} missing AUR package(s)")
        log_info(f"{'='*60}")
        for pkg in removed_packages:
            log_info(f"  - {pkg}")
        log_info(f"{'='*60}")
    else:
        log_debug("All AUR packages in targets.txt are still available")
    
    return removed_packages

def get_existing_packages():
    """Get list of packages that already exist in packages/ directory."""
    packages_dir = Path("packages")
    if not packages_dir.exists():
        return []
    
    packages = set()
    for pkg_file in packages_dir.glob("*.pkg.tar.zst"):
        # Extract package name from filename using pre-compiled regex
        # Pattern: package-name-version-release-arch.pkg.tar.zst
        match = PKG_NAME_SIMPLE_REGEX.match(pkg_file.name)
        if match:
            packages.add(match.group(1))
    
    return list(packages)

def check_package_outdated(package_name, remote_dest=None, is_git_package=False, git_url=None, debug=False):
    """Check if a package is outdated compared to AUR or git repository.
    
    Args:
        package_name: Name of the package to check
        remote_dest: Optional SSH destination for remote version checking
        is_git_package: True if this is a git URL package (skips AUR version check)
        git_url: Git URL for version checking (only used when is_git_package=True)
        debug: Enable debug output
        
    Returns:
        Tuple of (is_outdated: bool, status: str, version: str) where:
        - is_outdated: True if the package needs to be built/updated
        - status: Human-readable status message
        - version: The locally installed or remote package version (from 
                   packages/ directory or SSH remote if --remote-dest is 
                   specified), or '0' if not found
    """
    # For git URL packages, check version from git repository PKGBUILD
    if is_git_package:
        # Get version from git repository
        if git_url:
            git_version = get_git_package_version(git_url, package_name, debug=debug)
        else:
            git_version = '0'
        
        # Use remote version checking if remote_dest is specified, otherwise use local
        if remote_dest:
            local_version = get_remote_version(package_name, remote_dest)
            location_desc = f"remote ({remote_dest})"
        else:
            local_version = get_local_version(package_name)
            location_desc = "locally"
        
        if local_version == '0':
            return True, f"Git package not found {location_desc} (Git: {git_version})", '0'
        
        if git_version == '0':
            return False, f"Git package found {location_desc} (Version: {local_version}, Git version unknown)", local_version
        
        # Compare versions
        if git_version != local_version:
            return True, f"Outdated (Local: {local_version}, Git: {git_version})", local_version
        else:
            return False, f"Up to date (Version: {local_version})", local_version
    
    # For AUR packages, check version
    aur_version = get_aur_version(package_name)
    
    # Use remote version checking if remote_dest is specified, otherwise use local
    if remote_dest:
        local_version = get_remote_version(package_name, remote_dest)
        location_desc = f"remote ({remote_dest})"
    else:
        local_version = get_local_version(package_name)
        location_desc = "locally"
    
    if local_version == '0':
        return True, f"Package not found {location_desc} (AUR: {aur_version})", '0'
    
    if aur_version == '0':
        return False, f"Package not found in AUR (Local: {local_version})", local_version
    
    # Simple version comparison (this could be improved with proper semver parsing)
    if aur_version != local_version:
        return True, f"Outdated (Local: {local_version}, AUR: {aur_version})", local_version
    
    return False, f"Up to date (Version: {local_version})", local_version

def main():
    global current_log_level
    
    parser = argparse.ArgumentParser(description='AUR Utility - Build and manage AUR packages')
    parser.add_argument('package', nargs='?', help='Package name or git URL to build (supports AUR packages, HTTP/HTTPS URLs, and SSH URLs)')
    parser.add_argument('-f', '--force', action='store_true', help='Force build even if up to date')
    parser.add_argument('--check-only', action='store_true', help='Only check versions, don\'t build')
    parser.add_argument('--debug', action='store_true', help='Show detailed output from makepkg and pacman commands (useful for manual debugging)')
    parser.add_argument('--no-cleanup', action='store_true', help='Don\'t clean up packages installed during build process')
    parser.add_argument('--cleanup-only', action='store_true', help='Only clean up tracked packages and exit')
    parser.add_argument('--remote-dest', type=str, help='SSH destination to check for existing packages (user@host:path)')
    parser.add_argument('--cleanup-old-versions', action='store_true', help='Remove old versions of packages, keeping only the latest version of each')
    parser.add_argument('--force-rebuild-all', action='store_true', help='Force rebuild all packages from targets.txt regardless of version')
    
    args = parser.parse_args()
    
    # Set log level based on --debug flag or LOG_LEVEL environment variable
    log_level_env = os.environ.get('LOG_LEVEL', '').lower()
    if args.debug or log_level_env == 'debug':
        current_log_level = LOG_LEVEL_DEBUG
    else:
        current_log_level = LOG_LEVEL_INFO
    
    # Register cleanup function (unless --no-cleanup is specified)
    if not args.no_cleanup:
        register_cleanup()
    
    log_debug(f"AUR Utility - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_debug("=" * 50)
    
    # Show remote dest mode if enabled
    if args.remote_dest:
        log_debug(f"Remote destination mode enabled: {args.remote_dest}")
        log_debug("Package versions will be checked against remote SSH destination")
        log_debug("=" * 50)
    
    # Set root directory for AUR package building
    set_root_directory()
    
    # Handle cleanup-only mode
    if args.cleanup_only:
        manual_cleanup()
        return
    
    # Handle cleanup old versions mode
    if args.cleanup_old_versions:
        log_info("Cleaning up old package versions...")
        cleanup_old_package_versions_local()
        cleanup_old_package_versions_remote()
        log_info("Old version cleanup complete")
        return
    
    try:
        if args.package:
            # Build specific package
            target = args.package
            
            # Check if the argument is a git URL
            if is_git_url(target):
                git_url = target
                package_name = extract_package_name_from_git_url(target)
                log_debug(f"Building package from git URL: {package_name} ({git_url})")
            else:
                package_name = target
                git_url = None
                log_debug(f"Building package: {package_name}")
            
            if not args.check_only:
                try:
                    version = build_package_native(package_name, debug=args.debug, git_url=git_url)
                    
                    # Track successful build
                    build_success_info.append({
                        'package': package_name,
                        'version': version,
                        'timestamp': datetime.now().isoformat()
                    })
                    
                    # Update repository and sync after individual package build
                    update_repository()
                    sync_packages()
                    # Also sync individually for recursive dependencies
                    sync_single_package(package_name)
                except Exception as e:
                    log_debug(f"Failed to build {package_name}: {e}")
                    # Don't exit here, let the failure reporting handle it
            else:
                # Just check version
                is_git_package = git_url is not None
                is_outdated, status, version = check_package_outdated(package_name, args.remote_dest, is_git_package=is_git_package, git_url=git_url, debug=args.debug)
                log_info(f"Package {package_name}: {status}")
        
        else:
            # Check all packages and rebuild outdated ones
            log_debug("Checking all packages for updates...")
            
            # SECURITY: Check for missing AUR packages and handle them
            handle_missing_aur_packages()
            
            # Get packages from targets.txt or existing packages (after removing missing ones)
            target_packages = get_packages_from_targets()
            if not target_packages:
                # get_existing_packages returns simple package names
                existing = get_existing_packages()
                # Convert to tuple format (package_name, None)
                target_packages = [(pkg, None) for pkg in existing]
            
            if not target_packages:
                log_info("No packages found in targets.txt or packages/ directory")
                log_info("Usage: python aurutil.py <package-name>")
                log_info("Or create a 'targets.txt' file with package names")
                sys.exit(1)
            
            log_debug(f"Found {len(target_packages)} packages to check")
            
            packages_to_build = []
            
            for package_name, git_url in target_packages:
                log_debug(f"\nChecking {package_name}...")
                is_git_package = git_url is not None
                is_outdated, status, version = check_package_outdated(package_name, args.remote_dest, is_git_package=is_git_package, git_url=git_url, debug=args.debug)
                log_debug(f"  {status}")
                
                if is_outdated or args.force or args.force_rebuild_all:
                    packages_to_build.append((package_name, git_url))
                else:
                    # Track packages that are already up-to-date
                    uptodate_package_info.append({
                        'package': package_name,
                        'version': version,
                        'timestamp': datetime.now().isoformat()
                    })
            
            if packages_to_build:
                log_debug(f"\nBuilding {len(packages_to_build)} outdated packages...")
                for package_name, git_url in packages_to_build:
                    log_debug(f"\n{'='*20} Building {package_name} {'='*20}")
                    try:
                        version = build_package_native(package_name, debug=args.debug, git_url=git_url)
                        
                        # Track successful build
                        build_success_info.append({
                            'package': package_name,
                            'version': version,
                            'timestamp': datetime.now().isoformat()
                        })
                        
                        # Sync after each package for recursive dependencies
                        sync_single_package(package_name)
                    except Exception as e:
                        log_debug(f"Failed to build {package_name}: {e}")
                        # Continue with next package instead of exiting
                
                # Update repository and sync
                update_repository()
                sync_packages()
            else:
                log_debug("\nAll packages are up to date!")
    
    finally:
        # Clean up any remaining directories
        cleanup_cloned_directories()
        
        # Clean up installed packages if not disabled
        if not args.no_cleanup:
            cleanup_installed_packages()
        
        # Report successful builds (in minimal mode)
        report_build_successes()
        
        # Report up-to-date packages
        report_uptodate_packages()
        
        # Report any AUR connectivity errors first
        has_aur_errors = report_aur_connectivity_errors()
        
        # Report any build failures
        has_build_failures = report_build_failures()
        
        # Exit with error if there were any failures
        if has_aur_errors or has_build_failures:
            sys.exit(1)

if __name__ == "__main__":
    main()
