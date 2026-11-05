import os
import shutil
import hashlib

# Deleted items are moved here rather than permanently removed, so a bad
# click during review is always recoverable.
TRASH_DIR = os.path.join(os.path.expanduser("~"), ".agent_trash")

# Safety cap so a huge folder doesn't hash forever or spawn a 5,000-row UI.
MAX_FILES_TO_HASH = 5000
LARGE_FILE_MB_THRESHOLD = 100


def get_file_hash(filepath: str) -> str:
    """Generate SHA256 hash to detect duplicate files safely."""
    hasher = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            while chunk := f.read(8192):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return ""


def _human_mb(mb: float) -> str:
    if mb >= 1024:
        return f"{round(mb / 1024, 2)} GB"
    return f"{round(mb, 1)} MB"


def scan_storage(target_dir: str = None) -> dict:
    """Scan target_dir for duplicates and large files. Returns a dict with
    disk totals and two review lists:
      duplicates: [(dup_path, original_path, size_mb), ...]
      large_files: [(path, size_mb), ...]
    """
    if not target_dir:
        target_dir = os.path.expanduser("~/Downloads")
    target_dir = os.path.expanduser(target_dir)

    total, used, free = shutil.disk_usage("/")
    total_gb = round(total / (1024 ** 3), 2)
    free_gb = round(free / (1024 ** 3), 2)

    seen_hashes = {}   # hash -> first path seen (the "original")
    duplicates = []     # (dup_path, original_path, size_mb)
    large_files = []    # (path, size_mb)

    if not os.path.isdir(target_dir):
        return {
            "total_gb": total_gb, "free_gb": free_gb, "target_dir": target_dir,
            "duplicates": [], "large_files": [], "error": f"'{target_dir}' is not a valid directory."
        }

    hashed_count = 0
    for root, _, files in os.walk(target_dir):
        for file in files:
            filepath = os.path.join(root, file)
            try:
                size_mb = os.path.getsize(filepath) / (1024 * 1024)
                if size_mb > LARGE_FILE_MB_THRESHOLD:
                    large_files.append((filepath, round(size_mb, 1)))

                if hashed_count >= MAX_FILES_TO_HASH:
                    continue
                file_hash = get_file_hash(filepath)
                hashed_count += 1
                if file_hash:
                    if file_hash in seen_hashes:
                        duplicates.append((filepath, seen_hashes[file_hash], round(size_mb, 1)))
                    else:
                        seen_hashes[file_hash] = filepath
            except Exception:
                continue

    return {
        "total_gb": total_gb, "free_gb": free_gb, "target_dir": target_dir,
        "duplicates": duplicates, "large_files": large_files, "error": None
    }


def _move_to_trash(path: str) -> bool:
    try:
        os.makedirs(TRASH_DIR, exist_ok=True)
        dest = os.path.join(TRASH_DIR, os.path.basename(path))
        base, ext = os.path.splitext(dest)
        i = 1
        while os.path.exists(dest):
            dest = f"{base}_{i}{ext}"
            i += 1
        shutil.move(path, dest)
        return True
    except Exception as e:
        print(f"[Warning] could not move '{path}' to trash: {e}")
        return False


def _cli_review(duplicates, large_files):
    """Text-based fallback review when no display/tkinter is available."""
    deleted = []
    freed_bytes = 0

    if duplicates:
        print(f"\n--- Duplicate files ({len(duplicates)}) — original is always kept ---")
        for dup_path, original_path, size_mb in duplicates:
            ans = input(f"Move to trash? '{dup_path}' ({size_mb} MB, dup of {os.path.basename(original_path)}) [y/N]: ").strip().lower()
            if ans == "y":
                if _move_to_trash(dup_path):
                    deleted.append(dup_path)
                    freed_bytes += size_mb * 1024 * 1024

    if large_files:
        print(f"\n--- Large files ({len(large_files)}) — review manually ---")
        for path, size_mb in large_files:
            ans = input(f"Move to trash? '{path}' ({size_mb} MB) [y/N]: ").strip().lower()
            if ans == "y":
                if _move_to_trash(path):
                    deleted.append(path)
                    freed_bytes += size_mb * 1024 * 1024

    return deleted, freed_bytes


def _gui_review(duplicates, large_files):
    """Tkinter review window with checkboxes and a 'Move Selected to Trash'
    button. Falls back to the CLI reviewer if no display is available."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
    except Exception:
        return _cli_review(duplicates, large_files)

    deleted = []
    freed_bytes = 0

    root.title("Storage Optimizer — Review Files")
    root.geometry("700x520")

    tk.Label(root, text="Review flagged files, then choose what to move to Trash",
             font=("Segoe UI", 11, "bold")).pack(pady=8)

    # Scrollable list area
    outer = tk.Frame(root)
    outer.pack(fill="both", expand=True, padx=10, pady=5)
    canvas = tk.Canvas(outer, borderwidth=0)
    scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    checks = []  # (BooleanVar, filepath, size_mb)

    if duplicates:
        tk.Label(inner, text=f"Duplicate files ({len(duplicates)}) — safe to remove, original is kept:",
                 anchor="w", fg="#333", font=("Segoe UI", 10, "bold")).pack(fill="x", pady=(6, 2))
        for dup_path, original_path, size_mb in duplicates:
            var = tk.BooleanVar(value=True)  # pre-checked: these are safe duplicates
            label = f"{dup_path}   ({size_mb} MB)   [dup of {os.path.basename(original_path)}]"
            tk.Checkbutton(inner, text=label, variable=var, anchor="w", justify="left",
                            wraplength=640).pack(fill="x")
            checks.append((var, dup_path, size_mb))

    if large_files:
        tk.Label(inner, text=f"Large files ({len(large_files)}) — review manually, not pre-selected:",
                 anchor="w", fg="#333", font=("Segoe UI", 10, "bold")).pack(fill="x", pady=(12, 2))
        for path, size_mb in large_files:
            var = tk.BooleanVar(value=False)
            label = f"{path}   ({size_mb} MB)"
            tk.Checkbutton(inner, text=label, variable=var, anchor="w", justify="left",
                            wraplength=640).pack(fill="x")
            checks.append((var, path, size_mb))

    if not checks:
        tk.Label(inner, text="Nothing flagged — storage looks clean!").pack(pady=20)

    status_var = tk.StringVar(value="")
    tk.Label(root, textvariable=status_var, fg="#2e7d32").pack()

    def do_delete():
        nonlocal freed_bytes
        moved = 0
        for var, path, size_mb in checks:
            if var.get() and os.path.exists(path):
                if _move_to_trash(path):
                    deleted.append(path)
                    freed_bytes += size_mb * 1024 * 1024
                    moved += 1
        status_var.set(f"Moved {moved} file(s) to Trash ({TRASH_DIR}).")
        if moved:
            messagebox.showinfo("Done", f"Moved {moved} file(s) to Trash.\nYou can empty '{TRASH_DIR}' later to free the space permanently.")
        root.destroy()

    def do_close():
        root.destroy()

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=10)
    tk.Button(btn_frame, text="Move Selected to Trash", bg="#d9534f", fg="white",
              command=do_delete, padx=10, pady=4).pack(side="left", padx=6)
    tk.Button(btn_frame, text="Close without changes", command=do_close,
              padx=10, pady=4).pack(side="left", padx=6)

    root.mainloop()
    return deleted, freed_bytes


def summarize_and_optimize_storage(target_dir: str = None) -> str:
    """Scan target_dir, then open an interactive review UI (GUI if a display
    is available, otherwise a CLI prompt) so the user can choose which
    duplicate/large files to move to trash. Returns a text summary."""
    scan = scan_storage(target_dir)

    if scan["error"]:
        return f"Storage scan error: {scan['error']}"

    duplicates = scan["duplicates"]
    large_files = scan["large_files"]

    summary_lines = [
        f"Disk Summary: {scan['free_gb']} GB free of {scan['total_gb']} GB total.",
        f"Scanned folder: {scan['target_dir']}",
        f"- Found {len(duplicates)} duplicate file(s).",
        f"- Found {len(large_files)} large file(s) (>{LARGE_FILE_MB_THRESHOLD}MB).",
    ]

    if duplicates or large_files:
        deleted, freed_bytes = _gui_review(duplicates, large_files)
        if deleted:
            summary_lines.append(
                f"- Moved {len(deleted)} file(s) to trash, freeing about {_human_mb(freed_bytes / (1024 * 1024))}."
            )
            summary_lines.append(f"- Trash location: {TRASH_DIR} (safe to empty later).")
        else:
            summary_lines.append("- No files were removed.")
    else:
        summary_lines.append("- Nothing flagged for review.")

    return "\n".join(summary_lines)
