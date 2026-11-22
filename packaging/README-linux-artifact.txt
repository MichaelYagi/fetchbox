fetchbox is the server as one file, for Linux x86_64 (no tray).

    chmod +x fetchbox
    ./fetchbox serve            # then open http://127.0.0.1:8787

Data goes in ~/.local/share/fetchbox (or --data DIR). To let other computers reach it,
set a password first (./fetchbox reset-password), then ./fetchbox serve --host 0.0.0.0.
To run it as a service, see the systemd example in the project's README.
