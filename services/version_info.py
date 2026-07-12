"""
Version and git information utilities for WheelHouse services.
"""
import subprocess
from pathlib import Path


def get_version() -> str:
    """Get the version from the root VERSION file."""
    try:
        # Try multiple paths to find the VERSION file
        search_paths = []
        
        # Add __file__ based paths if available  
        try:
            search_paths.extend([
                Path(__file__).parent.parent / "VERSION",  # From services/version_info.py -> root/VERSION
                Path(__file__).parent / "VERSION",          # From root/version_info.py -> root/VERSION  
            ])
        except NameError:
            pass  # __file__ not available in command line context
            
        # Add working directory based paths
        search_paths.extend([
            Path.cwd() / "VERSION",                      # Current working directory
            Path.cwd().parent / "VERSION",               # Parent of current working directory
            Path.cwd().parent.parent / "VERSION",        # GrandParent of current working directory
        ])
        
        for version_file in search_paths:
            if version_file.exists():
                return version_file.read_text().strip()
        
        return "unknown"
    except Exception:
        return "unknown"


def get_git_branch() -> str:
    """Get the current git branch name."""
    try:
        # Try multiple paths to find the git repository
        search_paths = []
        
        # Add __file__ based paths if available
        try:
            search_paths.extend([
                Path(__file__).parent.parent,  # From services/version_info.py -> root
                Path(__file__).parent,          # From root/version_info.py -> root  
            ])
        except NameError:
            pass  # __file__ not available in command line context
            
        # Add working directory based paths
        search_paths.extend([
            Path.cwd(),                     # Current working directory
            Path.cwd().parent,              # Parent of current working directory
            Path.cwd().parent.parent,       # GrandParent of current working directory
        ])
        
        for repo_path in search_paths:
            if (repo_path / ".git").exists():
                result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=repo_path,
                    timeout=5
                )
                if result.returncode == 0:
                    return result.stdout.strip()
        
        return "unknown"
    except Exception:
        return "unknown"


def get_version_info() -> str:
    """Get formatted version and branch information."""
    version = get_version()
    branch = get_git_branch()
    return f"v{version} (branch: {branch})"


def get_startup_banner(service_name: str) -> str:
    """Get a formatted startup banner with version information."""
    version_info = get_version_info()
    return f"{service_name} {version_info}"