"""Read-only safety triage for links, uploaded files, documents, and code."""
import difflib
import hashlib
import ipaddress
import os
import re
import shutil
import subprocess
from urllib.parse import urlsplit


_URL_RE = re.compile(r"https?://[^\s<>\]\[()'\"]+", re.IGNORECASE)
_BRANDS = ("amazon", "apple", "facebook", "google", "instagram", "microsoft", "netflix", "paypal")
_SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "ow.ly", "is.gd", "buff.ly", "cutt.ly"}
_CODE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".php", ".rb", ".go", ".rs"}
_ACTIVE_EXTENSIONS = {".app", ".command", ".dmg", ".exe", ".jar", ".js", ".pkg", ".ps1", ".sh", ".vbs"}
_IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
_SIGNATURES = {
    ".pdf": lambda data: data.startswith(b"%PDF-"),
    ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
    ".webp": lambda data: len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    ".docx": lambda data: data.startswith(b"PK\x03\x04"),
    ".xlsx": lambda data: data.startswith(b"PK\x03\x04"),
    ".pptx": lambda data: data.startswith(b"PK\x03\x04"),
}
_CODE_RISKS = (
    (re.compile(r"\beval\s*\("), "Dynamic eval() can execute untrusted text; replace it with explicit parsing."),
    (re.compile(r"\bexec\s*\("), "Dynamic exec() can execute arbitrary code; remove it or tightly constrain the input."),
    (re.compile(r"\bos\.system\s*\(|\bsubprocess\.[A-Za-z_]+\s*\([^\n]*shell\s*=\s*True"),
     "Shell execution with composed input can permit command injection; prefer argument arrays and validate inputs."),
    (re.compile(r"\bpickle\.loads?\s*\("), "Unpickling untrusted data can execute code; use a non-executable data format."),
    (re.compile(r"\byaml\.load\s*\("), "yaml.load() may construct unsafe objects; use yaml.safe_load() for untrusted YAML."),
    (re.compile(r"\bverify\s*=\s*False\b|_create_unverified_context"),
     "TLS certificate verification appears disabled; restore certificate verification."),
    (re.compile(r"(?:base64\.b64decode|b64decode)[\s\S]{0,300}\b(?:exec|eval)\s*\("),
     "Encoded content appears connected to dynamic execution; inspect it as untrusted code."),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
     "A private-key block appears in the source; remove it and rotate the exposed credential."),
)
_SCAM_RISKS = (
    (re.compile(r"\b(?:send|share|reveal|confirm)\b.{0,50}\b(?:password|passcode|one[- ]time code|2fa|seed phrase|recovery phrase)\b", re.I),
     "The text asks for a password, verification code, or wallet recovery phrase."),
    (re.compile(r"\b(?:gift cards?|wire transfer|crypto(?:currency)?|bitcoin)\b.{0,60}\b(?:send|pay|buy|transfer)\b", re.I),
     "The text combines an unusual payment method with a request to send money."),
    (re.compile(r"\b(?:urgent|immediately|within 24 hours|act now)\b.{0,100}\b(?:account|payment|verify|suspended|locked)\b", re.I),
     "Urgency is paired with an account, payment, or verification demand."),
    (re.compile(r"\b(?:install|download)\b.{0,60}\b(?:remote access|anydesk|teamviewer|screenconnect)\b", re.I),
     "The text asks to install remote-access software."),
)


def _finding(level: str, message: str) -> dict:
    return {"level": level, "message": message}


def inspect_url(url: str) -> dict:
    url = (url or "").strip().rstrip(".,!?;:")
    findings = []
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return {"target": url, "findings": [_finding("high", "The URL could not be parsed reliably.")], "scanner": "not applicable"}

    if parsed.scheme not in ("http", "https") or not host:
        findings.append(_finding("high", "The link is not a complete HTTP or HTTPS URL."))
    if parsed.scheme == "http":
        findings.append(_finding("medium", "The link uses HTTP, so the connection is not protected by TLS."))
    if parsed.username is not None or parsed.password is not None:
        findings.append(_finding("high", "The URL contains user-info before the host, a common deception pattern."))
    if "xn--" in host:
        findings.append(_finding("high", "The hostname contains an internationalized punycode label; check for look-alike characters."))
    if host in _SHORTENERS:
        findings.append(_finding("medium", "This is a URL shortener, so the final destination is hidden."))

    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address and not address.is_global:
        findings.append(_finding("medium", "The link points to a non-public IP address rather than a public website."))

    labels = host.split(".")
    domain_label = labels[-2] if len(labels) > 1 else labels[0]
    normalized = re.sub(r"[^a-z0-9]", "", domain_label)
    suffix = labels[-1] if len(labels) > 1 else ""
    for brand in _BRANDS:
        canonical_hosts = (f"{brand}.com", f"{brand}.org", f"{brand}.net")
        if any(brand in re.sub(r"[^a-z0-9]", "", label) for label in labels[:-1]) \
            and not any(host == canonical or host.endswith("." + canonical) for canonical in canonical_hosts):
            findings.append(_finding("high", f"The hostname uses '{brand}' outside its common official domain; possible brand impersonation."))
            break
        ratio = difflib.SequenceMatcher(None, normalized, brand).ratio()
        if normalized != brand and (ratio >= 0.78 or (brand in normalized and len(normalized) > len(brand))):
            findings.append(_finding("high", f"The domain label resembles '{brand}' without being an exact match; possible brand impersonation."))
            break
        if normalized == brand and suffix not in ("com", "org", "net"):
            findings.append(_finding("medium", f"The domain uses the name '{brand}' with an unusual top-level suffix; verify it independently."))
            break

    if not findings:
        findings.append(_finding("info", "No obvious URL red flags were found by local checks; the destination was not opened or reputation-checked."))
    return {"target": url, "findings": findings, "scanner": "local URL heuristics only; no network request made"}


def inspect_text(text: str, subject: str = "provided text", code: bool = False) -> dict:
    text = (text or "")[:200_000]
    findings = []
    for pattern, message in _SCAM_RISKS:
        if pattern.search(text):
            findings.append(_finding("high", message))
    if code:
        for pattern, message in _CODE_RISKS:
            if pattern.search(text):
                findings.append(_finding("medium", message))
    for url in _URL_RE.findall(text):
        findings.extend(inspect_url(url)["findings"])

    verification_request = re.search(
        r"\b(?:authentic|authenticate|genuine|certificate|certification|credential|proof|verify|verification)\b",
        subject, re.I)
    if verification_request:
        findings.append(_finding("unknown", "Authenticity, issuer identity, and current validity cannot be established from text alone; confirm directly with the issuing organization using contact details found independently."))
    if not findings:
        findings.append(_finding("info", "No obvious scam or code-risk indicators were found in the supplied text; this does not prove it is safe or true."))
    return {"target": subject, "findings": findings, "scanner": "local text heuristics only"}


def inspect_file(file_path: str, extracted_text: str = "", subject: str = "") -> dict:
    path = os.path.abspath(os.path.expanduser((file_path or "").strip().strip("\"'")))
    findings = []
    if not path or not os.path.isfile(path):
        return {"target": file_path or "file", "findings": [_finding("high", "The file path does not identify a readable regular file.")], "scanner": "not run"}
    if os.path.getsize(path) > 100 * 1024 * 1024:
        return {"target": os.path.basename(path), "findings": [_finding("unknown", "The file exceeds the 100 MB local scan limit.")], "scanner": "not run"}

    extension = os.path.splitext(path)[1].lower()
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            header = source.read(32)
            digest.update(header)
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        sha256 = digest.hexdigest()
    except OSError as error:
        return {"target": path, "findings": [_finding("high", f"The file could not be read: {error}")], "scanner": "not run"}

    signature_check = _SIGNATURES.get(extension)
    if signature_check and not signature_check(header):
        findings.append(_finding("high", "The file contents do not match the format suggested by its extension."))
    if extension in _ACTIVE_EXTENSIONS:
        findings.append(_finding("medium", f"{extension} is an executable or active file type; do not run it unless its source is trusted."))
    if extension in _IMAGE_EXTENSIONS:
        findings.append(_finding("unknown", "This checks image file structure and optional antivirus results only; it cannot establish whether the image is synthetic, edited, or authentic."))

    scanner_status = "not available (ClamAV clamscan was not found)"
    scanner = shutil.which("clamscan")
    if scanner:
        try:
            result = subprocess.run([scanner, "--no-summary", path], capture_output=True,
                                    text=True, timeout=90, check=False)
            output = (result.stdout + "\n" + result.stderr).strip()
            if result.returncode == 1 and "FOUND" in output:
                scanner_status = "DETECTION"
                findings.append(_finding("critical", "ClamAV reported a detection. Do not open or share this file; review it with trusted security software."))
            elif result.returncode == 0:
                scanner_status = "no detection reported by ClamAV"
            else:
                scanner_status = "error or incomplete scan"
                findings.append(_finding("unknown", "ClamAV did not complete successfully; no clean result can be inferred."))
        except (OSError, subprocess.TimeoutExpired) as error:
            scanner_status = f"scan failed ({error})"
            findings.append(_finding("unknown", "The antivirus scan did not complete; no clean result can be inferred."))

    if extracted_text:
        is_code = extension in _CODE_EXTENSIONS
        text_report = inspect_text(extracted_text, subject=subject or os.path.basename(path), code=is_code)
        findings.extend(text_report["findings"])
    if re.search(r"\b(?:authentic|authenticate|genuine|certificate|certification|credential|proof|verify|verification)\b",
                 subject or os.path.basename(path), re.I):
        findings.append(_finding("unknown", "A filename or extracted document text cannot authenticate a certificate or prove who issued it; verify with the issuer directly."))
    if not findings:
        findings.append(_finding("info", "No obvious local indicators were found; this is not proof the file is safe, unmodified, or authentic."))
    return {"target": os.path.basename(path), "findings": findings, "scanner": scanner_status, "sha256": sha256}


def format_report(report: dict) -> str:
    findings = report.get("findings", [])
    levels = {item.get("level") for item in findings}
    verdict = "HIGH RISK" if levels & {"critical", "high"} else "CAUTION" if "medium" in levels else "UNVERIFIED"
    lines = [f"{verdict}: {report.get('target', 'supplied item')}"]
    lines.extend(f"- {item['level'].upper()}: {item['message']}" for item in findings)
    lines.append(f"- Scanner: {report.get('scanner', 'local checks only')}.")
    if report.get("sha256"):
        lines.append(f"- SHA-256: {report['sha256']} (useful for comparison, not proof of authenticity).")
    lines.append("No file was executed, changed, blocked at the OS level, or quarantined.")
    return "\n".join(lines)


def review_safety(url: str = "", file_path: str = "", content: str = "", subject: str = "") -> str:
    reports = []
    if url:
        reports.append(inspect_url(url))
    if file_path:
        reports.append(inspect_file(file_path, extracted_text=content, subject=subject))
    elif content:
        is_code = bool(re.search(r"\b(?:eval|exec|os\.system|subprocess|pickle|yaml\.load)\s*\b", content))
        reports.append(inspect_text(content, subject=subject or "provided text", code=is_code))
    return "\n\n".join(format_report(report) for report in reports) or "Provide a URL, a local file, or text to review."


TOOL_SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "review_safety",
        "description": "Read-only local triage of a URL, file, document text, or code for suspicious indicators. Never executes or changes files and cannot prove authenticity.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "file_path": {"type": "string"},
                           "content": {"type": "string"}, "subject": {"type": "string"}},
            "required": [],
        },
    },
}]
FUNCTIONS = {"review_safety": review_safety}
TOOL_INTENTS = {
    r"\b(?:scam|scams|phish(?:ing)?|malware|virus|compromised|fake|forged|authentic|authenticate|certificate|certification|proof|verify|verification|security risk|safe link|safe site)\b":
        {"review_safety"},
}
