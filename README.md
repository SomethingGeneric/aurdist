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

The build system automatically handles AUR package dependencies natively:

- **Dependency Detection**: Parses PKGBUILD files to identify all dependencies
- **Repository Checking**: Checks if dependencies are available in official Arch repositories  
- **AUR Validation**: Uses the AUR RPC API to verify AUR package availability
- **Native Installation**: Installs official repo packages with Pacman, then builds AUR dependencies natively
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

## GitHub Actions

The repository includes a GitHub Actions workflow that automatically builds all target AUR packages using an official Arch Linux container. The workflow:

- Builds all packages listed in `targets.txt`
- Handles AUR dependencies automatically
- Creates a complete pacman-compatible repository with `repo-add`
- Uploads the repository as a downloadable artifact

### Using the Workflow Artifact

1. Go to the **Actions** tab in the GitHub repository
2. Select a successful workflow run
3. Download the `aurdist-packages` artifact
4. Extract the artifact on your Arch system
5. Add the repository to your `/etc/pacman.conf`:

```
[aurdist]
SigLevel = Never
Server = file:///path/to/extracted/packages
```

6. Run `pacman -Sy` to sync the repository
7. Install packages with `pacman -S package-name`