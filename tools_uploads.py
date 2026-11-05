import json
import base64
import io
import os
import plistlib
import re
import uuid
import zipfile
from email import policy
from email.parser import BytesParser

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agatha_data", "uploads")
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_REQUEST_BYTES = 110 * 1024 * 1024
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".py", ".html", ".xml", ".yaml", ".yml", ".log",
    ".rst", ".tex", ".sql", ".ini", ".toml", ".cfg", ".conf", ".sh", ".bash", ".zsh", ".ps1",
    ".bat", ".c", ".h", ".cpp", ".java", ".go", ".rs", ".rb", ".php", ".css", ".js", ".ts",
    ".jsx", ".tsx", ".vue", ".srt", ".vtt", ".ics", ".eml",
}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".odt", ".pptx", ".xlsx", ".rtf", ".epub"}
LINK_EXTENSIONS = {".url", ".webloc"}
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
}
AUDIO_TYPES = {".mp3": "audio/mpeg"}
MAX_EXTRACTED_TEXT_CHARS = 2_000_000


def _decode_text(data: bytes, filename: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
        raise ValueError(f"{filename} must be UTF-8 or UTF-16 text.")


def _extract_document_text(extension: str, data: bytes, filename: str) -> str:
    if extension in TEXT_EXTENSIONS:
        text = _decode_text(data, filename)
        if extension in (".html", ".xml"):
            try:
                from bs4 import BeautifulSoup
                text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
            except ImportError:
                text = re.sub(r"<[^>]+>", " ", text)
        elif extension == ".eml":
            from email.parser import BytesParser
            message = BytesParser(policy=policy.default).parsebytes(data)
            text = "\n".join(part.get_content() for part in message.walk()
                              if part.get_content_type() == "text/plain" and not part.is_multipart())
        return text[:MAX_EXTRACTED_TEXT_CHARS]

    if extension == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for index, page in enumerate(reader.pages):
                page_text = (page.extract_text() or "").strip()
                if page_text:
                    pages.append(f"[Page {index + 1}]\n{page_text}")
        except ImportError as error:
            raise ValueError("PDF reading needs the 'pypdf' package. Install the project requirements.") from error
        except Exception as error:
            raise ValueError(f"Could not read {filename} as a PDF: {error}") from error
        text = "\n\n".join(pages).strip()
        if not text:
            raise ValueError(f"{filename} has no selectable text; scanned PDFs need OCR first.")
        return text[:MAX_EXTRACTED_TEXT_CHARS]

    if extension == ".docx":
        try:
            from docx import Document
            document = Document(io.BytesIO(data))
        except ImportError as error:
            raise ValueError("Word document reading needs 'python-docx'. Install the project requirements.") from error
        except Exception as error:
            raise ValueError(f"Could not read {filename} as a Word document: {error}") from error
        blocks = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        blocks.extend(" | ".join(cell.text for cell in row.cells)
                      for table in document.tables for row in table.rows)
        return "\n".join(blocks)[:MAX_EXTRACTED_TEXT_CHARS]

    if extension == ".pptx":
        try:
            from pptx import Presentation
            presentation = Presentation(io.BytesIO(data))
        except ImportError as error:
            raise ValueError("PowerPoint reading needs 'python-pptx'. Install the project requirements.") from error
        except Exception as error:
            raise ValueError(f"Could not read {filename} as a presentation: {error}") from error
        slides = []
        for index, slide in enumerate(presentation.slides, 1):
            lines = [shape.text for shape in slide.shapes if getattr(shape, "has_text_frame", False) and shape.text.strip()]
            slides.append(f"[Slide {index}]\n" + "\n".join(lines))
        return "\n\n".join(slides)[:MAX_EXTRACTED_TEXT_CHARS]

    if extension == ".xlsx":
        try:
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except ImportError as error:
            raise ValueError("Excel reading needs 'openpyxl'. Install the project requirements.") from error
        except Exception as error:
            raise ValueError(f"Could not read {filename} as a spreadsheet: {error}") from error
        rows = []
        try:
            for sheet in workbook.worksheets:
                rows.append(f"[Sheet: {sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    values = [str(value) for value in row if value is not None]
                    if values:
                        rows.append(" | ".join(values))
                    if sum(map(len, rows)) >= MAX_EXTRACTED_TEXT_CHARS:
                        break
                if sum(map(len, rows)) >= MAX_EXTRACTED_TEXT_CHARS:
                    break
        finally:
            workbook.close()
        return "\n".join(rows)[:MAX_EXTRACTED_TEXT_CHARS]

    if extension == ".odt":
        try:
            from odf.opendocument import load
            from odf.teletype import extractText
            document = load(io.BytesIO(data))
            text = extractText(document.text)
        except ImportError as error:
            raise ValueError("OpenDocument reading needs 'odfpy'. Install the project requirements.") from error
        except Exception as error:
            raise ValueError(f"Could not read {filename} as an OpenDocument file: {error}") from error
        return text[:MAX_EXTRACTED_TEXT_CHARS]

    if extension == ".rtf":
        try:
            from striprtf.striprtf import rtf_to_text
            text = rtf_to_text(_decode_text(data, filename))
        except ImportError as error:
            raise ValueError("Rich Text reading needs 'striprtf'. Install the project requirements.") from error
        return text[:MAX_EXTRACTED_TEXT_CHARS]

    if extension == ".epub":
        try:
            from bs4 import BeautifulSoup
            with zipfile.ZipFile(io.BytesIO(data)) as book:
                chapters = [BeautifulSoup(book.read(entry), "html.parser").get_text(" ", strip=True)
                            for entry in book.namelist()
                            if entry.lower().endswith((".xhtml", ".html", ".htm"))]
        except Exception as error:
            raise ValueError(f"Could not read {filename} as an EPUB book: {error}") from error
        text = "\n\n".join(chapter for chapter in chapters if chapter)
        if not text:
            raise ValueError(f"{filename} contains no readable book text.")
        return text[:MAX_EXTRACTED_TEXT_CHARS]

    return ""


def parse_multipart_form(content_type: str, body: bytes) -> tuple[str, list[dict]]:
    """Validate and store dashboard uploads; return accompanying text and attachments."""
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body
    )
    if not message.is_multipart():
        raise ValueError("Expected a multipart form upload.")

    attachments = []
    text = ""
    for part in message.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        if field_name == "text":
            text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace").strip()
            continue
        if field_name != "files":
            continue
        filename = os.path.basename(part.get_filename() or "").strip()
        if not filename:
            raise ValueError("An uploaded file is missing its filename.")
        extension = os.path.splitext(filename)[1].lower()
        if extension not in TEXT_EXTENSIONS and extension not in DOCUMENT_EXTENSIONS and extension not in LINK_EXTENSIONS and extension not in IMAGE_TYPES and extension not in VIDEO_TYPES and extension not in AUDIO_TYPES:
            raise ValueError(f"Unsupported file type: {extension or 'no extension'}.")
        data = part.get_payload(decode=True) or b""
        if not data:
            raise ValueError(f"{filename} is empty.")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{filename} exceeds the 50 MB per-file limit.")

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        stored_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}{extension}")
        with open(stored_path, "xb") as uploaded:
            uploaded.write(data)

        attachment = {"name": filename, "path": stored_path}
        if extension in TEXT_EXTENSIONS or extension in DOCUMENT_EXTENSIONS:
            try:
                attachment["text"] = _extract_document_text(extension, data, filename)
                attachment["text_truncated"] = len(attachment["text"]) >= MAX_EXTRACTED_TEXT_CHARS
            except ValueError as error:
                os.remove(stored_path)
                raise ValueError(str(error)) from error
            attachment["mime_type"] = "text/plain"
        elif extension in LINK_EXTENSIONS:
            try:
                if extension == ".webloc":
                    link_data = plistlib.loads(data)
                    link = link_data.get("URL", "") if isinstance(link_data, dict) else ""
                else:
                    content = data.decode("utf-8-sig")
                    match = re.search(r"(?im)^URL\s*=\s*(https?://\S+)\s*$", content)
                    link = match.group(1) if match else ""
                if not link.startswith(("https://", "http://")):
                    raise ValueError("Shortcut must contain an HTTP or HTTPS URL.")
            except (UnicodeDecodeError, plistlib.InvalidFileException, ValueError) as error:
                os.remove(stored_path)
                raise ValueError(f"{filename} is not a valid web link shortcut.") from error
            attachment["url"] = link
            attachment["mime_type"] = "text/uri-list"
        elif extension in IMAGE_TYPES:
            attachment["mime_type"] = IMAGE_TYPES[extension]
            attachment["image_base64"] = base64.b64encode(data).decode("ascii")
        elif extension in AUDIO_TYPES:
            attachment["mime_type"] = AUDIO_TYPES[extension]
        else:
            attachment["mime_type"] = VIDEO_TYPES[extension]
        attachments.append(attachment)

    return text, attachments


def describe_attachments(attachments: list[dict]) -> str:
    """Build a bounded text representation for providers without file/image support."""
    sections = []
    for attachment in attachments:
        name = attachment.get("name", "uploaded file")
        text = attachment.get("text")
        if text is not None:
            sections.append(f"\n\n[Attached file: {name}]\n{text[:100000]}")
        elif attachment.get("url"):
            sections.append(f"\n\n[Attached link: {name}] {attachment['url']}")
        elif attachment.get("mime_type", "").startswith("audio/"):
            transcript = attachment.get("transcript")
            if transcript:
                sections.append(f"\n\n[Transcript of attached audio: {name}]\n{transcript[:100000]}")
            else:
                reason = attachment.get("transcription_error", "transcription was unavailable")
                sections.append(f"\n\n[Audio attached: {name}; it was not transcribed because {reason}.]")
        elif attachment.get("mime_type", "").startswith("video/"):
            sections.append(
                f"\n\n[Video attached: {name}. Saved locally at {attachment.get('path', '')}. "
                "This model cannot inspect video frames directly.]"
            )
        elif attachment.get("mime_type", "").startswith("image/"):
            sections.append(f"\n\n[Attached image: {name}]")
    return "".join(sections)


def transcribe_audio(path: str) -> tuple[str | None, str]:
    """Transcribe an audio file locally using the creator toolkit's optional Whisper setup."""
    try:
        from tools_creator import _transcribe
    except Exception as error:
        return None, f"the local transcription helper is unavailable ({error})"
    segments, error = _transcribe(path)
    if not segments:
        return None, error or "the recording contained no recognizable speech"
    return " ".join(segment["text"] for segment in segments), ""
