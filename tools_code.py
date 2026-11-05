import os


def read_code_file(file_path: str) -> str:
    file_path = os.path.expanduser(file_path)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"'{file_path}' does not exist or is not a file.")
    if os.path.getsize(file_path) > 2_000_000:  # 2 MB sanity cap
        raise ValueError(f"'{file_path}' is too large to process (>2MB) — split it up first.")
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_cleaned_code(original_path: str, new_content: str) -> str:
    """Never overwrites the original file — writes alongside it with a
    '_cleaned' suffix so nothing is destructively modified before the user
    has reviewed the diff themselves. Returns the new file's path."""
    original_path = os.path.expanduser(original_path)
    base, ext = os.path.splitext(original_path)
    out_path = f"{base}_cleaned{ext}"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return out_path


def strip_markdown_fences(text: str) -> str:
    """LLMs sometimes wrap code in ``` fences even when told not to —
    strip them defensively before writing the file out."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text
