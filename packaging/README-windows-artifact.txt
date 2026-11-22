Two ways to run Fetchbox on Windows 10 or 11:

  Fetchbox-...-Setup.exe   Installs Fetchbox for your user (no administrator needed):
                           a Start menu entry, optionally starting when you sign in,
                           and an uninstaller in Settings > Apps.
  Fetchbox-...zip          The same program without installing: unzip, double-click
                           Fetchbox.exe.

Fetchbox starts in the system tray (the blue box icon near the clock; Windows may hide
it under the ^ arrow) and opens in your browser. Right-click the icon to open Fetchbox
again, pause downloads, stop or restart it, open its data or logs folder, start it at
sign-in, or quit.

Windows SmartScreen may say it "protected your PC", because this build isn't signed.
Click "More info", then "Run anyway". Your data is kept in %LOCALAPPDATA%\Fetchbox;
uninstalling asks before deleting it, and never touches your movies or downloads.

From a terminal it is also the full command line, e.g.  Fetchbox.exe serve --port 8787
or  Fetchbox.exe reset-password.
