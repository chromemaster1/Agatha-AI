# macOS Integrations

Agatha uses Apple Events through `/usr/bin/osascript` to create entries in Calendar, Reminders, and Notes, to read Mail, and to control Music.app or Spotify when requested. macOS asks for Automation permission the first time each target app is used. Agatha cannot grant this permission itself; approve the macOS prompt.

## Automation Access

On macOS Monterey, open **System Preferences > Security & Privacy > Privacy > Automation**. Enable Calendar, Notes, Reminders, or Mail under the app that launched Agatha (for example, Terminal or Visual Studio Code). If access was denied, change it there and fully quit/reopen the launcher before retrying.

- Calendar requests create and verify events in Calendar.app's first writable calendar.
- Note requests create notes in Notes.app. To-do items are also stored in this project and synchronized to the `Agent To-Do List` note.
- Timed reminders are exported as alarm-bearing `.ics` files and are **not scheduled until imported**. On this Monterey machine, Reminders.app Apple Events can read lists but due-date writes fail in ReminderKit; Agatha therefore does not claim they succeeded.
- Email summaries read Mail.app and require an account configured in Mail.
- Music requests can search the local Music library, control Music.app or Spotify playback, and open supported local audio files in Music.app. The chosen app must be installed and accessible to the logged-in user.

## Other Privacy Permissions

- **Messages:** reading texts is off by default. To opt in, set `ALLOW_MESSAGES_ACCESS=true` in `.env`, then grant **Full Disk Access** to the launcher under **System Preferences > Security & Privacy > Privacy > Full Disk Access**. Fully quit and reopen the launcher after changing access.
- **Microphone:** voice input requires permission under **System Preferences > Security & Privacy > Privacy > Microphone**. Approve the prompt for the app that launched Agatha.
- **Public voice focus:** enable with `enable public voice mode`, or set `AGATHA_PUBLIC_VOICE_MODE=true` before startup. In this mode, microphone transcripts are ignored unless they begin with “Hey Agatha.” This reduces accidental pickup but is not voice biometrics: a passer-by who says the wake phrase can still trigger Agatha. Live speech transcription currently uses the configured SpeechRecognition Google service, so captured audio is sent to that service for recognition.
- **Music playback:** requests can search the local Music library, control Music.app or Spotify, and open supported local audio files in Music.app. Approve the macOS Automation prompt for Music or Spotify when asked.
- **Public voice focus:** enable it with `enable public voice mode`, or set `AGATHA_PUBLIC_VOICE_MODE=true` before startup. In this mode, microphone transcripts are ignored unless they begin with “Hey Agatha.” This reduces accidental pickup but is not voice biometrics: a passer-by who says the wake phrase can still trigger Agatha. Live speech transcription currently uses the configured SpeechRecognition Google service, so captured audio is sent to that service for recognition.
- **Files:** storage scans and code tools can access files the running user can read. macOS may separately deny protected folders; grant access only to the folders Agatha needs, or select an accessible folder.
- **Network services:** web research, market data, cloud LLM providers, and social posting need network access. Provider credentials and OAuth consent are separate from macOS Automation permissions.

## Safety Review and Tutoring

- Ask Agatha to review a link, uploaded document/image/code file, or pasted text for suspicious indicators. Checks are read-only: local URL and scam heuristics, file signature comparison, SHA-256, and code-pattern warnings. If `clamscan` from ClamAV is installed, an uploaded file is also scanned locally.
- These checks are triage, not a guarantee. A clean ClamAV result or no heuristic match does not prove a file or site safe. Links are not opened or reputation-checked, and files are not blocked, changed, executed, or quarantined.
- Agatha can explain suspicious indicators and suggest safer fixes, but she cannot authenticate a certificate, issuer, image, or proof from appearance/text alone. Confirm documents directly with the issuing organization using contact details obtained independently.
- Ask Agatha to teach, tutor, quiz, or walk through a subject for structured explanations, worked examples, and practice. Tutoring adapts within the conversation; it does not retrain the underlying language model.

## Tutoring and Image Creation

- Ask Agatha to teach, tutor, quiz, or walk through a subject for a structured explanation, examples, and practice. It can adapt within the current conversation, but this does not retrain the underlying language model.
- Image creation requires a configured image provider. Include “3D” or “3D render” in the request for detailed three-dimensional render direction.

## Alarm Support

The Clock.app bundle is not installed in this macOS 12 environment. Agatha exports an alarm-bearing `.ics` file for “alarm” requests; it does not create a Clock.app alarm or activate the notification until the user imports the file. Check the calendar app's notification settings and Focus modes afterward.

Existing entries in the project's `calendar.json` are retained as legacy data and are not automatically imported into Calendar.app.

## Windows and Android

- Windows Calendar writes use classic Outlook COM automation when the Outlook desktop application and `pywin32` are available. New Outlook and machines without Outlook get an `.ics` export in `Documents/Agatha Calendar`; import it manually. Windows does not offer a universal permission that lets an arbitrary Python process write to every calendar app.
- Android calendar writes use the system event-insert screen when Agatha itself is running on Android with the `am` intent service. Review and tap **Save** in the calendar app. When Agatha runs on a computer, this targets that computer and cannot reach a connected phone.
- Other systems receive `.ics` calendar files. Timed reminders include a display alarm in the file, but the user must import it before the calendar app can deliver a notification.
- Notes on non-macOS systems are Markdown files in `Documents/Agatha Notes`. Android shared storage is used only if it is already mounted and writable; in Termux, Android may require the user to run `termux-setup-storage` first.

Native Android and Windows permissions are granted by those operating systems/apps and may require app packaging, a manifest permission, OAuth, or an explicit confirmation screen. This Python desktop project cannot grant them automatically. A cloud-hosted copy also cannot control apps or files on a user's device; that requires a separately installed companion client.

## Data and Consent

Agatha requests access only when a feature is used. Permission denials are returned as errors rather than treated as successful app writes. Messages access additionally requires the explicit `.env` opt-in; local to-do data is stored in `todo_list.json` beside the project code.
