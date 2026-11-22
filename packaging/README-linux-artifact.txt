Fetchbox for Linux x86_64, three ways (no tray; the web interface is at
http://127.0.0.1:8787):

  .deb       Debian, Ubuntu and relatives: a service that starts with the computer.
               sudo apt install ./Fetchbox-Linux-x86_64-*.deb
             It runs as the "fetchbox" user with its data in /var/lib/fetchbox.
             Settings (network access, downloads folder): /etc/default/fetchbox.
             Logs: journalctl -u fetchbox. Remove: sudo apt remove fetchbox (your data
             in /var/lib/fetchbox is kept).
  .AppImage  One file, any distribution: chmod +x it and run it (it starts the
             server). With arguments it is the command line, e.g.
             ./Fetchbox-*.AppImage reset-password
  .tar.gz    The plain program:  ./fetchbox serve

Outside the .deb, data goes in ~/.local/share/fetchbox (or --data DIR). To let other
computers reach it, set a password first (fetchbox reset-password), then serve with
--host 0.0.0.0.
