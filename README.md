# aurdist

`aurdist` is a comprehensive Python tool for building and managing your own repository of AUR packages. It can run natively or in a containerized environment and automatically handles AUR package dependencies.

## Features
* **Single Python script** - Everything consolidated into `aurutil.py`
* **Automatic dependency resolution** - Detects and handles AUR package dependencies
* **Version checking** - Compares local packages with AUR versions
* **Cron-friendly** - Run with no arguments to check and rebuild outdated packages
* **Flexible building** - Native or Docker-based package building
* **Repository management** - Automatically updates pacman repository database
* **Remote syncing** - Optional rsync to web server directories

## Installation
Pacman dependencies: `sudo pacman -Sy --noconfirm base-devel pacman-contrib git docker pigz docker-buildx rsync curl jq python python-requests`

1. Clone repo on an Arch box w/ docker installed (ensure your user is in Docker group & has passwordless sudo)
2. Packages get built in `packages/` under the repo
3. If you'd like them to be rsync'd somewhere else, e.g. where nginx is expecting them, then do: `echo "PATH" > .where` (and ensure you've installed `rsync` from repos)

## Usage

### Basic Usage
```bash
# Check all packages and rebuild outdated ones (perfect for cron)
python aurutil.py

# Build a specific package
python aurutil.py google-chrome

# Force build a package even if up to date
python aurutil.py -f google-chrome

# Check versions only (don't build)
python aurutil.py --check-only

# Build natively instead of using Docker
python aurutil.py --native google-chrome
```

### Cron Setup
Add to your crontab to check and rebuild packages daily:
```bash
# Check and rebuild outdated packages every day at 2 AM
0 2 * * * cd /path/to/aurdist && python aurutil.py
```

### Package Management
Create a `targets.txt` file with package names (one per line) to specify which packages to track:
```
google-chrome
slack-desktop
visual-studio-code-bin
```

## Dependency Resolution

The build system automatically handles AUR package dependencies:

- **Dependency Detection**: Parses PKGBUILD files to identify all dependencies
- **Repository Checking**: Checks if dependencies are available in official Arch repositories  
- **AUR Validation**: Uses the AUR RPC API to verify AUR package availability
- **Smart Installation**: Installs official repo packages first, then builds AUR dependencies
- **Detailed Reporting**: Shows which dependencies are found where and any missing packages

## Pacman Config Examples
Local folder:
```
[aurdist]
SigLevel = Never
Server = file:///home/you/aurdist/packages
```

HTTP: (assuming localhost, otherwise use remote IP)
```
[aurdist]
SigLevel = Never
Server = http://127.0.0.1/
```