# macOS Integrations

Agatha uses Apple Events through `/usr/bin/osascript` to create entries in Calendar, Reminders, and Notes, and to read Mail. macOS asks for Automation permission the first time each target app is used. Agatha cannot grant this permission itself; approve the macOS prompt.

## Automation Access

On macOS Monterey, open **System Preferences > Security & Privacy > Privacy > Automation**. Enable Calendar, Notes, Reminders, or Mail under the app that launched Agatha (for example, Terminal or Visual Studio Code). If access was denied, change it there and fully quit/reopen the launcher before retrying.

- Calendar requests create and verify events in Calendar.app's first writable calendar.
- Note requests create notes in Notes.app. To-do items are also stored in this project and synchronized to the `Agent To-Do List` note.
- Timed reminders are exported as alarm-bearing `.ics` files and are **not scheduled until imported**. On this Monterey machine, Reminders.app Apple Events can read lists but due-date writes fail in ReminderKit; Agatha therefore does not claim they succeeded.
- Email summaries read Mail.app and require an account configured in Mail.

## Other Privacy Permissions

- **Messages:** reading texts is off by default. To opt in, set `ALLOW_MESSAGES_ACCESS=true` in `.env`, then grant **Full Disk Access** to the launcher under **System Preferences > Security & Privacy > Privacy > Full Disk Access**. Fully quit and reopen the launcher after changing access.
- **Microphone:** voice input requires permission under **System Preferences > Security & Privacy > Privacy > Microphone**. Approve the prompt for the app that launched Agatha.
- **Files:** storage scans and code tools can access files the running user can read. macOS may separately deny protected folders; grant access only to the folders Agatha needs, or select an accessible folder.
- **Network services:** web research, market data, cloud LLM providers, and social posting need network access. Provider credentials and OAuth consent are separate from macOS Automation permissions.

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
