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

# Global tracking for cleanup
cloned_directories = set()
build_failures = []
installed_packages = set()  # Track packages installed during build process
root_directory = None  # Track the root directory for AUR package building
aur_connectivity_errors = []  # Track AUR connectivity failures
current_log_level = LOG_LEVEL_INFO  # Default to minimal output
build_success_info = []  # Track successful builds for reporting
uptodate_package_info = []  # Track packages that are already up-to-date

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
    """Check if a package is in the official repositories using pacman."""
    stdout, stderr = run_command(f"pacman -Si {package_name}", check=False)
    return stdout and "Repository" in stdout

def is_package_in_aur(package_name):
    """Check if a package exists in the AUR using the RPC interface."""
    response = aur_rpc_request_with_retry(f"{AUR_RPC_URL}{package_name}")
    if response:
        try:
            data = response.json()
            return data.get("resultcount", 0) > 0
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON response for {package_name}: {e}")
    return False

def get_aur_package_info(package_name):
    """Get detailed information about an AUR package."""
    response = aur_rpc_request_with_retry(f"{AUR_RPC_URL}{package_name}")
    if response:
        try:
            data = response.json()
            if data.get("resultcount", 0) > 0:
                return data["results"][0]
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON response for {package_name}: {e}")
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
        
        # Extract pkgver using regex
        # Match: pkgver=value or pkgver='value' or pkgver="value"
        match = re.search(r'^pkgver=[\'\"]?([^\'\"\n]+)[\'\"]?', content, re.MULTILINE)
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
        # Pattern: package-name-version-release-arch.pkg.tar.zst
        match = re.match(r"^(.+)-([^-]+)-([^-]+)-([^-]+)\.pkg\.tar\.zst$", pkg_file.name)
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
    
    # Generate HTML
    html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>aurdist - AUR Package Repository</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 1000px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
            border-bottom: 2px solid #0066cc;
            padding-bottom: 10px;
        }
        h2 {
            color: #444;
            margin-top: 30px;
        }
        code {
            background-color: #f4f4f4;
            padding: 2px 4px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
        }
        pre {
            background-color: #f8f8f8;
            padding: 15px;
            border-radius: 5px;
            overflow-x: auto;
            border-left: 4px solid #0066cc;
        }
        .note {
            background-color: #e7f3ff;
            padding: 15px;
            border-radius: 5px;
            border-left: 4px solid #0066cc;
            margin: 20px 0;
        }
        .package-list {
            margin: 20px 0;
        }
        .package-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
        }
        .package-table th {
            background-color: #0066cc;
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: bold;
        }
        .package-table td {
            padding: 10px 12px;
            border-bottom: 1px solid #ddd;
        }
        .package-table tr:hover {
            background-color: #f5f5f5;
        }
        .package-name {
            font-weight: bold;
            color: #0066cc;
        }
        .package-count {
            color: #666;
            font-style: italic;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>aurdist - AUR Package Repository</h1>
        
        <p>This is a pre-built repository of AUR (Arch User Repository) packages.</p>
        
        <h2>Usage</h2>
        <p>To use this repository with pacman, add the following to your <code>/etc/pacman.conf</code>:</p>
        
        <pre>[aurdist]
SigLevel = Never
Server = https://aur.mattcompton.dev/</pre>
        
        <p>Then update your package database:</p>
        <pre>sudo pacman -Sy</pre>
        
        <p>You can now install packages from this repository:</p>
        <pre>sudo pacman -S package-name</pre>
        
        <div class="note">
            <strong>Note:</strong> This repository is automatically updated when new package versions are available. The packages are built in an official Arch Linux container environment for compatibility.
        </div>
        
        <h2>Available Packages</h2>
        <div class="package-list">
'''
    
    if packages:
        html += f'            <p class="package-count">Total packages: {len(packages)}</p>\n'
        html += '            <table class="package-table">\n'
        html += '                <thead>\n'
        html += '                    <tr>\n'
        html += '                        <th>Package Name</th>\n'
        html += '                        <th>Version</th>\n'
        html += '                        <th>Arch</th>\n'
        html += '                        <th>Size</th>\n'
        html += '                        <th>File</th>\n'
        html += '                    </tr>\n'
        html += '                </thead>\n'
        html += '                <tbody>\n'
        
        for pkg in packages:
            html += '                    <tr>\n'
            html += f'                        <td class="package-name">{pkg["name"]}</td>\n'
            html += f'                        <td>{pkg["version"]}-{pkg["release"]}</td>\n'
            html += f'                        <td>{pkg["arch"]}</td>\n'
            html += f'                        <td>{pkg["size"]}</td>\n'
            html += f'                        <td><a href="{pkg["filename"]}">{pkg["filename"]}</a></td>\n'
            html += '                    </tr>\n'
        
        html += '                </tbody>\n'
        html += '            </table>\n'
    else:
        html += '            <p>No packages available yet.</p>\n'
    
    html += '''        </div>
        
        <h2>Repository Files</h2>
        <p>This repository contains:</p>
        <ul>
            <li><code>*.pkg.tar.zst</code> - Built package files</li>
            <li><code>aurdist.db.tar.zst</code> - Repository database</li>
            <li><code>aurdist.files.tar.zst</code> - File list database</li>
        </ul>
        
        <h2>Source Code</h2>
        <p>See the source code and build process at: <a href="https://github.com/SomethingGeneric/aurdist">https://github.com/SomethingGeneric/aurdist</a></p>
    </div>
</body>
</html>
'''
    
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

def check_package_outdated(package_name, remote_dest=None, is_git_package=False, git_url=None, debug=False):
    """Check if a package is outdated compared to AUR or git repository.
    
    Args:
        package_name: Name of the package to check
        remote_dest: Optional SSH destination for remote version checking
        is_git_package: True if this is a git URL package (skips AUR version check)
        git_url: Git URL for version checking (only used when is_git_package=True)
        debug: Enable debug output
        
    Returns:
        Tuple of (is_outdated: bool, status: str, version: str)
        where version is the current local/remote version
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
            
            # Get packages from targets.txt or existing packages
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
                
                if is_outdated or args.force:
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
