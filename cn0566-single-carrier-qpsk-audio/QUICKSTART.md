# Quick start

```bash
chmod +x *.sh *.py
./setup_rpi.sh
./run_simulation.sh
```

Inspect the physical map and safe values:

```bash
cat PORT_PROFILE.md
nano config.env
```

Then proceed in order:

```bash
./run_rf_sanity.sh
./run_qpsk_no_play.sh
./run_qpsk_audio.sh
```

Use Ctrl-C once and allow cleanup to finish. The application returns Pluto TX2
attenuation to -88 dB and destroys IIO buffers during normal shutdown.
