#!/usr/bin/env python3
"""
Sync script that mirrors source directory to destination directory.
Similar to rsync --archive --delete behavior.
"""

import os
import sys
import shutil
import stat
import argparse
from pathlib import Path
from typing import Optional, Set, Tuple


def get_relative_paths(root: Path, follow_symlinks: bool = False) -> Set[Path]:
    """Get all relative paths under root directory."""
    paths = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        rel_dir = Path(dirpath).relative_to(root)

        # Add directory itself (except root)
        if rel_dir != Path("."):
            paths.add(rel_dir)

        # Add all files
        for filename in filenames:
            paths.add(rel_dir / filename)

    return paths


def should_copy(src: Path, dst: Path, time_tolerance: float = 2.0) -> bool:
    """Determine if file should be copied based on modification time and size."""
    try:
        if not dst.exists():
            return True

        # Don't copy if destination is a symlink (will be handled separately)
        if dst.is_symlink():
            return True

        src_stat = src.lstat()  # Use lstat to not follow symlinks
        dst_stat = dst.lstat()

        # Copy if size differs or modification time differs
        if src_stat.st_size != dst_stat.st_size:
            return True

        # Use configurable tolerance for modification time (default 2s for FAT32 compatibility)
        if abs(src_stat.st_mtime - dst_stat.st_mtime) > time_tolerance:
            return True

        return False
    except OSError as e:
        print(
            f"Warning: Could not stat file {src} or {dst} for comparison: {e}. Assuming copy is needed.",
            file=sys.stderr,
        )
        return True


def copy_with_metadata(src: Path, dst: Path):
    """Copy file and preserve all metadata."""
    try:
        # Handle symbolic links
        if src.is_symlink():
            # Remove destination if it exists
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            # Create symlink pointing to the same target
            link_target = os.readlink(src)
            os.symlink(link_target, dst)
            return

        # Copy file content and most metadata
        shutil.copy2(src, dst)

        # Explicitly preserve access time
        src_stat = src.stat()
        try:
            os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))
        except OSError:
            pass  # Some filesystems don't support setting times

        # Set permissions (handle platform differences)
        try:
            dst.chmod(stat.S_IMODE(src_stat.st_mode))
        except (OSError, NotImplementedError):
            pass  # Windows may not support all permission operations

    except OSError as e:
        print(
            f"Error: Failed to copy {src} to {dst} or preserve metadata: {e}",
            file=sys.stderr,
        )
        raise


def is_path_inside(child: Path, parent: Path) -> bool:
    """Check if child path is inside parent path."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def sync_directories(
    source: Path,
    destination: Path,
    verbose: bool = True,
    dry_run: bool = False,
    exclude_patterns: Optional[Set[str]] = None,
    time_tolerance: float = 2.0,
) -> Tuple[int, int, int, int]:
    """
    Sync source directory to destination, deleting files not in source.

    Args:
        source (Path): Source directory path
        destination (Path): Destination directory path
        verbose (bool): Enable verbose output
        dry_run (bool): Show changes without making them
        exclude_patterns (Set[str]): Patterns to exclude
        time_tolerance (float): Time difference tolerance in seconds

    Returns: 
        Tuple[int, int, int, int]: (created_dirs, copied_files, deleted_items, failed_copies)
    
    Raises:
        SystemExit: If source/destination are invalid or overlap
    """

    if not source.exists():
        print(f"Error: Source directory '{source}' does not exist", file=sys.stderr)
        sys.exit(1)

    if not source.is_dir():
        print(f"Error: Source '{source}' is not a directory", file=sys.stderr)
        sys.exit(1)

    # Check for dangerous path overlaps
    if destination == source:
        print(f"Error: Source and destination are the same", file=sys.stderr)
        sys.exit(1)

    if is_path_inside(destination, source):
        print(f"Error: Destination is inside source directory", file=sys.stderr)
        sys.exit(1)

    if is_path_inside(source, destination):
        print(f"Error: Source is inside destination directory", file=sys.stderr)
        sys.exit(1)

    # Create destination if it doesn't exist
    if not destination.exists():
        if not dry_run:
            try:
                destination.mkdir(parents=True, exist_ok=True)
                if verbose:
                    print(f"Created destination directory: {destination}")
            except OSError as e:
                print(
                    f"Error: Failed to create destination directory {destination}: {e}",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            if verbose:
                print(f"[DRY RUN] Would create destination directory: {destination}")

    # Get all relative paths in source and destination
    if verbose:
        print("\nScanning directories...")

    source_paths = get_relative_paths(source, follow_symlinks=False)
    dest_paths = (
        get_relative_paths(destination, follow_symlinks=False)
        if destination.exists()
        else set()
    )

    # Apply exclusion patterns if provided
    if exclude_patterns:
        source_paths = {
            p
            for p in source_paths
            if not any(pat in str(p) for pat in exclude_patterns)
        }

    # Delete files/dirs in destination that aren't in source
    paths_to_delete = dest_paths - source_paths
    deleted_count = 0

    if paths_to_delete:
        if verbose:
            print(
                f"\n{'[DRY RUN] Would delete' if dry_run else 'Deleting'} {len(paths_to_delete)} items not in source..."
            )

        # Sort by depth (deepest first) to delete files before their parent directories
        sorted_deletes = sorted(
            paths_to_delete, key=lambda p: len(p.parts), reverse=True
        )

        for rel_path in sorted_deletes:
            dst_path = destination / rel_path
            if dst_path.exists() or dst_path.is_symlink():
                try:
                    if dry_run:
                        if verbose:
                            item_type = "directory" if dst_path.is_dir() else "file"
                            print(f"  [DRY RUN] Would delete {item_type}: {rel_path}")
                        deleted_count += 1
                    else:
                        # Only delete empty directories or individual files
                        # Don't use rmtree - let directories be removed only when empty
                        if dst_path.is_dir() and not dst_path.is_symlink():
                            try:
                                dst_path.rmdir()  # Only removes if empty
                                if verbose:
                                    print(f"  Deleted directory: {rel_path}")
                                deleted_count += 1
                            except OSError:
                                # Directory not empty, skip (contents will be synced)
                                pass
                        else:
                            dst_path.unlink()
                            if verbose:
                                print(f"  Deleted file: {rel_path}")
                            deleted_count += 1
                except OSError as e:
                    print(f"Error: Failed to delete {dst_path}: {e}", file=sys.stderr)

    # Copy/update files and create directories
    copied_count = 0
    created_dirs = 0
    failed_copies = 0
    directories_to_update = []  # Track directories for mtime updates after file ops

    if verbose and not dry_run:
        print(f"\nSyncing files...")
    elif verbose and dry_run:
        print(f"\n[DRY RUN] Changes that would be made:")

    for rel_path in sorted(source_paths):
        src_path = source / rel_path
        dst_path = destination / rel_path

        if src_path.is_dir() and not src_path.is_symlink():
            if not dst_path.exists():
                if dry_run:
                    if verbose:
                        print(f"  [DRY RUN] Would create directory: {rel_path}")
                    created_dirs += 1
                else:
                    try:
                        dst_path.mkdir(parents=True, exist_ok=True)
                        created_dirs += 1
                        if verbose:
                            print(f"  Created directory: {rel_path}")
                    except OSError as e:
                        print(
                            f"Error: Failed to create directory {dst_path}: {e}",
                            file=sys.stderr,
                        )

            # Track directory for mtime update after all files are copied
            if not dry_run and dst_path.exists():
                directories_to_update.append((src_path, dst_path))

        else:  # It's a file or symlink
            if should_copy(src_path, dst_path, time_tolerance):
                if dry_run:
                    if verbose:
                        action = "create" if not dst_path.exists() else "update"
                        item_type = "symlink" if src_path.is_symlink() else "file"
                        print(f"  [DRY RUN] Would {action} {item_type}: {rel_path}")
                    copied_count += 1
                else:
                    try:
                        # Ensure parent directory exists before copying file
                        if not dst_path.parent.exists():
                            dst_path.parent.mkdir(parents=True, exist_ok=True)
                        copy_with_metadata(src_path, dst_path)
                        copied_count += 1
                        if verbose:
                            print(f"  Copied: {rel_path}")
                    except Exception as e:
                        failed_copies += 1
                        print(f"Error: Failed to copy {src_path}: {e}", file=sys.stderr)

    # Update directory permissions and mtimes after all file operations
    # This ensures directory mtimes aren't changed by subsequent file copies
    if not dry_run and directories_to_update:
        for src_path, dst_path in directories_to_update:
            try:
                src_stat = src_path.stat()
                dst_path.chmod(stat.S_IMODE(src_stat.st_mode))
                # Preserve directory modification time
                os.utime(dst_path, (src_stat.st_atime, src_stat.st_mtime))
            except (OSError, NotImplementedError):
                pass  # Ignore permission errors on some platforms
    
    # Update root destination directory mtime to match source
    if not dry_run and destination.exists():
        try:
            src_stat = source.stat()
            destination.chmod(stat.S_IMODE(src_stat.st_mode))
            os.utime(destination, (src_stat.st_atime, src_stat.st_mtime))
        except (OSError, NotImplementedError):
            pass

    return created_dirs, copied_count, deleted_count, failed_copies


def main():
    parser = argparse.ArgumentParser(
        description="Sync source directory to destination (rsync-style with --delete)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Interactive mode
  %(prog)s /src /dst                # Sync /src to /dst
  %(prog)s /src /dst --dry-run      # Show what would change
  %(prog)s /src /dst --exclude .git # Exclude .git directories
        """,
    )
    parser.add_argument("source", nargs="?", help="Source directory path")
    parser.add_argument("destination", nargs="?", help="Destination directory path")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=True,
        help="Verbose output (default: True)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Minimal output")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude paths containing this pattern (can be used multiple times)",
    )
    parser.add_argument(
        "--time-tolerance",
        type=float,
        default=2.0,
        help="Time difference tolerance in seconds (default: 2.0 for FAT32 compatibility)",
    )

    args = parser.parse_args()

    verbose = args.verbose and not args.quiet
    exclude_patterns = set(args.exclude)
    dry_run = args.dry_run  # Initialize from args, may be overridden in interactive mode

    # Common patterns that users might want to exclude
    COMMON_EXCLUDES = [
        ".DS_Store",           # macOS metadata
        ".Spotlight-V100",     # macOS Spotlight
        ".Trashes",            # macOS trash
        "Thumbs.db",           # Windows thumbnails
        "desktop.ini",         # Windows folder settings
        "$RECYCLE.BIN",        # Windows recycle bin
        ".fseventsd",          # macOS file system events
        ".TemporaryItems",     # macOS temporary items
        ".VolumeIcon.icns",    # macOS volume icon
        "VT100.trash",         # VT100 trash
        ".git",                # Git repository
        ".svn",                # SVN repository
        "__pycache__",         # Python cache
        ".pyc",                # Python compiled files
        "node_modules",        # Node.js dependencies
        ".idea",               # JetBrains IDE
        ".vscode",             # VS Code settings
    ]

    # Interactive mode if no arguments provided
    if args.source is None or args.destination is None:
        print("=" * 60)
        print("Directory Sync Tool (rsync-style)")
        print("=" * 60)
        print("\nThis will sync the source directory to the destination.")
        print("Files in destination not in source will be DELETED.")
        print("Do NOT change any files in the source or destination during the sync.")
        print()

        # Get source directory
        while True:
            source_input = input("Enter source directory path: ").strip()
            if not source_input:
                print("Source path cannot be empty. Please try again.", file=sys.stderr)
                continue

            source = Path(source_input).expanduser().resolve()

            if not source.exists():
                print(
                    f"Error: '{source}' does not exist. Please try again.",
                    file=sys.stderr,
                )
                continue

            if not source.is_dir():
                print(
                    f"Error: '{source}' is not a directory. Please try again.",
                    file=sys.stderr,
                )
                continue

            break

        # Get destination directory
        while True:
            dest_input = input("Enter destination directory path: ").strip()
            if not dest_input:
                print(
                    "Destination path cannot be empty. Please try again.",
                    file=sys.stderr,
                )
                continue

            destination = Path(dest_input).expanduser().resolve()

            # Prevent syncing a directory into itself
            if (
                destination == source
                or is_path_inside(destination, source)
                or is_path_inside(source, destination)
            ):
                print(
                    "Error: Source and destination cannot overlap. Please try again.",
                    file=sys.stderr,
                )
                continue

            break

        # Ask about exclusions
        print("\n" + "=" * 60)
        print("File Exclusions")
        print("=" * 60)
        exclude_input = input("\nWould you like to exclude common system/cache files? (yes/no): ").strip().lower()
        
        if exclude_input in ["yes", "y"]:
            print("\nCommon exclusion patterns:")
            for i, pattern in enumerate(COMMON_EXCLUDES, 1):
                print(f"  {i:2d}. {pattern}")
            
            print("\nOptions:")
            print("  - Press Enter to exclude ALL patterns above")
            print("  - Enter pattern numbers (e.g., '1,3,5' or '1 3 5') for specific exclusions")
            print("  - Enter 'none' to skip exclusions")
            print("  - Enter 'custom' to add your own patterns")
            
            exclusion_choice = input("\nYour choice: ").strip().lower()
            
            if exclusion_choice == "":
                # Exclude all common patterns
                exclude_patterns.update(COMMON_EXCLUDES)
                print(f"Excluding all {len(COMMON_EXCLUDES)} common patterns.")
            elif exclusion_choice == "none":
                print("No exclusions selected.")
            elif exclusion_choice == "custom":
                print("\nEnter custom patterns to exclude (one per line, empty line to finish):")
                while True:
                    custom_pattern = input("  Pattern: ").strip()
                    if not custom_pattern:
                        break
                    exclude_patterns.add(custom_pattern)
                    print(f"  Added: {custom_pattern}")
            else:
                # Parse pattern numbers
                try:
                    # Handle both comma and space separation
                    numbers = exclusion_choice.replace(',', ' ').split()
                    for num in numbers:
                        idx = int(num) - 1
                        if 0 <= idx < len(COMMON_EXCLUDES):
                            exclude_patterns.add(COMMON_EXCLUDES[idx])
                        else:
                            print(f"Warning: Invalid number {num}, skipping.", file=sys.stderr)
                    print(f"Excluding {len(exclude_patterns)} pattern(s).")
                except ValueError:
                    print("Invalid input, no exclusions added.", file=sys.stderr)
        
        # Ask if user wants to add custom patterns
        if exclude_input not in ["yes", "y"]:
            custom_input = input("\nWould you like to add custom exclusion patterns? (yes/no): ").strip().lower()
            if custom_input in ["yes", "y"]:
                print("\nEnter custom patterns to exclude (one per line, empty line to finish):")
                while True:
                    custom_pattern = input("  Pattern: ").strip()
                    if not custom_pattern:
                        break
                    exclude_patterns.add(custom_pattern)
                    print(f"  Added: {custom_pattern}")

        # Show summary and confirm
        print("\n" + "=" * 60)
        print(f"Source:      {source}")
        print(f"Destination: {destination}")
        print("=" * 60)
        print("\nWARNING: This will DELETE files in destination not in source!")

        # Ask about dry-run
        dry_run_input = input("\nRun in dry-run mode (preview changes without applying)? (yes/no): ").strip().lower()
        dry_run = dry_run_input in ["yes", "y"]

        if dry_run:
            print("\n[DRY RUN MODE - No changes will be made]")
        else:
            confirm = input("\nProceed with sync? (yes/no): ").strip().lower()
            if confirm not in ["yes", "y"]:
                print("Sync cancelled.")
                sys.exit(0)
    else:
        source = Path(args.source).expanduser().resolve()
        destination = Path(args.destination).expanduser().resolve()

        if verbose:
            print("=" * 60)
            print("Directory Sync Tool")
            print("=" * 60)
            print(f"\nSource:      {source}")
            print(f"Destination: {destination}")
            if args.dry_run:
                print("\n[DRY RUN MODE - No changes will be made]")
            if exclude_patterns:
                print(f"Excluding:   {', '.join(exclude_patterns)}")
            print("=" * 60)

    # Perform sync
    created_dirs, copied_files, deleted_items, failed_copies = sync_directories(
        source,
        destination,
        verbose=verbose,
        dry_run=dry_run,
        exclude_patterns=exclude_patterns,
        time_tolerance=args.time_tolerance,
    )

    # Print summary
    print(
        f"\n{'[DRY RUN] ' if dry_run else ''}Sync {'would be' if dry_run else ''} complete!"
    )
    print(f"  Directories created: {created_dirs}")
    print(f"  Files copied/updated: {copied_files}")
    print(f"  Items deleted: {deleted_items}")

    if failed_copies > 0:
        print(f"  Files that failed to copy: {failed_copies}", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print("\nNo changes were made. Run without --dry-run to apply changes.")


if __name__ == "__main__":
    main()

