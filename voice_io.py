import os
import sys
import json
import threading

# Printed unconditionally so any "package not found" message below can be
# compared directly against wherever you ran `pip install` — the #1 cause
# of "I installed it but it still says missing" is pip and this script
# pointing at two different Python installations.
print(f"[Diagnostics] voice_io.py is loading with interpreter: {sys.executable}")

# --- Load .env file, if present, BEFORE reading any env vars below.
# Without this, values set in .env are invisible to os.getenv() — they are
# NOT loaded automatically by Python. This was silently breaking
# MIC_DEVICE_INDEX / SR_ENERGY_THRESHOLD / OLLAMA_* / API keys set in .env.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print(f"[Info] python-dotenv not installed for THIS interpreter ({sys.executable}) — "
          f".env file values will be ignored. Fix with:\n"
          f"    {sys.executable} -m pip install python-dotenv\n"
          f"(using '{sys.executable} -m pip' — not a bare 'pip install' — guarantees it installs "
          f"into the exact Python that is running this script, which is the usual reason a package "
          f"'installed' but still shows as missing).")

try:
    import pyttsx3
except ImportError as e:
    raise ImportError(
        f"pyttsx3 is not installed for THIS interpreter ({sys.executable}): {e}\n"
        f"Fix with:\n    {sys.executable} -m pip install pyttsx3 pyobjc\n"
        f"(pyobjc is required for pyttsx3's speech backend on macOS.)\n"
        f"If you already ran 'pip install pyttsx3' and still see this error, it almost certainly "
        f"installed into a DIFFERENT Python than {sys.executable} — always use "
        f"'{sys.executable} -m pip install ...' to guarantee they match."
    )

try:
    import speech_recognition as sr
except ImportError as e:
    raise ImportError(
        f"SpeechRecognition is not installed for THIS interpreter ({sys.executable}): {e}\n"
        f"Fix with:\n    {sys.executable} -m pip install SpeechRecognition pyaudio"
    )

try:
    import pyaudio
except ImportError as e:
    raise ImportError(
        f"PyAudio is not installed for THIS interpreter ({sys.executable}): {e}\n"
        f"Fix with:\n    {sys.executable} -m pip install pyaudio\n"
        f"On macOS, if that fails to build: brew install portaudio, then retry."
    )

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_config.json")

DEFAULT_RATE = 170

TONE_PRESETS = {
    "neutral":   {"rate": 170, "pitch": 50},
    "warm":      {"rate": 158, "pitch": 46},
    "calm":      {"rate": 145, "pitch": 42},
    "energetic": {"rate": 195, "pitch": 58},
    "deep":      {"rate": 150, "pitch": 30},
    "bright":    {"rate": 185, "pitch": 64},
}

FEMALE_HINTS = ["female", "zira", "hazel", "susan", "samantha", "victoria",
                "karen", "moira", "tessa", "fiona", "eva", "salli", "joanna", "+f", "f1", "f2", "f3"]
MALE_HINTS = ["male", "david", "mark", "alex", "daniel", "fred", "george",
              "guy", "matthew", "joey", "+m", "m1", "m2", "m3"]


class MicrophoneUnavailable(Exception):
    """Raised when no working microphone/audio input device can be opened."""
    pass


# --- STT setup ---
_recognizer = sr.Recognizer()


def _resolve_mic_device_index():
    """Read MIC_DEVICE_INDEX from the environment and turn it into an actual
    device index speech_recognition can use. Accepts either:
      - a plain number, e.g. MIC_DEVICE_INDEX=2
      - a name/substring to match against the detected device list, e.g.
        MIC_DEVICE_INDEX=USB Microphone
    Never raises — any problem here just falls back to the system default
    device and prints a clear reason why, instead of crashing the whole
    voice_io import (which is what silently disabled voice entirely before)."""
    raw = os.getenv("MIC_DEVICE_INDEX")
    if not raw or not raw.strip():
        return None
    raw = raw.strip()

    if raw.lstrip("-").isdigit():
        return int(raw)

    try:
        names = sr.Microphone.list_microphone_names()
    except Exception as e:
        print(f"[Warning] MIC_DEVICE_INDEX='{raw}' was given as a name, but microphones "
              f"could not be enumerated to resolve it ({e}). Using system default device.")
        return None

    raw_lower = raw.lower()
    for i, name in enumerate(names):
        if raw_lower in name.lower():
            print(f"[Info] MIC_DEVICE_INDEX='{raw}' matched device {i}: '{name}'.")
            return i

    print(f"[Warning] MIC_DEVICE_INDEX='{raw}' did not match any detected microphone name. "
          f"Falling back to the system default device. Run 'list microphones' to see the exact "
          f"names/indexes available, then set MIC_DEVICE_INDEX to one of those (a number is most reliable).")
    return None


try:
    _MIC_DEVICE_INDEX = _resolve_mic_device_index()
except Exception as e:
    print(f"[Warning] Could not resolve MIC_DEVICE_INDEX ({e}); using system default microphone.")
    _MIC_DEVICE_INDEX = None

_ENERGY_THRESHOLD = os.getenv("SR_ENERGY_THRESHOLD")
if _ENERGY_THRESHOLD:
    try:
        _recognizer.energy_threshold = float(_ENERGY_THRESHOLD)
        _recognizer.dynamic_energy_threshold = False
        print(f"[Info] Using fixed SR_ENERGY_THRESHOLD={_recognizer.energy_threshold} from environment "
              f"(auto-calibration disabled).")
    except ValueError:
        print(f"[Warning] SR_ENERGY_THRESHOLD='{_ENERGY_THRESHOLD}' is not a valid number; ignoring it.")


# ---------------------------------------------------------------------------
# Raw PyAudio capture, bypassing sr.Microphone() entirely.
#
# After exhausting every configuration-level cause (permissions confirmed
# "authorized" by macOS's own AVFoundation API, correct device selected,
# packages installed against the right interpreter), the remaining symptom
# — mic.__enter__() returns successfully but source.stream is None — points
# to a bug/incompatibility inside SpeechRecognition's own Microphone
# context-manager wrapper on this system, not anything configurable. Rather
# than keep fighting that wrapper's internals, we talk to PyAudio directly:
# open a stream ourselves, read raw frames, and hand the result to
# Recognizer.recognize_google() as a manually-built AudioData object. This
# keeps the (working) Google recognition call, but removes the
# (unreliable) SpeechRecognition Microphone class from the picture.
# ---------------------------------------------------------------------------
_RAW_FORMAT = pyaudio.paInt16
_RAW_CHUNK = 1024


def _compute_rms(data: bytes) -> float:
    """RMS volume of a 16-bit PCM chunk, used for simple silence detection.
    Uses audioop if available (fast); falls back to pure Python otherwise
    (audioop was removed in Python 3.13+)."""
    try:
        import audioop
        return audioop.rms(data, 2)
    except Exception:
        import struct
        count = len(data) // 2
        if count == 0:
            return 0.0
        total = 0
        for i in range(count):
            sample = struct.unpack_from("<h", data, i * 2)[0]
            total += sample * sample
        return (total / count) ** 0.5


def _downmix_to_mono(data: bytes, channels: int) -> bytes:
    """Collapse an interleaved N-channel 16-bit PCM buffer down to mono.
    recognize_google() expects mono audio; not every input device records
    in mono (hence the -9998 'Invalid number of channels' error this
    replaces — we now open the device at whatever channel count it
    actually supports, then downmix here instead of guessing 1 upfront)."""
    if channels <= 1:
        return data
    try:
        import audioop
        if channels == 2:
            return audioop.tomono(data, 2, 0.5, 0.5)
    except Exception:
        pass
    import struct
    frame_size = 2 * channels
    num_frames = len(data) // frame_size
    out = bytearray()
    fmt = f"<{channels}h"
    for i in range(num_frames):
        offset = i * frame_size
        samples = struct.unpack_from(fmt, data, offset)
        out += struct.pack("<h", int(sum(samples) / channels))
    return bytes(out)


def _resolve_device_for_capture(pa):
    """Return (device_index, device_info) for a concrete input device —
    never None — so we can read its actual supported channel count instead
    of guessing. This is what -9998 'Invalid number of channels' meant:
    we were requesting a channel count the resolved device doesn't support."""
    try:
        if _MIC_DEVICE_INDEX is not None:
            info = pa.get_device_info_by_index(_MIC_DEVICE_INDEX)
            return _MIC_DEVICE_INDEX, info
        info = pa.get_default_input_device_info()
        return int(info["index"]), info
    except Exception as e:
        raise MicrophoneUnavailable(
            f"Could not query audio device info: {e}\n"
            f"Run 'list microphones' and set MIC_DEVICE_INDEX to a valid input device index."
        )


def _raw_record(max_duration: float = 8.0, silence_duration: float = 1.2, ambient_duration: float = 0.6):
    """Open a raw PyAudio input stream directly, record until a pause is
    detected (or max_duration is hit), and return an sr.AudioData ready for
    recognize_google(). Returns None if nothing but silence was captured.
    Raises MicrophoneUnavailable only on a genuine device-open/read failure —
    silence is a normal, non-error outcome, not an exception."""
    pa = pyaudio.PyAudio()

    try:
        device_index, dev_info = _resolve_device_for_capture(pa)
    except MicrophoneUnavailable:
        pa.terminate()
        raise

    max_channels = int(dev_info.get("maxInputChannels", 0) or 0)
    if max_channels < 1:
        pa.terminate()
        raise MicrophoneUnavailable(
            f"Device '{dev_info.get('name')}' (index {device_index}) reports 0 input channels — it "
            f"isn't actually usable as a microphone (possibly an output-only or misidentified device). "
            f"Run 'list microphones' and set MIC_DEVICE_INDEX to a real input device."
        )

    rate = int(dev_info.get("defaultSampleRate", 16000) or 16000)

    # Try the device's native channel count first (this is what -9998 was
    # about — we previously always requested 1 regardless of what the
    # device actually supports), then fall back to mono, then stereo, as a
    # last-resort sweep for unusual devices.
    channel_attempts = []
    for c in (max_channels, 1, 2):
        if c >= 1 and c not in channel_attempts:
            channel_attempts.append(c)

    stream = None
    opened_channels = None
    last_err = None
    for ch in channel_attempts:
        try:
            stream = pa.open(
                format=_RAW_FORMAT, channels=ch, rate=rate, input=True,
                input_device_index=device_index, frames_per_buffer=_RAW_CHUNK,
            )
            opened_channels = ch
            break
        except Exception as e:
            last_err = e
            continue

    if stream is None:
        pa.terminate()
        raise MicrophoneUnavailable(
            f"PyAudio could not open '{dev_info.get('name')}' (index {device_index}) with any of the "
            f"channel counts tried ({channel_attempts}): {last_err}\n"
            f"Try a different device: run 'list microphones' and set MIC_DEVICE_INDEX to another index. "
            f"If no device works at all, the PortAudio build itself may be broken:\n"
            f"    brew install portaudio\n"
            f"    {sys.executable} -m pip uninstall -y pyaudio\n"
            f"    {sys.executable} -m pip install --no-cache-dir pyaudio"
        )

    frames = []
    threshold = _recognizer.energy_threshold if _recognizer.energy_threshold else 300

    try:
        if _recognizer.dynamic_energy_threshold:
            ambient_chunks = max(1, int(rate / _RAW_CHUNK * ambient_duration))
            levels = []
            for _ in range(ambient_chunks):
                data = stream.read(_RAW_CHUNK, exception_on_overflow=False)
                levels.append(_compute_rms(data))
            if levels:
                threshold = max(50.0, (sum(levels) / len(levels)) * 2.5)
                _recognizer.energy_threshold = threshold

        max_chunks = int(rate / _RAW_CHUNK * max_duration)
        silence_chunks_needed = max(1, int(rate / _RAW_CHUNK * silence_duration))
        silence_streak = 0
        speech_detected = False

        print(f"\n🎤 Listening (device='{dev_info.get('name')}', channels={opened_channels}, "
              f"threshold={round(threshold)})...")
        for _ in range(max_chunks):
            data = stream.read(_RAW_CHUNK, exception_on_overflow=False)
            frames.append(data)
            rms = _compute_rms(data)
            if rms > threshold:
                speech_detected = True
                silence_streak = 0
            else:
                silence_streak += 1
            if speech_detected and silence_streak >= silence_chunks_needed:
                break
    except Exception as e:
        raise MicrophoneUnavailable(f"Error while reading raw audio frames: {e}")
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        pa.terminate()

    if not frames:
        return None

    raw_bytes = b"".join(frames)
    if opened_channels > 1:
        raw_bytes = _downmix_to_mono(raw_bytes, opened_channels)

    # Rough check: if literally nothing exceeded the threshold, treat as silence.
    if _compute_rms(raw_bytes) <= threshold * 0.5:
        return None

    sample_width = 2  # paInt16 = 2 bytes/sample, matches _RAW_FORMAT above
    return sr.AudioData(raw_bytes, rate, sample_width)


# ---------------------------------------------------------------------------
# Microphone diagnostics
# ---------------------------------------------------------------------------
def list_microphones():
    """Return [(index, name), ...] of all detected audio input devices."""
    try:
        names = sr.Microphone.list_microphone_names()
        return list(enumerate(names))
    except Exception as e:
        print(f"[Warning] Could not enumerate microphones: {e}")
        return []


def list_microphones_text() -> str:
    mics = list_microphones()
    if not mics:
        return ("No audio input devices were detected at all. This usually means PyAudio "
                "isn't actually usable (even if pip says it's installed), or the OS/container "
                "has no microphone permission granted to this process. Try: pip install pyaudio "
                "(on Linux you may first need: sudo apt install portaudio19-dev), and double-check "
                "OS-level microphone permissions for your terminal/Python.")
    lines = [f"{i}: {name}" for i, name in mics]
    if _MIC_DEVICE_INDEX is not None:
        current = f"\nCurrently using: device {_MIC_DEVICE_INDEX} ('{mics[_MIC_DEVICE_INDEX][1] if _MIC_DEVICE_INDEX < len(mics) else '?'}')"
    else:
        current = ("\nCurrently using: system default device. If that's not your real mic, set "
                    "MIC_DEVICE_INDEX in your .env to one of the numbers above (a number is more "
                    "reliable than a name).")
    return "Available microphones:\n" + "\n".join(lines) + current


# ---------------------------------------------------------------------------
# macOS-specific: real permission check via AVFoundation (TCC), because
# PyAudio/PortAudio does NOT reliably register with or trigger macOS's actual
# permission system. This means: (a) the switch in System Settings can look
# "on" while macOS still has a stale "denied" decision cached against this
# exact process from before you enabled it, and (b) PyAudio's open() call
# then fails to produce a stream SILENTLY — no real exception, nothing to
# catch — which is exactly the "stream was None" symptom. Querying/
# requesting through AVFoundation instead gets the real, authoritative
# answer instead of guessing from a PyAudio side-effect.
# ---------------------------------------------------------------------------
def check_mac_mic_permission():
    """Returns (status, message):
      status: 'authorized' | 'denied' | 'restricted' | 'not_determined' | 'unknown'
      message: human-readable explanation / next step.
    On non-macOS platforms, returns ('authorized', 'Not macOS — no TCC check needed.')."""
    if sys.platform != "darwin":
        return "authorized", "Not macOS — no permission check needed."

    try:
        import AVFoundation
    except ImportError:
        return "unknown", (
            f"Can't verify macOS microphone permission directly — the AVFoundation binding isn't "
            f"installed. Fix with:\n    {sys.executable} -m pip install pyobjc-framework-AVFoundation\n"
            f"Without it, we can only infer permission indirectly from PyAudio, which is unreliable."
        )

    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)

    if status == 3:  # AVAuthorizationStatusAuthorized
        return "authorized", "macOS reports microphone access is authorized for this process."

    if status == 2:  # AVAuthorizationStatusDenied
        return "denied", (
            "macOS has a DENIED decision cached for this exact process — this is very likely the real "
            "cause even though you enabled it in System Settings, because that toggle doesn't always "
            "clear an old cached denial for command-line/IDE-launched processes. Fix:\n"
            "  1. In macOS Terminal.app (not VS Code), run:  tccutil reset Microphone\n"
            "  2. Fully quit VS Code (Cmd+Q) — closing the window is not enough.\n"
            "  3. Reopen VS Code, open a fresh integrated terminal, and run the agent again.\n"
            "  4. macOS should now show a real permission popup the moment the mic is first used — click Allow."
        )

    if status == 1:  # AVAuthorizationStatusRestricted
        return "restricted", ("Microphone access is restricted by macOS system policy (parental controls "
                               "or an MDM profile) and cannot be granted through normal settings.")

    # status == 0: AVAuthorizationStatusNotDetermined — actively trigger the real system prompt
    result = {"granted": None}
    done = threading.Event()

    def _on_result(granted):
        result["granted"] = bool(granted)
        done.set()

    print("[Info] macOS has not yet asked about microphone access for this process — "
          "requesting it now. Look for a system permission popup and click Allow.")
    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        AVFoundation.AVMediaTypeAudio, _on_result
    )
    done.wait(20)

    if result["granted"] is True:
        return "authorized", "Permission granted just now."
    if result["granted"] is False:
        return "denied", "Permission was denied or the system dialog was dismissed/timed out."
    return "unknown", "The permission dialog did not respond in time — try again."


def test_microphone() -> str:
    """Record a short live sample and report exactly what happened, so you
    can debug 'it doesn't hear me' with facts instead of guessing at
    thresholds. Call this via the 'test microphone' voice/text command.
    Uses the raw PyAudio path directly — see _raw_record() above for why."""
    perm_status, perm_message = check_mac_mic_permission()
    print(f"[Mic Test] macOS permission status: {perm_status}")
    if perm_status in ("denied", "restricted"):
        return perm_message

    print("[Mic Test] Say something in the next few seconds...")
    try:
        audio = _raw_record(max_duration=6.0)
    except MicrophoneUnavailable as e:
        return str(e)

    if audio is None:
        return (f"No audio above the silence threshold was captured. The stream opened and read "
                f"successfully (this is not a permission or device-open problem), but nothing loud "
                f"enough to count as speech came through. Try speaking louder/closer to the mic, "
                f"check the input isn't muted or at zero gain at the OS level, or set "
                f"SR_ENERGY_THRESHOLD lower in your .env (try 100-200).")

    try:
        text = _recognizer.recognize_google(audio)
        return (f"Success — captured audio and understood it as: \"{text}\". Your microphone, device "
                f"selection, and recognition pipeline are all working correctly.")
    except sr.UnknownValueError:
        return ("Audio WAS captured (so the device and volume level are fine), but the recognizer "
                "couldn't turn it into words. Try speaking closer to the mic or in a quieter room.")
    except sr.RequestError as e:
        return f"Audio was captured, but the online recognition service failed — check your internet connection: {e}"


# ---------------------------------------------------------------------------
# Voice persona management
# ---------------------------------------------------------------------------
def _create_engine():
    """Create a brand-new pyttsx3 engine configured from the saved persona.

    Why not reuse one global engine (as before): pyttsx3's macOS driver
    (NSSpeechSynthesizer) has a well-documented issue where a single
    long-lived engine instance's internal state can get "stuck" after
    repeated say()/runAndWait() cycles — property changes like voice/rate
    silently stop taking effect, even though setProperty() reports success.
    This is exactly the "voice selection does nothing" symptom. Creating a
    fresh engine per utterance, freshly configured from disk every time,
    sidesteps that stuck-state class of bug entirely — each utterance is
    guaranteed to reflect whatever was most recently saved to voice_config.json."""
    eng = pyttsx3.init()
    cfg = _load_config()

    voice_id = cfg.get("voice_id")
    if voice_id:
        try:
            eng.setProperty('voice', voice_id)
        except Exception as e:
            print(f"[Warning] Could not apply saved voice_id: {e}")

    eng.setProperty('rate', cfg.get("rate", DEFAULT_RATE))

    tone = cfg.get("tone")
    if tone and tone in TONE_PRESETS:
        try:
            eng.setProperty('pitch', TONE_PRESETS[tone]["pitch"])
        except Exception:
            pass  # pitch isn't supported by every driver — silently skip

    return eng


def list_voices():
    """Enumerate installed system voices using a throwaway engine instance
    (never the shared long-lived state some drivers get stuck in)."""
    eng = pyttsx3.init()
    try:
        voices = eng.getProperty('voices')
        result = []
        for idx, v in enumerate(voices):
            result.append({
                "index": idx,
                "id": v.id,
                "name": v.name,
                "gender": _guess_gender(v),
            })
        return result
    finally:
        try:
            eng.stop()
        except Exception:
            pass


def _guess_gender(voice) -> str:
    name = (voice.name or "").lower()
    vid = (voice.id or "").lower()
    for h in FEMALE_HINTS:
        if h in name or h in vid:
            return "female"
    for h in MALE_HINTS:
        if h in name or h in vid:
            return "male"
    return "unknown"


def _load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[Warning] could not save voice config: {e}")


def _try_apply_voice(candidate: dict):
    """Verify a candidate voice actually works with a throwaway engine.
    Returns True/False. A voice can be silently rejected by macOS's
    NSSpeechSynthesizer with no exception at all — most commonly because
    it's listed as available but was never actually downloaded (System
    Settings > Accessibility > Spoken Content > System Voice > Manage
    Voices). setProperty() just keeps the previous voice in that case."""
    eng = pyttsx3.init()
    try:
        eng.setProperty('voice', candidate["id"])
        applied = eng.getProperty('voice')
        return applied == candidate["id"]
    finally:
        try:
            eng.stop()
        except Exception:
            pass


def set_persona(gender: str = None, tone: str = None, index: int = None) -> str:
    cfg = _load_config()
    voices = list_voices()
    messages = []
    chosen = None
    tried_and_failed = []

    if index is not None:
        if 0 <= index < len(voices):
            candidate = voices[index]
            if _try_apply_voice(candidate):
                chosen = candidate
            else:
                tried_and_failed.append(candidate["name"])
        else:
            messages.append(f"Voice index {index} is out of range (valid: 0-{len(voices) - 1}). Say 'list voices' to see options.")

    elif gender:
        gender = gender.lower().strip()
        # Try EVERY matching voice, not just the first — a voice being
        # listed doesn't mean it's actually installed/downloaded, and only
        # trying one candidate was the actual bug here.
        candidates = [v for v in voices if v["gender"] == gender]
        candidates += [v for v in voices if v["gender"] != gender]  # last-resort fallback pool
        for candidate in candidates:
            if _try_apply_voice(candidate):
                chosen = candidate
                if candidate["gender"] != gender:
                    messages.append(f"No working voice labeled '{gender}' was found — using closest working match '{chosen['name']}'.")
                break
            tried_and_failed.append(candidate["name"])

        if not chosen and not voices:
            messages.append("No system TTS voices were detected at all — pyttsx3 may not be configured correctly on this machine.")

    if chosen:
        messages.insert(0, f"Voice set to '{chosen['name']}' ({chosen['gender']}).")
        cfg["voice_id"] = chosen["id"]
        cfg["gender"] = chosen["gender"]
    elif tried_and_failed:
        messages.insert(0, (
            f"Tried {len(tried_and_failed)} voice(s) ({', '.join(tried_and_failed[:5])}"
            f"{'...' if len(tried_and_failed) > 5 else ''}) and none of them actually took effect. "
            f"This almost always means those voices are listed but not actually downloaded — open "
            f"System Settings > Accessibility > Spoken Content > System Voice > Manage Voices, "
            f"download the one you want, and try again."
        ))

    if tone:
        tone = tone.lower().strip()
        preset = TONE_PRESETS.get(tone)
        if preset:
            cfg["tone"] = tone
            cfg["rate"] = preset["rate"]
            note = f"Tone set to {tone} (speaking rate {preset['rate']})."
            messages.append(note)
        else:
            valid = ", ".join(TONE_PRESETS.keys())
            messages.append(f"Unknown tone '{tone}'. Choose from: {valid}.")

    _save_config(cfg)

    if chosen or tone:
        # This call goes through speak() below, which builds a FRESH engine
        # from the config we just saved — so the confirmation itself is
        # spoken in the newly selected voice/tone, not the old one.
        speak("This is what my voice sounds like now.")

    return " ".join(messages) if messages else "No changes made — specify a gender, tone, or voice index."


def get_persona_status() -> str:
    cfg = _load_config()
    gender = cfg.get("gender", "default")
    tone = cfg.get("tone", "default")
    voice_id = cfg.get("voice_id")
    if voice_id:
        name = next((v["name"] for v in list_voices() if v["id"] == voice_id), voice_id)
    else:
        name = "system default"
    return f"Current voice: '{name}' (gender={gender}, tone={tone})."


def list_voices_text() -> str:
    voices = list_voices()
    if not voices:
        return "No system TTS voices were found — pyttsx3 may not be configured correctly on this machine."
    lines = [f"{v['index']}: {v['name']} ({v['gender']})" for v in voices]
    note = ""
    if len(voices) == 1:
        note = "\nOnly one voice is installed on this system, so male/female switching has nothing to switch between."
    return "Available voices:\n" + "\n".join(lines) + note


# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------
def speak(text: str):
    print(f"\n🤖 Agent: {text}")
    try:
        eng = _create_engine()  # fresh engine, freshly configured from disk every time
        eng.say(text)
        eng.runAndWait()
        try:
            eng.stop()
        except Exception:
            pass
    except Exception as e:
        print(f"[Warning] TTS playback failed (audio output may not be configured on this system): {e}")


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------
_mac_perm_checked = False
_mac_perm_status = "unknown"
_mac_perm_message = ""


def _ensure_mac_permission_checked():
    """Run the real macOS permission check exactly once per process run
    (not on every listen() call — the listener thread calls listen() in a
    tight loop, and the AVFoundation check, while cheap once resolved, can
    block up to 20s the very first time if macOS hasn't asked yet)."""
    global _mac_perm_checked, _mac_perm_status, _mac_perm_message
    if _mac_perm_checked:
        return _mac_perm_status, _mac_perm_message
    _mac_perm_status, _mac_perm_message = check_mac_mic_permission()
    print(f"[Info] macOS microphone permission status: {_mac_perm_status}")
    if _mac_perm_status in ("denied", "restricted"):
        print(_mac_perm_message)
    _mac_perm_checked = True
    return _mac_perm_status, _mac_perm_message


def listen(timeout: int = 5, phrase_time_limit: int = 15) -> str:
    """Speech-to-Text execution, using the raw PyAudio capture path (see
    _raw_record() above) rather than SpeechRecognition's Microphone wrapper.

    Return values:
      - non-empty string: recognized speech
      - "" (empty string): mic worked fine but nothing usable was heard —
        caller should just try again
      - raises MicrophoneUnavailable: a genuine device-level failure —
        caller should treat voice input as unavailable
    """
    perm_status, perm_message = _ensure_mac_permission_checked()
    if perm_status in ("denied", "restricted"):
        raise MicrophoneUnavailable(perm_message)

    audio = _raw_record(max_duration=phrase_time_limit + timeout, silence_duration=1.2)

    if audio is None:
        return ""  # normal: silence / nothing above threshold this round

    try:
        text = _recognizer.recognize_google(audio)
        print(f"👤 You: {text}")
        return text
    except sr.UnknownValueError:
        print("[Info] Audio was captured but not recognized as speech (mumbled, too quiet, or background noise).")
        return ""
    except sr.RequestError as e:
        print(f"[Warning] Speech recognition service unreachable (check internet connection): {e}")
        return ""
    except Exception as e:
        print(f"[Warning] Error reading audio: {e}")
        return ""
