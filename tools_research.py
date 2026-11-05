import os
import sys
from datetime import datetime

try:
    from tools_artifacts import record_artifact
except Exception:
    record_artifact = None

_DATA_DIR = os.getenv("AGATHA_DATA_DIR")
RESEARCH_DIR = os.getenv("AGATHA_RESEARCH_DIR", os.path.join(_DATA_DIR, "research")
                         if _DATA_DIR else os.path.expanduser("~/Documents/Agent Research"))


def web_search_free(query: str, max_results: int = 6) -> list:
    """Free, no-API-key web search. Returns [{title, url, snippet}, ...].
    Uses the 'ddgs' package (the current name — this library was renamed
    from 'duckduckgo-search')."""
    try:
        from ddgs import DDGS
    except ImportError as e:
        raise ImportError(
            f"The 'ddgs' package is not installed for THIS interpreter ({sys.executable}): {e}\n"
            f"Install with:\n    {sys.executable} -m pip install ddgs"
        )

    max_results = max(1, min(int(max_results), 15))
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title", "").strip(),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", "").strip(),
                })
    except Exception as e:
        raise RuntimeError(f"Web search failed: {e}")

    return results


def _safe_filename(topic: str) -> str:
    keep = "".join(c if c.isalnum() or c in (" ", "-", "_") else "" for c in topic)
    keep = keep.strip().replace(" ", "_")[:60] or "research"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{keep}"


def _pdf_safe(text: str) -> str:
    """fpdf2's built-in core fonts only support latin-1 — replace anything
    outside that range rather than letting PDF generation crash on, say,
    curly quotes or emoji the LLM happened to use."""
    return text.encode("latin-1", "replace").decode("latin-1")


def save_research_note(topic: str, content: str, also_pdf: bool = True) -> dict:
    """Save a research summary as a Markdown note, and (best-effort) a PDF
    alongside it. Returns {'md_path': str, 'pdf_path': str|None}. Never
    raises for the PDF step — a PDF-generation failure just means pdf_path
    is None; the Markdown note is always saved as the reliable fallback."""
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    base = _safe_filename(topic)

    md_path = os.path.join(RESEARCH_DIR, base + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Research: {topic}\n\n{content}\n")
    if record_artifact:
        try:
            record_artifact("research_note", md_path, title=topic)
        except Exception as e:
            print(f"[Warning] Could not record artifact: {e}")

    pdf_path = None
    if also_pdf:
        try:
            from fpdf import FPDF  # provided by the 'fpdf2' package
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Helvetica", "B", 16)
            pdf.multi_cell(0, 10, _pdf_safe(f"Research: {topic}"))
            pdf.ln(4)
            pdf.set_font("Helvetica", size=11)
            for line in content.split("\n"):
                pdf.multi_cell(0, 7, _pdf_safe(line) if line.strip() else " ")
            pdf_path = os.path.join(RESEARCH_DIR, base + ".pdf")
            pdf.output(pdf_path)
            if record_artifact:
                try:
                    record_artifact("research_pdf", pdf_path, title=topic)
                except Exception as e:
                    print(f"[Warning] Could not record artifact: {e}")
        except ImportError:
            print(f"[Info] PDF not generated — 'fpdf2' isn't installed for THIS interpreter ({sys.executable}). "
                  f"Install with: {sys.executable} -m pip install fpdf2. Markdown note saved instead.")
            pdf_path = None
        except Exception as e:
            print(f"[Warning] PDF generation failed ({e}); Markdown note was still saved.")
    return {"md_path": md_path, "pdf_path": pdf_path}
