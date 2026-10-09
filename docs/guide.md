[**العربية**](guide.ar.md) · English

# User guide

## The main window

<img src="screenshots/en/main.png" alt="The main window" width="700">

- **Watch folder** (top): your anime folder. **Change…** picks another one. Use the folder that holds your series, for example `D:\Videos\Anime`, not a whole drive.
- **Watch** switch (top right): while it's on, the app keeps every `.ass` subtitle in the folder styled, including new ones a few seconds after they arrive. The first time you turn it on for a folder, the app shows how many subtitle files it will restyle and asks you to confirm.
- **Live preview**: your preview text in your font and colour, with the black outline the app adds. Turn on **Real frame** to show a still from one of your videos behind it, and **Shuffle** to pick another. **Subtitle line** shows a real line from your subtitles on the frame where it plays. **Preview text…** changes the text.
- **Font**: type a name, use the mouse wheel or arrow keys to go through fonts, or click **▾** to open the font picker. The star marks a favourite.
- **Color**: the colour of the dialogue text.
- **B / I / U**: bold, italic, underline.
- **Size**: **—** keeps each file's own size. **S / M / L** sets a size that looks the same in every release and every font. **− / +** fine-tunes the size for the current font.
- **Apply to library**: restyles the files that are already in your library, for example after you change the font. It skips files that are already up to date.
- **Extract subs**: saves subtitles from new videos as separate files next to them. Choose the languages with **▾**.
- **Restore originals…**: puts the saved originals back.
- **⋯**: more options (see below).
- **Status line** (bottom): what the app did last, and **Issues** when something went wrong.

## Fonts

The font picker shows every font in its own style, with a sample in Arabic and English. **Arabic-capable** shows only fonts that can display Arabic. **★ Favorites** shows the fonts you starred.

The font also has to be installed wherever you watch: on another PC, a TV or a phone, install the same font there, or choose a font that's already installed on it.

If the chosen font can't display a file's language (for example English subtitles with an Arabic-only font), the app leaves that file as it is instead of filling it with empty boxes.

## Your originals

- Before a subtitle is restyled for the first time, the original is copied into **Original Subs** next to it. Don't delete these folders if you might want the originals back.
- If a downloader replaces a subtitle with a newer release, the newer release becomes the saved original. The older one is kept in `Original Subs\.older`.
- **Restore originals…** (with Watch turned off) puts the originals back for the series you choose. Restored files stay original: Watch leaves them alone until a new version of the file arrives, and **Apply to library** restyles them if you change your mind.

## Extracting subtitles

Many videos carry their subtitles inside the video file. With **Extract subs** on, the app saves the subtitles of each **new** video as `.ass` or `.srt` files next to it, in the languages you chose with **▾** (all languages if you chose none). **⋯ → Extract all subs** does every video in the folder. Image-based subtitles from DVDs can't be extracted.

## The ⋯ menu

- **Extract all subs**: extract from every video in the folder.
- **Open log**: the app's activity log, useful when reporting a problem.
- **Reset…**: clear settings or the app's list of styled files. Your subtitles and backups are never deleted.
- **Autostart at login** and **Start minimized**: start the app with Windows, in the notification area.
- **Language**: English or العربية (the app restarts).
- **Check for updates once a day**: off by default. When on, the app tells you when a new version is available.
- **User guide**, **Report a problem…**, **About**.

## The notification area

When you close the window, the app asks whether to keep running in the notification area (next to the clock) or quit. While it's there it keeps watching your folder. Click its icon to open the window. Windows sometimes hides new icons under the **^** arrow; you can drag it out to keep it visible.

## When something goes wrong

The **Issues** panel at the bottom of the window lists the files the app couldn't change, with the reason in plain words. Common reasons:

- **Windows blocked the change**: the file is read-only, or Ransomware protection (Controlled folder access) is on. See [installation help](install-help.md#4-antivirus-warnings).
- **Another program has the file open**: often your video player. The app tries again later.
- **The folder isn't reachable**: an unplugged drive or an offline network share.
- **Changed by another program after it was styled**: another copy of the app, or a subtitle editor. The app leaves it alone; **Apply to library** restyles it.

If the app itself runs into an error, it shows a window with **Copy details** and **Open log folder**. Please include those details when you [report a problem](https://github.com/roko-tech/Anime-Subs-Customizer/issues/new/choose). Logs contain your folder and file names, so read them first.

## Where things are

| What | Where |
|---|---|
| The app | `%LOCALAPPDATA%\Programs\Anime Subs Customizer` |
| Settings, the list of styled files, logs | `%LOCALAPPDATA%\Anime Subs Customizer` |
| Originals | an `Original Subs` folder next to each subtitle |

The portable version keeps its settings in its own folder.
