# AUR Distribution Tool (aurdist)

Always reference these instructions first and fallback to search or bash commands only when you encounter unexpected information that does not match the info here.

aurdist is a comprehensive Python tool for building and managing your own repository of AUR (Arch User Repository) packages. It handles automatic dependency resolution, version checking, and repository management natively on Arch Linux systems.

## CRITICAL REQUIREMENTS

**ARCH LINUX ONLY**: This tool ONLY works on Arch Linux systems. Do NOT attempt to run builds on Ubuntu, Debian, or other distributions.

**NETWORK ACCESS REQUIRED**: The tool requires internet access to:
- Query the AUR RPC API at `aur.archlinux.org`
- Clone git repositories from `https://aur.archlinux.org/`
- If network is blocked, commands will fail with DNS resolution errors

## Working Effectively

### Bootstrap and Setup (Arch Linux Only)
**NEVER CANCEL**: Setup and builds can take 15-45 minutes depending on package complexity. Set timeout to 60+ minutes.

1. **Install dependencies** (REQUIRED - takes 2-5 minutes):
   ```bash
   sudo pacman -Sy --noconfirm base-devel pacman-contrib git rsync curl jq python python-requests
   ```

2. **Ensure passwordless sudo** (REQUIRED):
   ```bash
   # Test this works without password prompt:
   sudo pacman -Q base-devel
   ```

3. **Verify network access** (REQUIRED):
   ```bash
   curl -s "https://aur.archlinux.org/rpc/?v=5&type=info&arg%5B%5D=google-chrome" | jq .
   ```

### Package Management Workflow

4. **Create targets file** (recommended):
   ```bash
   # Create targets.txt with one package name per line
   echo "google-chrome" > targets.txt
   echo "slack-desktop" >> targets.txt
   ```

5. **Test basic functionality** (takes 30-60 seconds):
   ```bash
   python3 aurutil.py --check-only
   ```

### Build Operations
**NEVER CANCEL**: Build operations take 15-45 minutes per package. ALWAYS set timeout to 60+ minutes minimum.

6. **Check all packages for updates** (takes 1-2 minutes):
   ```bash
   python3 aurutil.py --check-only
   ```

7. **Build outdated packages** (takes 15-45 minutes total):
   ```bash
   python3 aurutil.py
   ```

8. **Build specific package** (takes 15-30 minutes per package):
   ```bash
   python3 aurutil.py google-chrome
   ```

9. **Force rebuild package** (takes 15-30 minutes):
   ```bash
   python3 aurutil.py -f google-chrome
   ```

10. **Debug build issues** (takes 15-30+ minutes):
    ```bash
    python3 aurutil.py --debug google-chrome
    ```

### Repository Management

11. **Setup remote sync** (optional):
    ```bash
    echo "/var/www/html/repo" > .where
    # Packages will be rsync'd to this location after builds
    ```

12. **Configure pacman to use local repository**:
    ```bash
    # Add to /etc/pacman.conf:
    [aurdist]
    SigLevel = Never
    Server = file:///path/to/aurdist/packages
    ```

## Validation Scenarios

**MANUAL VALIDATION REQUIREMENT**: After making changes, ALWAYS test these scenarios:

### Basic Functionality Test (Safe on any system)
```bash
# Should complete in 30-60 seconds - these work on any system
python3 aurutil.py --help
python3 aurutil.py --cleanup-only
bash -n cron.sh  # Validate cron script syntax
ls -la  # Verify all key files exist
wc -l aurutil.py  # Should show 744 lines
```

### Network-Dependent Tests (Arch Linux + Internet Required)
```bash
# These require internet access - will fail with DNS errors if blocked
python3 aurutil.py --check-only  # Takes 1-2 minutes
curl -s "https://aur.archlinux.org/rpc/?v=5&type=info&arg%5B%5D=htop" | jq .
```

### End-to-End Package Build Test (Arch Linux Only)
**NEVER CANCEL**: This takes 15-30 minutes. Set timeout to 60+ minutes.
```bash
# ONLY run on Arch Linux with internet access
echo "htop" > test-targets.txt
cp test-targets.txt targets.txt
python3 aurutil.py --check-only
python3 aurutil.py htop --debug
ls -la packages/  # Should contain built packages
```

### Repository Validation (After successful builds)
```bash
# Verify repository database creation
ls -la packages/aurdist.db*  # Should exist after builds
ls -la packages/*.pkg.tar.zst  # Should contain package files
pacman -Sl aurdist  # Should list packages if configured
```

## Timing Expectations and Timeouts

**CRITICAL**: Always use these minimum timeout values based on actual testing:

### Quick Operations (30-120 seconds)
- **Help/syntax checks**: 1-5 seconds
- **Cleanup operations**: 5-30 seconds
- **Package info queries**: 10-60 seconds (network dependent)
- **Version checking only**: 30-120 seconds total

### Medium Operations (2-15 minutes)
- **Dependency analysis**: 2-5 minutes per package
- **Repository database update**: 2-5 minutes
- **Setup and verification**: 2-5 minutes

### Long Operations (15-60+ minutes) - NEVER CANCEL
- **Single package build**: 15-45 minutes (includes dependencies)
- **Multiple package builds**: 15-60+ minutes (multiply by package count)
- **Complex dependency resolution**: 10-30 minutes additional

**NEVER CANCEL these critical operations**:
- `makepkg -sf --noconfirm` (building packages)
- `makepkg -si --noconfirm` (building + installing dependencies)
- `sudo pacman -S --noconfirm` (installing dependencies)
- `git clone https://aur.archlinux.org/` (cloning packages)
- `repo-add -vn aurdist.db.tar.zst` (repository database updates)

### Actual Command Examples with Timeouts
```bash
# Set these timeout values in your automation:
timeout 60 python3 aurutil.py --help                    # 60 seconds
timeout 120 python3 aurutil.py --check-only             # 2 minutes
timeout 300 python3 aurutil.py --cleanup-only           # 5 minutes
timeout 3600 python3 aurutil.py package-name            # 60 minutes
timeout 3600 python3 aurutil.py --debug package-name    # 60 minutes
timeout 7200 python3 aurutil.py                         # 120 minutes for all packages
```

## Common Issues and Limitations

### Network Issues (Most Common)
```bash
# If you see these errors, network access is blocked:
# "Could not resolve host: aur.archlinux.org"
# "HTTPSConnectionPool(host='aur.archlinux.org', port=443): Max retries exceeded"
# "NameResolutionError: Failed to resolve 'aur.archlinux.org'"
# "fatal: unable to access 'https://aur.archlinux.org/'"

# Network-dependent operations that will fail:
# - All build operations (require git clone from AUR)
# - Version checking (requires AUR RPC API access)
# - Only --help and --cleanup-only work without network
```

### Non-Arch Systems
```bash
# On Ubuntu/Debian/etc., you'll see:
# "pacman: command not found"
# "makepkg: command not found"
# "repo-add: command not found"
# DO NOT attempt to build - this tool requires Arch Linux

# Commands that work on any system:
python3 aurutil.py --help
python3 aurutil.py --cleanup-only
bash -n cron.sh
```

### Permission Issues
```bash
# If sudo prompts for password during builds:
# Configure passwordless sudo for your user
sudo visudo  # Add: username ALL=(ALL) NOPASSWD: ALL

# Test with:
sudo pacman -Q base-devel  # Should work without password prompt
```

### Build Failures
```bash
# Common build failure patterns:
# "BUILD FAILURE for <package>: Command failed: 'makepkg -sf --noconfirm'"
# "Error building package <package>: [specific error]"
# "PKGBUILD not found for <package>"

# Always check build failure report at end of run
# Use --debug flag for detailed output
```

## Key Files and Structure

### Repository Root Structure
```
aurutil.py          # Main Python script (744 lines)
targets.txt         # Package list (one per line)
cron.sh            # Cron wrapper script
packages/          # Built packages directory
.where             # Optional remote sync destination
.gitignore         # Excludes packages/* and .where
README.md          # Project documentation
```

### Generated Files (after builds)
```
packages/
├── *.pkg.tar.zst     # Built package files
├── aurdist.db*       # Repository database files
└── README.html       # Repository index
```

### Key Functions in aurutil.py
- `build_package_native()` - Main build function (line 520)
- `check_and_install_dependencies()` - Dependency resolution (line 446)
- `update_repository()` - Repository database management (line 552)
- `sync_packages()` - Remote sync functionality (line 581)

## Command Reference

### Development Commands
```bash
python3 aurutil.py --help                    # Show help
python3 aurutil.py --check-only              # Check versions only
python3 aurutil.py --cleanup-only            # Clean up tracked packages
python3 aurutil.py --debug <package>         # Build with debug output
python3 aurutil.py --no-cleanup              # Don't auto-cleanup
python3 aurutil.py -f <package>              # Force rebuild
```

### Cron Usage
```bash
# Use cron.sh for automated builds
./cron.sh  # Equivalent to: python3 aurutil.py
```

## IMPORTANT: This Tool Cannot Be "Built" or "Tested" in Traditional Sense

- **No unit tests exist** - validation is done through actual package builds
- **No build system** - it's a single Python script with 744 lines
- **No linting configured** - uses standard Python 3 syntax
- **Environment dependent** - requires Arch Linux with specific tools
- **No CI/CD pipeline** - designed for manual/cron execution

### What CAN Be Validated (Any System)
```bash
# These commands work on any system and validate syntax/basic functionality:
python3 --version          # Verify Python 3 available
python3 aurutil.py --help  # Validate script syntax and help
python3 aurutil.py --cleanup-only  # Test basic execution path
bash -n cron.sh           # Validate shell script syntax
ls -la                    # Verify file structure
wc -l aurutil.py         # Should show 744 lines
grep -c "def " aurutil.py # Should show ~20 functions
```

### What CANNOT Be Validated (Without Arch Linux + Internet)
```bash
# These operations require Arch Linux environment and network access:
python3 aurutil.py --check-only     # Requires AUR API access
python3 aurutil.py package-name     # Requires makepkg/pacman/git
pacman -Q                          # Requires pacman
makepkg --version                  # Requires makepkg
repo-add --help                    # Requires pacman-contrib
```

## Emergency Procedures

### Clean Up Failed Builds
```bash
python3 aurutil.py --cleanup-only
# Manually remove temp directories if needed:
rm -rf <package-name>/  # Any cloned AUR directories
```

### Reset Repository
```bash
rm -rf packages/*  # Remove all built packages
# Repository will be recreated on next build
```

### Debug Build Failures
```bash
python3 aurutil.py --debug <package-name>
# Check output for makepkg or dependency errors
# Examine PKGBUILD in cloned directory
```