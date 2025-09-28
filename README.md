# aurdist

`aurdist` is a comprehensive Python tool for building and managing your own repository of AUR packages. It runs natively and automatically handles AUR package dependencies by pulling them with Pacman.

## Features
* **Single Python script** - Everything consolidated into `aurutil.py`
* **Automatic dependency resolution** - Detects and handles AUR package dependencies natively
* **Version checking** - Compares local packages with AUR versions
* **Cron-friendly** - Run with no arguments to check and rebuild outdated packages
* **Native building** - Builds packages directly on your system using Pacman
* **Repository management** - Automatically updates pacman repository database
* **Remote syncing** - Optional rsync to web server directories

## Installation
Pacman dependencies: `sudo pacman -Sy --noconfirm base-devel pacman-contrib git rsync curl jq python python-requests`

1. Clone repo on an Arch Linux system
2. Ensure your user has passwordless sudo access for package installation
3. Packages get built in `packages/` under the repo
4. If you'd like them to be rsync'd somewhere else, e.g. where nginx is expecting them, then do: `echo "PATH" > .where` (and ensure you've installed `rsync` from repos)

## Usage

The primary usage of this tool is through GitHub Actions, which automatically builds packages and serves them via my personal web server (aur.mattcompton.dev)

### Basic Usage
```bash
# Check all packages and rebuild outdated ones
python aurutil.py

# Build a specific package
python aurutil.py google-chrome

# Force build a package even if up to date
python aurutil.py -f google-chrome

# Check versions only (don't build)
python aurutil.py --check-only
```

### Package Management
Create a `targets.txt` file with package names (one per line) to specify which packages to track:
```
google-chrome
slack-desktop
visual-studio-code-bin
```

## Dependency Resolution

The build system automatically handles AUR package dependencies natively:

- **Dependency Detection**: Parses PKGBUILD files to identify all dependencies
- **Repository Checking**: Checks if dependencies are available in official Arch repositories  
- **AUR Validation**: Uses the AUR RPC API to verify AUR package availability
- **Native Installation**: Installs official repo packages with Pacman, then builds AUR dependencies natively
- **Detailed Reporting**: Shows which dependencies are found where and any missing packages

## Pacman Config Examples

**Hosted:**
```
[aurdist]
SigLevel = Never
Server = https://aur.mattcompton.dev
```

**Local folder:**
```
[aurdist]
SigLevel = Never
Server = file:///home/you/aurdist/packages
```

**HTTP (self-hosted):**
```
[aurdist]
SigLevel = Never
Server = http://your-server.com/path/to/packages/
```
